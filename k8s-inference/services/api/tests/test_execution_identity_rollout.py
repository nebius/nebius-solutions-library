"""API59: the sanitized execution identity of a run class on the public model (docs/API.md "Models": image, immutable,
per-class images, delegates ids, retry, finalize; never the container configuration), the endpoint's rollout state
(kube._rollout: Knative serves the latest ready revision, so a spec change is attributed only when converged), and the
opt-in convergence fence of an endpoint call (`execution.require_converged`: 409 while diverged, admitted at zero when
converged; the ready revision and image recorded on the call as admission-time evidence)."""

import copy
from types import SimpleNamespace as NS

import catalog
import kube
import models
from test_api import KEY_INFO, H, admin_keys, client, cluster, fake_keys  # noqa: F401
from test_async_private import _bind, _tenant

NS_T = "tenant-demo"


def test_run_class_projects_its_execution_identity_without_the_spec(client, admin_keys, monkeypatch):
    c, _cl = client
    import auth

    async def info(key):
        return {
            **KEY_INFO,
            "models": ["esmfold2-fast"],
            "metadata": {"tenant": "demo", "legacy": {"principal_id": "p1"}},
        }

    monkeypatch.setattr(auth, "key_info", info)
    from test_api import FLEET

    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    spec = {
        "id": "esmfold2-fast",
        "kind": "job",
        "image": "cr.example/cancer-immunotherapy/esmfold2-fast@sha256:" + "6e" * 32,
        "command": "fold --secret-flag",
        "env": {"TOKEN": "never-public"},
        "gpu": {"count": 1, "classes": ["h100"]},
        "delegates": ["esmfold2-collect"],
        "retry": {"max_attempts": 2},
        "finalize": {"command": "deliver", "on_exit_codes": [75]},
    }
    entry = catalog.normalise(models.to_entry(spec))
    collect = catalog.normalise(
        models.to_entry(
            {
                **spec,
                "id": "esmfold2-collect",
                "delegates": None,
                "retry": None,
                "finalize": None,
                "image": "cr.example/collect:1",
            }
        )
    )
    endpoint_before = catalog.get("llm-example")
    monkeypatch.setattr(catalog, "all_models", lambda: {"esmfold2-fast": entry, "esmfold2-collect": collect})
    monkeypatch.setattr(catalog, "get", lambda mid: {"esmfold2-fast": entry, "esmfold2-collect": collect}.get(mid))
    pub = catalog.to_public(entry)
    assert pub["spec"] is not None  # the raw projection carries it; the route strips it for non-admins
    assert pub["image"] == spec["image"].replace("cr.example/", "cr.example/") and pub["execution"] == {
        "image": entry["job"]["image"],
        "immutable": True,
        "images": None,
        "gpu_classes": ["h100"],
        "nodes": 1,
        "delegates": ["esmfold2-collect"],
        "retry": {"max_attempts": 2, "on_exit_codes": [137, 143]},
        "finalize": True,
    }
    assert "secret-flag" not in str(pub["execution"]) and "never-public" not in str(pub["execution"])
    assert catalog.to_public(collect)["execution"]["immutable"] is False  # a tag, not a digest
    assert catalog.to_public(endpoint_before)["execution"] is None  # endpoints: none
    # the restricted key: the class with its execution identity, spec null; the delegate class not granted: absent
    listed = {m["id"]: m for m in c.get("/v1/models", headers=H).json()}
    assert "esmfold2-fast" in listed and "esmfold2-collect" not in listed
    m = listed["esmfold2-fast"]
    assert (
        m["spec"] is None
        and m["image"] == entry["job"]["image"]
        and m["execution"]["delegates"] == ["esmfold2-collect"]
    )
    assert m["execution"]["immutable"] is True and m["execution"]["finalize"] is True
    one = c.get("/v1/models/esmfold2-fast", headers=H).json()
    assert one["execution"] == m["execution"] and one["spec"] is None
    assert c.get("/v1/models/esmfold2-collect", headers=H).status_code in (403, 404)
    # the per-class images are projected without `default`
    multi = catalog.normalise(
        models.to_entry({**spec, "id": "multi", "image": None, "images": {"default": "img:d", "h100": "img:h"}})
    )
    assert catalog.to_public(multi)["execution"]["images"] == {"h100": multi["job"]["images"]["h100"]}  # no `default`
    assert multi["job"]["images"]["h100"].endswith("/img:h")


