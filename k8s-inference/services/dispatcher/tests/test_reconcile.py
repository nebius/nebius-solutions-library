"""The reconcile entrypoint end to end with mocked Kubernetes clients: fleet document, worker clients, nomination,
the volume pass and the sweeper in one call (a helper-only suite let a missing `load_fleet` through, 2026-10-12)."""

import base64
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import yaml
from kubernetes import client as k8s

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import dispatcher as d  # noqa: E402

POOLS = {
    "hub-h100-spot": {
        "region": "hub",
        "pool": "h100-spot",
        "gpu_class": "h100",
        "capacity": "spot",
        "usd_per_gpu_hour": 2.15,
    }
}
T = "tenant-t"


def api_exc(status):
    return k8s.exceptions.ApiException(status=status, reason="mocked")


def template(claim):
    return {
        "metadata": {"annotations": {d.PVC_SIZE_ANN: "20"}},
        "spec": {
            "containers": [{"name": "main", "resources": {"requests": {"nvidia.com/gpu": "1"}}}],
            "volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": claim}}],
        },
    }


def workload(job, cluster=None, pending=False, finished=False):
    st = {"conditions": [{"type": "QuotaReserved", "status": "True"}]}
    if pending:
        st["admissionChecks"] = [{"name": "multikueue", "state": "Pending"}]
    if cluster:
        st["clusterName"] = cluster
        st["conditions"].append({"type": "Admitted", "status": "True"})
    if finished:
        st["conditions"].append({"type": "Finished", "status": "True"})
    return {
        "metadata": {
            "namespace": T,
            "name": f"job-{job}",
            "ownerReferences": [{"kind": "Job", "name": job, "uid": f"uid-{job}"}],
            "annotations": {},
        },
        "spec": {"queueName": "default", "podSets": [{"count": 1, "template": template(f"{job}-work")}]},
        "status": st,
    }


def mjob(name, claim, successor=None):
    ann = {d.SUCCESSOR_ANN: successor} if successor else {}
    return NS(
        metadata=NS(name=name, uid=f"uid-{name}", annotations=ann, deletion_timestamp=None),
        spec=NS(template=NS(spec=NS(volumes=[NS(name="work", persistent_volume_claim=NS(claim_name=claim))]))),
        status=NS(conditions=[]),
    )


def pvc(name, jobs, age_s=10):
    return NS(
        metadata=NS(
            namespace=T,
            name=name,
            annotations={d.PVC_JOBS_ANN: jobs},
            labels={d.MANAGED_LABEL: "dispatcher"},
            creation_timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_s),
        )
    )


class ControlCore:
    def read_namespaced_config_map(self, name, ns):
        assert ns == d.NS
        if name == d.FLEET_CM:
            return NS(data={"pools.yaml": yaml.safe_dump(POOLS)})
        raise api_exc(404)  # no spot price feed yet

    def read_namespaced_secret(self, name, ns):
        assert (name, ns) == ("hub-kubeconfig", d.NS)
        return NS(data={"kubeconfig": base64.b64encode(b"apiVersion: v1\n").decode()})


class ControlCustom:
    def __init__(self, workloads):
        self.workloads, self.patches, self.nominated = workloads, [], []

    def list_cluster_custom_object(self, group, version, plural):
        assert (group, version) == (d.GROUP, d.VERSION)
        if plural == "multikueueclusters":
            return {
                "items": [
                    {
                        "metadata": {"name": "hub", "labels": {d.REGION_LABEL: "eu-north1"}},
                        "spec": {"kubeConfig": {"locationType": "Secret", "location": "hub-kubeconfig"}},
                    }
                ]
            }
        assert plural == "workloads"
        return {"items": self.workloads}

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        assert plural == "localqueues"
        return {"spec": {"clusterQueue": "default"}}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.patches.append((name, body))

    def patch_namespaced_custom_object_status(self, group, version, ns, plural, name, body, **kw):
        assert kw["field_manager"] == "kueue-admission"
        self.nominated.append((name, body["status"]["nominatedClusterNames"]))


class ControlBatch:
    def __init__(self, jobs):
        self.jobs, self.lists, self.reads = jobs, 0, []

    def list_namespaced_job(self, ns):
        self.lists += 1
        if self.jobs is None:
            raise RuntimeError("manager unreachable")
        return NS(items=[j for j in self.jobs.values()])

    def read_namespaced_job(self, name, ns):
        self.reads.append((name, ns))
        if self.jobs is None:
            raise RuntimeError("manager unreachable")
        if name not in self.jobs:
            raise api_exc(404)
        return self.jobs[name]


