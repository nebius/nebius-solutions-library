"""Only terminal, durably recorded consumers of an already-deleting work PVC may be removed."""

import asyncio
import copy
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

import attempts
import billing
import kube
from config import LABEL as L

NS, ROOT = "tenant-demo", "op-delivered"


class Cluster:
    def __init__(self):
        self.api_client = self
        self.pvc = {
            "metadata": {
                "name": ROOT + "-work",
                "namespace": NS,
                "uid": "claim-uid",
                "deletionTimestamp": "2026-10-15T09:30:00Z",
                "finalizers": ["kubernetes.io/pvc-protection"],
                "labels": {f"{L}/operation": ROOT},
            }
        }
        self.pods, self.jobs, self.records, self.deleted = [], {}, [], []
        self.read_error = self.delete_error = None
        for i, name in enumerate((ROOT, ROOT + "-x1")):
            self.jobs[name] = {
                "metadata": {
                    "name": name,
                    "namespace": NS,
                    "uid": "job-" + name,
                    "annotations": {f"{L}/pvc": ROOT + "-work", f"{L}/resumed-from": ROOT},
                },
                "spec": {"ttlSecondsAfterFinished": 7776000},
                "status": {"conditions": [{"type": "Complete", "status": "True"}]},
            }
            self.pods.append(
                {
                    "metadata": {
                        "name": name + "-pod",
                        "namespace": NS,
                        "uid": "pod-" + name,
                        "resourceVersion": str(i + 1),
                        "ownerReferences": [
                            {
                                "kind": "Job",
                                "apiVersion": "batch/v1",
                                "name": name,
                                "uid": "job-" + name,
                                "controller": True,
                            }
                        ],
                    },
                    "spec": {"volumes": [{"name": "work", "persistentVolumeClaim": {"claimName": ROOT + "-work"}}]},
                    "status": {"phase": "Succeeded"},
                }
            )
            self.records.append(
                {
                    "operation": name,
                    "root": ROOT,
                    "namespace": NS,
                    "pod": name + "-pod",
                    "pod_uid": "pod-" + name,
                    "status": "succeeded",
                    "ended_at": "2026-10-15T09:29:00Z",
                }
            )

    def sanitize_for_serialization(self, value):
        return copy.deepcopy(value)

    def list_namespaced_persistent_volume_claim(self, ns, label_selector, **kw):
        assert ns == NS and label_selector == f"{L}/operation"
        return SimpleNamespace(items=[self.pvc])

    def list_namespaced_pod(self, ns, **kw):
        assert ns == NS and "label_selector" not in kw  # no omitted unrelated readers
        if self.read_error:
            raise self.read_error
        return SimpleNamespace(items=self.pods)

    def read_namespaced_job(self, name, ns, **kw):
        if name not in self.jobs:
            raise ApiException(status=404)
        return self.jobs[name]

    def delete_namespaced_pod(self, name, ns, body, **kw):
        if self.delete_error:
            raise self.delete_error
        pod = next(p for p in self.pods if p["metadata"]["name"] == name)
        assert body.preconditions.uid == pod["metadata"]["uid"]
        assert body.preconditions.resource_version == pod["metadata"]["resourceVersion"]
        assert body.grace_period_seconds is None  # normal deletion, no force
        self.deleted.append((name, body.preconditions.uid, body.preconditions.resource_version))
        self.pods.remove(pod)


@pytest.fixture
def cluster(monkeypatch):
    fake = Cluster()
    monkeypatch.setattr(kube, "core", lambda region: fake)
    monkeypatch.setattr(kube, "batch", lambda region: fake)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: copy.deepcopy(fake.records))
    monkeypatch.setattr(billing, "retry", lambda fn, *a, **kw: fn(*a, **kw))
    return fake


def test_deleting_pvc_releases_only_exact_terminal_pods_once_preserving_jobs_and_records(cluster):
    original = copy.deepcopy((cluster.pvc, cluster.jobs, cluster.records))
    assert billing.cleanup_released_pods("north", NS) == 2
    assert billing.cleanup_released_pods("north", NS) == 0
    assert len(cluster.deleted) == 2
    assert (cluster.pvc, cluster.jobs, cluster.records) == original


def test_failed_terminal_consumer_of_an_explicitly_released_volume_is_also_safe(cluster):
    cluster.pods[0]["status"]["phase"] = "Failed"
    cluster.jobs[ROOT]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    cluster.records[0]["status"] = "failed"
    assert billing.cleanup_released_pods("north", NS) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "started"),
        ("pod_uid", "wrong-uid"),
        ("pod_uid", None),
        ("root", "different-root"),
        ("namespace", "tenant-other"),
        ("ended_at", None),
        ("operation", "unrelated"),
        ("pod", "unrelated"),
    ],
)
def test_missing_or_conflicting_durable_attempt_blocks_all_consumers(cluster, field, value):
    cluster.records[-1][field] = value
    assert billing.cleanup_released_pods("north", NS) == 0 and not cluster.deleted


