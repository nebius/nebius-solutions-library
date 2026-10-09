import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import json

import buffer as b  # noqa: E402

METRICS = """# HELP kn_revision_concurrency_stable x
kn_revision_concurrency_stable{k8s_namespace_name="models",kn_revision_name="llm-predictor-00002",kn_service_name="llm-predictor"} 5.2
kn_revision_concurrency_target{k8s_namespace_name="models",kn_revision_name="llm-predictor-00002",kn_service_name="llm-predictor"} 4
kn_revision_pods_desired{k8s_namespace_name="models",kn_revision_name="llm-predictor-00002",kn_service_name="llm-predictor"} 2
kn_revision_concurrency_stable{k8s_namespace_name="models",kn_revision_name="idle-predictor-00001",kn_service_name="idle-predictor"} 0
kn_revision_concurrency_target{k8s_namespace_name="models",kn_revision_name="idle-predictor-00001",kn_service_name="idle-predictor"} 4
kn_revision_rps_stable{k8s_namespace_name="models",kn_revision_name="rps-predictor-00001",kn_service_name="rps-predictor"} 12
kn_revision_rps_target{k8s_namespace_name="models",kn_revision_name="rps-predictor-00001",kn_service_name="rps-predictor"} 5
"""


def test_parse_and_demand():
    m = b.parse_metrics(METRICS)
    assert b.demand(m["llm-predictor-00002"]) == 2       # 5.2 / 4 -> 2 replicas
    assert b.demand(m["idle-predictor-00001"]) == 0
    assert b.demand(m["rps-predictor-00001"]) == 3       # 12 / 5 -> 3
    assert b.demand({}) is None


def test_floor():
    assert b.floor_for(2, 0, 4, 1) == 3
    assert b.floor_for(2, 0, 2, 1) == 2                  # never above max
    assert b.floor_for(0, 0, 4, 1) == 0                  # idle: the model's own minimum, scale to zero stays
    assert b.floor_for(None, 1, 4, 1) == 1               # unmeasured: the minimum
    assert b.floor_for(3, 2, 8, 0) == 2                  # no buffer


class FakeCustom:
    def __init__(self, isvcs, revisions):
        self.isvcs, self.revisions, self.patches = isvcs, revisions, []

    def list_namespaced_custom_object(self, group, version, ns, plural):
        return {"items": self.isvcs if plural == "inferenceservices" else self.revisions}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.patches.append((name, body["metadata"]["annotations"]))
        for r in self.revisions:
            if r["metadata"]["name"] == name:
                ann = r["metadata"].setdefault("annotations", {})
                for k, v in body["metadata"]["annotations"].items():
                    if v is None:
                        ann.pop(k, None)
                    else:
                        ann[k] = v


class FakeCore:
    """The autoscaler metrics behind call_api, and the API's model copies (ConfigMaps with spec.json)."""
    class _Client:
        def __init__(self, text):
            self.text = text

        def call_api(self, *a, **k):
            class R:
                data = self.text.encode()
            return R()

    class _CM:
        def __init__(self, mid, buf):
            self.data = {"spec.json": json.dumps({"id": mid, "scaling": {"buffer": buf}})}
            self.metadata = type("M", (), {"labels": {"serverless2.nebius/model": mid}})()

    def __init__(self, text, buffers=None):
        self.api_client = self._Client(text)
        self.buffers = buffers or {}

    def list_namespaced_config_map(self, ns, label_selector=None):
        return type("L", (), {"items": [self._CM(m, buf) for m, buf in self.buffers.items()]})()


def isvc(name, buf, min_=0, max_=4, cooldown=None):
    ann = {b.COOLDOWN_ANN: cooldown} if cooldown else {}
    BUFFERS[name] = buf
    return {"metadata": {"name": name, "annotations": ann}, "spec": {"predictor": {"minReplicas": min_, "maxReplicas": max_}}}


BUFFERS: dict = {}


def rev(name, model, active=True, ann=None):
    return {"metadata": {"name": name, "annotations": dict(ann or {}),
                         "labels": {"serverless2.nebius/model": model, "serving.knative.dev/routingState": "active" if active else "reserve"}}}


def test_raise_hold_lower_and_restore():
    BUFFERS.clear()
    custom = FakeCustom([isvc("llm", 1), isvc("idle", 1)], [rev("llm-predictor-00002", "llm"), rev("idle-predictor-00001", "idle")])
    out = b.reconcile_worker(custom, FakeCore(METRICS, BUFFERS), now=1000.0)
    assert out == {"llm-predictor-00002": 3}                       # demand 2 + buffer 1; the idle model keeps min 0
    assert custom.patches[-1][1] == {b.MIN_ANN: "3", b.RAISED_ANN: "1000", b.LOW_ANN: None}
    # demand drops to 0: the first quiet sample only starts the clock, a busy sample in between resets it, and the
    # floor returns to the minimum (our marks go) once demand has stayed low for a whole cooldown
    quiet = METRICS.replace('kn_revision_name="llm-predictor-00002",kn_service_name="llm-predictor"} 5.2', 'kn_revision_name="llm-predictor-00002",kn_service_name="llm-predictor"} 0')
    assert b.reconcile_worker(custom, FakeCore(quiet, BUFFERS), now=1060.0) == {}
    assert custom.patches[-1][1] == {b.LOW_ANN: "1060"}
    assert b.reconcile_worker(custom, FakeCore(METRICS, BUFFERS), now=1075.0) == {}        # one busy sample: clock reset
    assert custom.patches[-1][1] == {b.LOW_ANN: None}
    assert b.reconcile_worker(custom, FakeCore(quiet, BUFFERS), now=1100.0) == {}
    assert b.reconcile_worker(custom, FakeCore(quiet, BUFFERS), now=1150.0) == {}        # 50 s low: not yet
    assert b.reconcile_worker(custom, FakeCore(quiet, BUFFERS), now=1230.0) == {"llm-predictor-00002": 0}
    assert custom.patches[-1][1] == {b.MIN_ANN: "0", b.RAISED_ANN: None, b.LOW_ANN: None}


def test_buffer_removed_restores_the_floor_and_inactive_revisions_are_left_alone():
    BUFFERS.clear()
    custom = FakeCustom([isvc("llm", 0, min_=1)], [rev("llm-predictor-00002", "llm", ann={b.MIN_ANN: "3", b.RAISED_ANN: "1"}),
                                                   rev("llm-predictor-00001", "llm", active=False)])
    assert b.reconcile_worker(custom, FakeCore(METRICS, BUFFERS), now=5000.0) == {}          # starts the clock
    assert b.reconcile_worker(custom, FakeCore(METRICS, BUFFERS), now=5200.0) == {"llm-predictor-00002": 1}
    assert len(custom.patches) == 2