class WorkerCustom:
    def list_cluster_custom_object(self, group, version, plural):
        assert plural == "clusterqueues"
        return {
            "items": [
                {
                    "spec": {
                        "resourceGroups": [
                            {
                                "flavors": [
                                    {"name": "h100-spot", "resources": [{"name": "nvidia.com/gpu", "nominalQuota": 8}]}
                                ]
                            }
                        ]
                    },
                    "status": {"flavorsUsage": []},
                }
            ]
        }

    def get_namespaced_custom_object(self, *a):
        raise api_exc(404)


class WorkerCore:
    def __init__(self, pvcs):
        self.pvcs, self.created, self.deleted, self.patched = {p.metadata.name: p for p in pvcs}, [], [], []

    def list_persistent_volume_claim_for_all_namespaces(self, label_selector=None):
        assert label_selector == f"{d.MANAGED_LABEL}=dispatcher"
        return NS(items=list(self.pvcs.values()))

    def read_namespaced_persistent_volume_claim(self, name, ns):
        if name in self.pvcs:
            return self.pvcs[name]
        raise api_exc(404)

    def create_namespaced_persistent_volume_claim(self, ns, body):
        self.created.append(body["metadata"]["name"])
        self.pvcs[body["metadata"]["name"]] = pvc(
            body["metadata"]["name"], body["metadata"]["annotations"][d.PVC_JOBS_ANN], age_s=0
        )

    def patch_namespaced_persistent_volume_claim(self, name, ns, body):
        self.patched.append((name, body["metadata"]["annotations"][d.PVC_JOBS_ANN]))

    def delete_namespaced_persistent_volume_claim(self, name, ns):
        self.deleted.append(name)
        del self.pvcs[name]


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    """One worker `hub` behind the MultiKueueCluster's kubeconfig Secret; the worker's clients are the fakes."""
    wcore, wcustom = WorkerCore([pvc("op-1-work", "op-1"), pvc("op-3-work", "op-3", age_s=10**7)]), WorkerCustom()
    monkeypatch.setattr(d, "INFINIBAND", False)
    monkeypatch.setattr(
        d, "Timed", lambda api, timeout_s=0: api
    )  # the fakes take no _request_timeout; Timed has its own test
    monkeypatch.setattr(d.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(d.config, "new_client_from_config", lambda path: "hub-api")
    monkeypatch.setattr(d.client, "CoreV1Api", lambda api=None: wcore)
    monkeypatch.setattr(d.client, "CustomObjectsApi", lambda api=None: wcustom)
    monkeypatch.setattr(d.client, "BatchV1Api", lambda api=None: NS())
    return wcore


def test_reconcile_nominates_creates_and_sweeps_through_the_entrypoint(fleet):
    """One pass over four runs: a queued run is nominated to the only worker and gets no volume yet; the admitted
    successor of a recovery gets its fresh volume; the superseded original's volume is swept and never recreated
    (the observed defect); a FAILED run's volume is kept at any age; manager Jobs are listed once."""
    wls = [
        workload("op-1", cluster="hub"),  # superseded original: deactivated, never Finished
        workload("op-1-r-ab12cd34", cluster="hub"),  # its successor, running
        workload("op-2", pending=True),  # queued: waiting for a nomination
        workload("op-3", cluster="hub", finished=True),
    ]  # FAILED, resumable on its volume
    jobs = {
        "op-1": mjob("op-1", "op-1-work", successor="op-1-r-ab12cd34"),
        "op-1-r-ab12cd34": mjob("op-1-r-ab12cd34", "op-1-r-ab12cd34-work"),
        "op-2": mjob("op-2", "op-2-work"),
        "op-3": mjob("op-3", "op-3-work"),
    }
    custom, batch = ControlCustom(wls), ControlBatch(jobs)
    d.reconcile(ControlCore(), custom, batch)
    assert custom.nominated == [("job-op-2", ["hub"])]
    assert fleet.created == ["op-1-r-ab12cd34-work"]
    assert fleet.deleted == ["op-1-work"]
    assert set(fleet.pvcs) == {"op-3-work", "op-1-r-ab12cd34-work"}
    assert batch.lists == 1
    recorded = {
        n: b["metadata"]["annotations"][d.ADMITTED_ANN]
        for n, b in custom.patches
        if d.ADMITTED_ANN in b["metadata"].get("annotations", {})
    }
    assert recorded == {"job-op-1": "hub", "job-op-1-r-ab12cd34": "hub", "job-op-3": "hub"}
    # the next passes: nothing comes back, nothing else goes
    for _ in range(2):
        d.reconcile(ControlCore(), custom, ControlBatch(jobs))
    assert fleet.created == ["op-1-r-ab12cd34-work"] and fleet.deleted == ["op-1-work"]
    # the successor finishes and its handle is gone (uploader released the volume, TTL took the Job): the chain is closed
    del jobs["op-1-r-ab12cd34"]
    wls[1]["status"]["conditions"].append({"type": "Finished", "status": "True"})
    fleet.deleted.clear()
    d.reconcile(ControlCore(), custom, ControlBatch(jobs))
    assert fleet.deleted == []  # nothing references it, but the orphan rule waits for the grace period
    fleet.pvcs["op-1-r-ab12cd34-work"].metadata.creation_timestamp -= timedelta(seconds=d.ORPHAN_AFTER_S + 1)
    d.reconcile(ControlCore(), custom, ControlBatch(jobs))
    assert fleet.deleted == ["op-1-r-ab12cd34-work"]
    assert "op-3-work" in fleet.pvcs


def test_reconcile_preserves_everything_when_the_manager_cannot_be_listed(fleet):
    """Unknown manager state neither creates/adopts claims nor removes retained volumes."""
    wls = [workload("op-1", cluster="hub"), workload("op-3", cluster="hub", finished=True)]
    custom = ControlCustom(wls)
    d.reconcile(ControlCore(), custom, ControlBatch(None))
    assert fleet.deleted == [] and fleet.created == [] and set(fleet.pvcs) == {"op-1-work", "op-3-work"}
    assert custom.nominated == []


@pytest.mark.parametrize("condition", ["Complete", "Failed"])
@pytest.mark.parametrize("retained", [False, True])
def test_terminal_manager_fences_creation_before_workload_finished(fleet, condition, retained):
    """C1 argon race: the manager is terminal but its Workload still says Admitted. A released claim stays gone;
    an existing failed/export-needed claim is retained, even beyond the orphan grace period."""
    fleet.pvcs.clear()
    if retained:
        fleet.pvcs["op-done-work"] = pvc("op-done-work", "op-done", age_s=10**7)
    job = mjob("op-done", "op-done-work")
    job.status = k8s.V1JobStatus(
        conditions=[k8s.V1JobCondition(type=condition, status="True")],
        completion_time=datetime.now(timezone.utc),
    )
    wl = workload("op-done", cluster="hub")
    assert not d.finished(wl)
    for _ in range(3):
        d.reconcile(ControlCore(), ControlCustom([wl]), ControlBatch({"op-done": job}))
    assert fleet.created == fleet.patched == fleet.deleted == []
    assert set(fleet.pvcs) == ({"op-done-work"} if retained else set())


@pytest.mark.parametrize("jobs", [None, {}])
def test_missing_or_unreadable_manager_defers_new_claim_until_known(fleet, jobs):
    fleet.pvcs.clear()
    wl = workload("op-wait", cluster="hub")
    d.reconcile(ControlCore(), ControlCustom([wl]), ControlBatch(jobs))
    assert fleet.created == fleet.patched == fleet.deleted == []
    known = ControlBatch({"op-wait": mjob("op-wait", "op-wait-work")})
    d.reconcile(ControlCore(), ControlCustom([wl]), known)
    assert fleet.created == ["op-wait-work"] and known.lists == 1


@pytest.mark.parametrize("suspended", [False, True])
def test_running_or_admitted_suspended_manager_still_gets_claim(fleet, suspended):
    fleet.pvcs.clear()
    job = mjob("op-run", "op-run-work")
    job.spec.suspend = suspended  # Kueue can leave the manager suspended while its worker runs.
    job.status = k8s.V1JobStatus(
        active=0,
        failed=1,
        succeeded=1,
        conditions=[
            k8s.V1JobCondition(type="Complete", status="Unknown"),
            k8s.V1JobCondition(type="Failed", status="False"),
            k8s.V1JobCondition(type="SuccessCriteriaMet", status="True"),
        ],
    )
    d.reconcile(ControlCore(), ControlCustom([workload("op-run", cluster="hub")]), ControlBatch({"op-run": job}))
    assert fleet.created == ["op-run-work"] and fleet.deleted == []


@pytest.mark.parametrize("retained", [False, True])
def test_terminal_predecessor_does_not_block_its_live_same_claim_finalizer(fleet, retained):
    fleet.pvcs.clear()
    if retained:
        fleet.pvcs["op-done-work"] = pvc("op-done-work", "op-done", age_s=10**7)
    old = mjob("op-done", "op-done-work")
    old.status.conditions = [k8s.V1JobCondition(type="Failed", status="True")]
    finalizer = mjob("op-done-x1", "op-done-work")
    next_wl = workload("op-done-x1", cluster="hub")
    next_wl["spec"]["podSets"][0]["template"] = template("op-done-work")
    d.reconcile(
        ControlCore(),
        ControlCustom([workload("op-done", cluster="hub"), next_wl]),
        ControlBatch({"op-done": old, "op-done-x1": finalizer}),
    )
    assert fleet.deleted == []
    assert fleet.created == ([] if retained else ["op-done-work"])
    assert fleet.patched == ([("op-done-work", "op-done,op-done-x1")] if retained else [])


@pytest.mark.parametrize("changed", ["uid", "deleting", "jobset", "ambiguous"])
def test_unknown_or_replaced_manager_owner_cannot_create_claim(fleet, changed):
    fleet.pvcs.clear()
    job = mjob("op-run", "op-run-work")
    wl = workload("op-run", cluster="hub")
    if changed == "uid":
        job.metadata.uid = "replacement-job-uid"
    elif changed == "deleting":
        job.metadata.deletion_timestamp = datetime.now(timezone.utc)
    elif changed == "jobset":
        wl["metadata"]["ownerReferences"][0]["kind"] = "JobSet"
    else:
        wl["metadata"]["ownerReferences"].append({"kind": "Job", "name": "another"})
    d.reconcile(ControlCore(), ControlCustom([wl]), ControlBatch({"op-run": job}))
    assert fleet.created == fleet.patched == fleet.deleted == []


@pytest.mark.parametrize("changed", ["Complete", "Failed", "superseded", "uid", "missing", "unreachable"])
def test_missing_claim_rechecks_manager_after_cached_list(fleet, changed):
    fleet.pvcs.clear()
    initial = mjob("op-run", "op-run-work")

    class ChangedManager(ControlBatch):
        def read_namespaced_job(self, name, ns):
            self.reads.append((name, ns))
            if changed == "missing":
                raise api_exc(404)
            if changed == "unreachable":
                raise RuntimeError("manager unreachable")
            current = mjob(name, "op-run-work", successor="op-next" if changed == "superseded" else None)
            if changed == "uid":
                current.metadata.uid = "replacement"
            else:
                current.status.conditions = [k8s.V1JobCondition(type=changed, status="True")]
            return current

    batch = ChangedManager({"op-run": initial})
    d.reconcile(ControlCore(), ControlCustom([workload("op-run", cluster="hub")]), batch)
    assert batch.lists == 1 and batch.reads == [("op-run", T)]
    assert fleet.created == fleet.patched == fleet.deleted == []


def test_existing_claim_does_not_add_per_workload_manager_reads(fleet):
    fleet.pvcs.clear()
    fleet.pvcs["op-run-work"] = pvc("op-run-work", "op-run")
    batch = ControlBatch({"op-run": mjob("op-run", "op-run-work")})
    d.reconcile(ControlCore(), ControlCustom([workload("op-run", cluster="hub")]), batch)
    assert batch.lists == 1 and batch.reads == []
    assert fleet.created == fleet.patched == fleet.deleted == []


def test_timed_client_adds_a_request_timeout_to_every_call():
    """Every Kubernetes call of the dispatcher carries _request_timeout (one hung worker API server must not stall
    the single reconcile thread); an explicit timeout wins; attributes pass through."""
    calls = []

    class Api:
        version = "v1"

        def list_namespaced_job(self, ns, **kw):
            calls.append((ns, kw))
            return NS(items=[])

    t = d.Timed(Api(), timeout_s=7)
    t.list_namespaced_job("t")
    t.list_namespaced_job("u", _request_timeout=3, label_selector="x")
    assert calls == [("t", {"_request_timeout": 7}), ("u", {"_request_timeout": 3, "label_selector": "x"})]
    assert t.version == "v1" and d.REQUEST_TIMEOUT_S > 0