def test_duplicate_attempt_receipts_are_ambiguous(cluster):
    cluster.records.append(copy.deepcopy(cluster.records[0]))
    assert billing.cleanup_released_pods("north", NS) == 0


@pytest.mark.parametrize("phase", ["Pending", "Running", "Unknown", None])
def test_any_active_or_unknown_consumer_blocks_every_deletion(cluster, phase):
    cluster.pods[-1]["status"]["phase"] = phase
    assert billing.cleanup_released_pods("north", NS) == 0 and not cluster.deleted


def test_unrelated_reader_is_not_hidden_by_a_job_label_selector(cluster):
    reader = copy.deepcopy(cluster.pods[0])
    reader["metadata"] = {"name": "diagnostic-reader", "namespace": NS, "uid": "reader", "resourceVersion": "7"}
    cluster.pods.append(reader)
    assert billing.cleanup_released_pods("north", NS) == 0


@pytest.mark.parametrize(
    "change",
    [
        "not-deleting",
        "unlabelled",
        "foreign-pvc",
        "pod-no-uid",
        "pod-no-rv",
        "pod-foreign-ns",
        "not-job",
        "not-controller",
        "job-replaced",
        "job-active",
        "job-unfinished",
        "job-missing",
        "wrong-pvc",
        "different-root",
        "no-pvc-uid",
    ],
)
def test_incomplete_or_nonterminal_provenance_keeps_the_volume_protected(cluster, change):
    pm = cluster.pods[-1]["metadata"]
    job = cluster.jobs[ROOT + "-x1"]
    if change == "not-deleting":
        cluster.pvc["metadata"].pop("deletionTimestamp")
        cluster.records[0]["status"] = "failed"  # undelivered/failed retained work is never a cleanup candidate
    elif change == "unlabelled":
        cluster.pvc["metadata"].pop("labels")
    elif change == "foreign-pvc":
        cluster.pvc["metadata"]["labels"][f"{L}/operation"] = "other-root"
    elif change == "no-pvc-uid":
        cluster.pvc["metadata"].pop("uid")
    elif change == "pod-no-uid":
        pm.pop("uid")
    elif change == "pod-no-rv":
        pm.pop("resourceVersion")
    elif change == "pod-foreign-ns":
        pm["namespace"] = "tenant-other"
    elif change == "not-job":
        pm["ownerReferences"][0]["kind"] = "ReplicaSet"
    elif change == "not-controller":
        pm["ownerReferences"][0]["controller"] = False
    elif change == "job-replaced":
        job["metadata"]["uid"] = "replacement-job"
    elif change == "job-active":
        job["status"]["active"] = 1
    elif change == "job-unfinished":
        job["status"] = {"conditions": [{"type": "SuccessCriteriaMet", "status": "True"}]}
    elif change == "job-missing":
        del cluster.jobs[ROOT + "-x1"]
    elif change == "wrong-pvc":
        job["metadata"]["annotations"][f"{L}/pvc"] = "other-work"
    else:
        job["metadata"]["annotations"][f"{L}/resumed-from"] = "other-root"
    assert billing.cleanup_released_pods("north", NS) == 0 and not cluster.deleted


def test_unavailable_database_or_pod_listing_never_deletes(cluster, monkeypatch):
    cluster.read_error = ApiException(status=503)
    assert billing.cleanup_released_pods("north", NS) == 0
    cluster.read_error = None
    monkeypatch.setattr(attempts, "enabled", lambda: False)
    assert billing.cleanup_released_pods("north", NS) == 0


@pytest.mark.parametrize("code", [404, 409, 403, 503])
def test_delete_conflict_or_failure_never_falls_back_to_force_or_finalizer_removal(cluster, code):
    cluster.delete_error = ApiException(status=code)
    assert billing.cleanup_released_pods("north", NS) == 0
    assert len(cluster.pods) == 2 and cluster.pvc["metadata"]["finalizers"] == ["kubernetes.io/pvc-protection"]


def test_existing_pass_revisits_already_billed_jobs(cluster, monkeypatch):
    monkeypatch.setattr(kube, "regions", lambda: ["north"])
    monkeypatch.setattr(
        cluster,
        "list_namespace",
        lambda **kw: SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name=NS))]),
        raising=False,
    )
    monkeypatch.setattr(cluster, "list_namespaced_job", lambda *a, **kw: SimpleNamespace(items=[]), raising=False)
    monkeypatch.setattr(billing.jobs, "list_jobsets", lambda *a: [])
    assert asyncio.run(billing.bill_once()) == 0  # no charge, only the protected terminal Pods are removed
    assert len(cluster.deleted) == 2 and len(cluster.jobs) == 2