def _pod(ready=True, image_id="docker://cr.example/m@sha256:5555", image="cr.example/m:5"):
    cs = NS(name="kserve-container", ready=ready, image=image, image_id=image_id)
    return NS(
        metadata=NS(creation_timestamp=None, labels={}),
        status=NS(phase="Running", container_statuses=[cs], conditions=[]),
    )


def _isvc(created, ready, rolled, generation=2, observed=2, image="cr.example/m:5", ready_cond=True, mode=None):
    return {
        "metadata": {
            "generation": generation,
            **({"annotations": {"serving.kserve.io/deploymentMode": mode}} if mode else {}),
        },
        "spec": {"predictor": {"containers": [{"name": "kserve-container", "image": image}]}},
        "status": {
            "observedGeneration": observed,
            "conditions": [{"type": "Ready", "status": "True" if ready_cond else "False"}],
            "components": {
                "predictor": {
                    "latestCreatedRevision": created,
                    "latestReadyRevision": ready,
                    "latestRolledoutRevision": rolled,
                }
            },
        },
    }


def _revision(image="cr.example/m:5", digest="cr.example/m@sha256:5555"):
    return {"spec": {"containers": [{"image": image}]}, "status": {"containerStatuses": [{"imageDigest": digest}]}}


def test_rollout_converges_only_on_observed_generation_revision_image_and_pods(monkeypatch):
    """Revision names alone are never image proof: convergence needs the controller to have reconciled the current
    spec (observedGeneration == generation: the pre-reconcile window after a catalog image change still shows the old
    revisions as one), one revision, the ready Revision's stored image equal to the spec's (valid at zero, from the
    Revision object), and every ready pod on that image (by digest or reference)."""
    # converged at zero: the stored Revision proves the image, no pod needed
    zero = _rollout_of(monkeypatch, _isvc("m-5", "m-5", "m-5"), [], {"m-5": _revision()})
    assert zero["converged"] is True and zero["serving_images"] == [] and zero["reason"] is None
    assert zero["revision_image"] == "cr.example/m:5" == zero["desired_image"] and zero["generation"] == 2
    # the pre-reconcile window: the spec moved to a new image (generation 3), the controller has not observed it, the
    # old revisions are still one -> not converged
    window = _rollout_of(
        monkeypatch,
        _isvc("m-5", "m-5", "m-5", generation=3, observed=2, image="cr.example/m:6"),
        [],
        {"m-5": _revision()},
    )
    # on KServe the witness is the predictor Service: in that window it still carries the old image (not propagated)
    assert window["converged"] is False and "spec not propagated" in window["reason"]
    assert "not the desired" in window["reason"]
    # reconciled, one revision, but the ready Revision's image is not the spec's -> not converged
    stale = _rollout_of(monkeypatch, _isvc("m-5", "m-5", "m-5", image="cr.example/m:6"), [], {"m-5": _revision()})
    assert (
        stale["converged"] is False
        and "ready revision image cr.example/m:5 is not the desired cr.example/m:6" in stale["reason"]
    )
    # diverged revisions (the new one Pending, the old one serving)
    diverged = _rollout_of(
        monkeypatch,
        _isvc("m-6", "m-5", "m-5", generation=3, observed=3, image="cr.example/m:6"),
        [_pod()],
        {"m-5": _revision()},
    )
    assert diverged["converged"] is False and "revisions differ" in diverged["reason"]
    assert diverged["serving_images"] == ["docker://cr.example/m@sha256:5555"]
    # converged with a ready pod on the Revision's digest, one starting pod ignored
    hot = _rollout_of(monkeypatch, _isvc("m-5", "m-5", "m-5"), [_pod(), _pod(ready=False)], {"m-5": _revision()})
    assert hot["converged"] is True and len(hot["serving_images"]) == 1
    # a ready pod on another image than the ready Revision (a stray replica) -> not converged
    stray = _rollout_of(
        monkeypatch,
        _isvc("m-5", "m-5", "m-5"),
        [_pod(image_id="docker://cr.example/m@sha256:4444")],
        {"m-5": _revision()},
    )
    assert stray["converged"] is False and "another image" in stray["reason"]
    # the Revision object missing, or an empty status: not converged, never a guess
    missing = _rollout_of(monkeypatch, _isvc("m-5", "m-5", "m-5"), [], {})
    assert missing["converged"] is False and missing["revision_image"] is None
    assert _rollout_of(monkeypatch, {"status": {}}, [], {})["converged"] is False
    # an absent latestRolledoutRevision (before the first rollout completed) is not "the other two agree"
    absent = _rollout_of(monkeypatch, _isvc("m-5", "m-5", None), [], {"m-5": _revision()})
    assert absent["converged"] is False and "revisions differ" in absent["reason"]


