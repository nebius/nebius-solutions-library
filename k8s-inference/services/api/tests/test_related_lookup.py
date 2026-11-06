"""The bounded lookup of an operation's relatives (api 0.11.111, jobs.list_related): the finalizer and continuation
counters and the handle's finalizer and lineage views select the few Jobs they need from the tenant's Job list and
complete only those (pods, placement), where `list_ops` listed and completed the tenant's every operation (every pod
of every region, a Workload read per manager Job) on each owner read; the Workload list is one read per 2 s."""

import time

import jobs
import kube
from test_api import H, admin_keys, client, cluster, fake_keys  # noqa: F401

NS = "tenant-demo"


def _submit(c):
    r = c.post("/v1/models/hello-run:invoke", headers=H, json={"input": {}})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_list_related_selects_and_completes_only_the_relatives(client, admin_keys, fake_keys, monkeypatch):
    c, cl = client
    a, b = _submit(c), _submit(c)
    cl.jobs[(NS, b)]["metadata"]["annotations"]["serverless2.nebius/resumed-from"] = a
    pods_listed: list[str] = []
    real = cl.list_namespaced_pod

    def listing(ns, label_selector=""):
        pods_listed.append(label_selector)
        return real(ns, label_selector=label_selector)

    monkeypatch.setattr(cl, "list_namespaced_pod", listing)
    names = jobs.list_related(NS, "demo", lambda j: j["metadata"]["name"] == b, complete=False)
    assert [j["metadata"]["name"] for j in names] == [b] and pods_listed == []  # selected, not completed
    full = jobs.list_related(NS, "demo", lambda j: j["metadata"]["name"] == b)
    assert [j["metadata"]["name"] for j in full] == [b] and full[0]["_pods"] == [] and len(pods_listed) == 1
    assert jobs.continuations(NS, "demo", a) == 1 and jobs.continuations(NS, "demo", a, recovered=True) == 0
    assert jobs.continuations(NS, "demo", b) == 0 and jobs.export_retries(NS, "demo", a) == 0
    cl.jobs[(NS, b)]["metadata"]["annotations"]["serverless2.nebius/recovered-from"] = a
    assert jobs.continuations(NS, "demo", a, recovered=True) == 1
    # a finalizer of a: counted by its name, whatever its own annotations
    cl.jobs[(NS, f"{a}-x1")] = {
        **cl.jobs[(NS, b)],
        "metadata": {**cl.jobs[(NS, b)]["metadata"], "name": f"{a}-x1", "annotations": {}},
    }
    assert jobs.export_retries(NS, "demo", a) == 1 and jobs.continuations(NS, "demo", a) == 1
    # the tenant's listing of operations is untouched (it still completes everything it lists)
    assert {j["metadata"]["name"] for j in jobs.list_ops(NS, "demo", None, None)} == {a, b, f"{a}-x1"}


def test_the_workload_list_is_one_read_per_window(client, admin_keys, fake_keys, monkeypatch):
    c, cl = client
    monkeypatch.setattr(kube, "WORKLOADS_CACHE_S", 60.0)
    monkeypatch.setattr(kube, "_workloads_cache", {})
    reads: list[str] = []
    real = cl.list_namespaced_custom_object

    def listing(group, version, ns, plural, label_selector=""):
        reads.append(plural)
        return real(group, version, ns, plural, label_selector=label_selector)

    monkeypatch.setattr(cl, "list_namespaced_custom_object", listing)
    uid = "11111111-2222-3333-4444-555555555555"
    cl.custom[(NS, "workloads", "job-x")] = {
        "metadata": {"name": "job-x", "labels": {kube.KUEUE_JOB_UID_LABEL: uid}},
        "status": {"clusterName": "south", "conditions": []},
    }
    assert kube.workload_clusters(NS) == {uid: "south"} and kube.workload_reasons(NS) == {}
    assert reads == ["workloads"]  # the second consumer read the same list

    def placed(cluster):  # the API server answers a fresh object on every read: a new one here too
        cl.custom[(NS, "workloads", "job-x")] = {
            "metadata": {"name": "job-x", "labels": {kube.KUEUE_JOB_UID_LABEL: uid}},
            "status": {"clusterName": cluster, "conditions": []},
        }

    placed("north")
    assert kube.workload_clusters(NS) == {uid: "south"}  # within the window: the view of the read it serves
    kube._workloads_cache[NS] = (time.time() - 1, kube._workloads_cache[NS][1])  # the window passed
    assert kube.workload_clusters(NS) == {uid: "north"} and reads == ["workloads", "workloads"]
    monkeypatch.setattr(kube, "WORKLOADS_CACHE_S", 0.0)
    placed("hub")
    assert kube.workload_clusters(NS) == {uid: "hub"}  # off: every read is a read