def _ksvc(generation, observed, image):
    """The predictor Knative Service as KServe renders it (the reconciliation witness, kube._rollout)."""
    return {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"containers": [{"image": image}]}}},
        "status": {"observedGeneration": observed},
    }


def _rollout_of(monkeypatch, isvc, pods, revisions, ksvc="derived"):
    from kubernetes.client.rest import ApiException

    kube._isvc_cache.clear()
    if ksvc == "derived":
        # a predictor Service consistent with the ISVC: reconciled at the ISVC's observed generation; its template
        # carries the desired image once the ISVC's own generation was observed, else the ready Revision's (not yet
        # propagated)
        md, st = isvc.get("metadata") or {}, isvc.get("status") or {}
        observed = st.get("observedGeneration")
        desired = kube._desired_image(isvc)
        ready = ((st.get("components") or {}).get("predictor") or {}).get("latestReadyRevision")
        rev_image = ((revisions.get(ready) or {}).get("spec") or {}).get("containers", [{}])[0].get("image")
        ksvc = _ksvc(observed, observed, desired if md.get("generation") == observed else rev_image)

    def get_obj(g, v, ns, plural, name, **kw):
        if plural == "inferenceservices":
            return isvc
        if plural == "services":
            if ksvc is None:
                raise ApiException(status=404, reason="NotFound")
            return ksvc
        if name in revisions:
            return revisions[name]
        raise ApiException(status=404, reason="NotFound")

    api = NS(get_namespaced_custom_object=get_obj)
    core = NS(list_namespaced_pod=lambda ns, label_selector="", **kw: NS(items=pods))
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": api)
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": core)
    st = kube.endpoint_status("m", "models", "eu-north1")
    return st["rollout"]


def test_require_converged_fences_an_endpoint_call_and_records_the_ready_revision(client, monkeypatch):
    c, cl = client
    import attempts
    import db

    monkeypatch.setattr(db, "enabled", lambda: False)
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    _tenant(monkeypatch)
    _bind(c)
    monkeypatch.setattr(api_module().cfg, "ROUTER_KEY", "synthetic-router")
    state = {
        "rollout": {
            "latest_created": "r-5",
            "latest_ready": "r-4",
            "latest_rolled_out": "r-4",
            "serving_images": ["img@sha256:4"],
            "converged": False,
            "reason": "revisions differ (created r-5, ready r-4, rolled out r-4)",
        }
    }
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: {"status": "deploying", "replicas_ready": 0, **copy.deepcopy(state)},
    )
    body = {
        "mode": "async",
        "input": {},
        "execution": {"io_mode": "application", "private_input_ref": "call-1", "require_converged": True},
    }
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert (
        r.status_code == 409 and "not converged" in r.json()["detail"] and "revisions differ" in r.json()["detail"]
    ), r.text
    assert not [k for k in cl.jobs if k[0] == NS_T]  # nothing admitted
    # converged at zero: admitted, the ready revision and image recorded on the call
    state["rollout"].update(
        {"latest_ready": "r-5", "latest_rolled_out": "r-5", "serving_images": [], "converged": True, "reason": None}
    )
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: {
            "status": "scaled-to-zero",
            "replicas_ready": 0,
            **copy.deepcopy(state),
        },
    )
    r = c.post("/v1/models/llm-example:invoke", json=body, headers=H)
    assert r.status_code == 202, r.text
    oid = r.json()["id"]
    ann = cl.jobs[(NS_T, oid)]["metadata"]["annotations"]
    assert ann["serverless2.nebius/endpoint-revision"] == "r-5" and "serverless2.nebius/endpoint-image" not in ann
    op = c.get(f"/v1/operations/{oid}", headers=H).json()
    assert op["endpoint_revision"] == "r-5" and op["endpoint_image"] is None
    # without the option: recorded, never refused
    state["rollout"].update({"latest_ready": "r-4", "converged": False, "serving_images": ["img@sha256:4"]})
    monkeypatch.setattr(
        kube,
        "endpoint_status",
        lambda n, ns, region="eu-north1", nodes=1: {"status": "ready", "replicas_ready": 1, **copy.deepcopy(state)},
    )
    plain = {"mode": "async", "input": {"messages": []}}
    r = c.post("/v1/models/llm-example:invoke", json=plain, headers=H)
    assert r.status_code == 202, r.text
    ann = cl.jobs[(NS_T, r.json()["id"])]["metadata"]["annotations"]
    assert (
        ann["serverless2.nebius/endpoint-revision"] == "r-4"
        and ann["serverless2.nebius/endpoint-image"] == "img@sha256:4"
    )
    # the option is an endpoint call's: a run refuses it
    r = c.post("/v1/models/hello-run:invoke", json={"input": {}, "execution": {"require_converged": True}}, headers=H)
    assert r.status_code == 400


def api_module():
    import app

    return app


def test_rollout_takes_the_predictor_service_as_the_reconciliation_witness_on_kserve(monkeypatch):
    """KServe 0.20 writes the predictor Knative Service's observedGeneration onto the InferenceService (a never
    edited ISVC shows generation 1 / observed 2 for good: the SAM2 proof, 2026-10-14), so the ISVC generations are
    no witness. The predictor Service is: converged when it reconciled its own current spec (generation ==
    observedGeneration) and that spec carries the ISVC's desired image; not reconciled while its generation leads its
    observation; not propagated while its template still carries another image. Without a predictor Service
    (RawDeployment) the ISVC's own generations decide, as before. Nothing is weakened to >= or to the image alone."""
    isvc = _isvc("m-5", "m-5", "m-5", generation=1, observed=2)
    live = _rollout_of(monkeypatch, isvc, [], {"m-5": _revision()}, ksvc=_ksvc(2, 2, "cr.example/m:5"))
    assert live["converged"] is True and live["reason"] is None
    assert (live["generation"], live["observed_generation"]) == (1, 2)
    assert (live["service_generation"], live["service_observed_generation"]) == (2, 2)
    assert live["service_image"] == "cr.example/m:5"
    # the Service's own spec moved (generation 3) and Knative has not observed it: not reconciled
    lead = _rollout_of(monkeypatch, isvc, [], {"m-5": _revision()}, ksvc=_ksvc(3, 2, "cr.example/m:5"))
    assert lead["converged"] is False and "predictor Service generation 3 not reconciled (observed 2)" in lead["reason"]
    # the ISVC asks for m:6, the Service still carries m:5: the spec was not propagated yet (never >=, never image-only)
    moved = _isvc("m-5", "m-5", "m-5", generation=2, observed=2, image="cr.example/m:6")
    behind = _rollout_of(monkeypatch, moved, [], {"m-5": _revision()}, ksvc=_ksvc(2, 2, "cr.example/m:5"))
    assert behind["converged"] is False and "spec not propagated" in behind["reason"]
    assert "not the desired cr.example/m:6" in behind["reason"]  # the ready Revision is behind as well
    # a reconciled Service carrying m:6 while the ready Revision still runs m:5: propagated, revision not yet ready
    rolling = _rollout_of(monkeypatch, moved, [], {"m-5": _revision()}, ksvc=_ksvc(3, 3, "cr.example/m:6"))
    assert rolling["converged"] is False and "spec not propagated" not in rolling["reason"]
    assert "ready revision image cr.example/m:5 is not the desired cr.example/m:6" in rolling["reason"]
    # Knative mode and the predictor Service cannot be read: no witness, never a guess (not the ISVC rule)
    unread = _rollout_of(monkeypatch, _isvc("m-5", "m-5", "m-5"), [], {"m-5": _revision()}, ksvc=None)
    assert unread["converged"] is False and "predictor Service m-predictor not readable (404)" in unread["reason"]
    # RawDeployment, proven by the ISVC's own mode: no Knative Service is expected, its generations decide as before
    raw_ok = _rollout_of(
        monkeypatch, _isvc("m-5", "m-5", "m-5", mode="RawDeployment"), [], {"m-5": _revision()}, ksvc=None
    )
    assert raw_ok["converged"] is True and raw_ok["service_generation"] is None
    raw_window = _rollout_of(
        monkeypatch,
        _isvc("m-5", "m-5", "m-5", generation=3, observed=2, mode="RawDeployment"),
        [],
        {"m-5": _revision()},
        ksvc=None,
    )
    assert raw_window["converged"] is False and "spec generation 3 not reconciled (observed 2)" in raw_window["reason"]
