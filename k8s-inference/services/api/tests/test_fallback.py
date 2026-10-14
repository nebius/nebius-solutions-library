"""Portable retries and placement-time image selection (docs/SCHEDULING.md "Fallback to the next class", docs/JOBS.md
"Execution options"): a queued run moves to the next class of its preference after the wait bound, a resume with a
checkpoint reference needs no volume and may change class (and image), a resume re-renders the pinned model revision,
and a queued operation says why it waits."""

import json

import pytest

import app as appmod
import attempts
import catalog
import db
import execution
import jobs
import kube
import models as models_mod
import placement as _placement
import reconciliation
from config import LABEL
from test_api import FLEET, KEY_INFO, H, admin_keys, client, cluster, fake_keys, pod  # noqa: F401

_real_choose_class = _placement.choose_class  # captured before the autouse stub replaces it
_real_cluster_region = kube.cluster_region  # same for the cluster id map

CFG = {"identities": {}, "execution_profiles": {}}
PER_CLASS = {
    "id": "fold",
    "kind": "job",
    "image": "img:default",
    "images": {"rtx-pro-6000": "img:rtx", "l40s": "img:l40s"},
    "command": "fold",
    "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]},
}
TWO_CLASS_FLEET = {
    "pools": {
        "us-rtx": {
            "region": "us-central1",
            "pool": "rtx6000-spot-1x",
            "gpu_class": "rtx-pro-6000",
            "usd_per_gpu_hour": 1.09,
        },
        "hub-l40s": {"region": "hub", "pool": "l40s-spot-1x", "gpu_class": "l40s", "usd_per_gpu_hour": 0.85},
    }
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(execution, "tenant_config", lambda ns, region="eu-north1": CFG)
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)
    monkeypatch.setattr(kube, "regions", lambda: ["eu-north1", "us-central1"])
    monkeypatch.setattr(kube, "workload_reasons", lambda ns: {})
    monkeypatch.setattr(appmod, "FLEET_MANAGER", True)
    monkeypatch.setattr(reconciliation, "FLEET_MANAGER", True)
    monkeypatch.setattr(
        jobs.placing if hasattr(jobs, "placing") else appmod.placing,
        "choose_class",
        lambda m, region, gpus, available=None, tenant=None, regions=None: (
            available[0] if available else "rtx-pro-6000",
            "test",
        ),
    )


@pytest.fixture(autouse=True)
def _fresh_catalog():
    """The file catalog is cached by mtime: entries normalised under this module's patched fleet must not leak."""
    yield
    catalog._cache.update({"mtime": None, "models": {}})


def _fold_model():
    import models

    e = models.to_entry(PER_CLASS, managed_by="api")
    return catalog.normalise({**e, "version": 3})


def test_fallback_moves_a_queued_run_to_the_next_class(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)  # after the client fixture's own fleet
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post(
        "/v1/models/fold:invoke",
        json={
            "input": {},
            "execution": {"placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "fallback_wait_s": 60}},
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[("tenant-demo", op)]
    assert (
        job["metadata"]["labels"][f"{LABEL}/gpu-class"] == "rtx-pro-6000"
        and job["metadata"]["annotations"][f"{LABEL}/fallback-wait-s"] == "60"
    )
    assert job["metadata"]["annotations"][f"{LABEL}/model-version"] == "3"
    assert next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")["image"].endswith(
        "img:rtx"
    )
    # not yet: the wait bound has not passed
    from datetime import datetime, timezone

    job["metadata"]["creationTimestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert appmod.fallback_switch() == []
    job["metadata"]["creationTimestamp"] = "2026-10-05T20:00:00Z"
    moved = appmod.fallback_switch()
    assert moved == [f"{op}-r1"]
    # the original handle stays readable, names its successor and never admits again
    orig = fake.jobs[("tenant-demo", op)]
    assert orig["metadata"]["annotations"][f"{LABEL}/successor"] == f"{op}-r1" and orig["spec"]["suspend"] is True
    via = c.get(f"/v1/operations/{op}", headers=H).json()
    assert via["id"] == f"{op}-r1" and via["requested_id"] == op and via["resumed_from"] == op
    assert jobs.normalise(orig)["status"] == "SUPERSEDED" and jobs.normalise(orig)["successor"] == f"{op}-r1"
    assert appmod.fallback_switch() == []  # a superseded original is never moved again
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    assert (
        new["metadata"]["labels"][f"{LABEL}/gpu-class"] == "l40s"
        and new["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == "prefer-l40s"
    )
    assert next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")["image"].endswith(
        "img:l40s"
    )
    assert new["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/gpu-classes"] == "l40s"
    assert json.loads(new["metadata"]["annotations"][f"{LABEL}/execution"])["placement"]["gpu_preference"] == ["l40s"]
    assert "rtx-pro-6000 -> l40s" in new["metadata"]["annotations"][f"{LABEL}/fallback"]
    got = c.get(f"/v1/operations/{op}-r1", headers=H).json()
    assert got["resumed_from"] == op and "rtx-pro-6000 -> l40s" in got["reason"]
    # the last class of the preference keeps waiting
    new["metadata"]["creationTimestamp"] = "2026-10-05T20:00:00Z"
    assert appmod.fallback_switch() == []


def test_cluster_ids_map_to_regions_through_the_fleet_document(client, monkeypatch):
    """Custom cluster ids (tfvars `regions.<r>.id`, e.g. north/south) resolve to their Nebius region through the
    fleet document's clusters.yaml: kubeconfig choice, logs/metrics clients and admitted cancellation of a run that a
    worker named `south` holds all depend on it (0.11.25 knew only the `hub` alias)."""
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {},
            "clusters": {
                "north": {"region": "eu-north1", "project": "p1"},
                "south": {"region": "eu-south1", "project": "p2"},
                "control": {"region": "eu-north1", "project": "p1"},
            },
        },
    )
    monkeypatch.setattr(kube, "cluster_region", _real_cluster_region)  # the module's autouse stub knows only hub
    assert (
        kube.cluster_region("north") == "eu-north1"
        and kube.cluster_region("south") == "eu-south1"
        and kube.cluster_region("control") == "eu-north1"
    )
    assert kube.cluster_region("eu-west2") == "eu-west2"  # an id that is its own region
    monkeypatch.setattr(kube, "fleet", lambda: {"pools": {}})  # a fleet document without the map: the legacy alias
    assert kube.cluster_region("hub") == kube.HUB_REGION and kube.cluster_region("south") == "south"
    # a run admitted by `south` is read, cancelled and logged through the eu-south1 clients
    c, fake = client
    monkeypatch.setattr(
        kube, "fleet", lambda: {"pools": {}, "clusters": {"south": {"region": "eu-south1", "project": "p2"}}}
    )
    seen = []
    orig_batch, orig_core = kube.batch, kube.core
    monkeypatch.setattr(kube, "batch", lambda region="eu-north1": (seen.append(region), orig_batch(region))[1])
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": (seen.append(region), orig_core(region))[1])
    r = c.post("/v1/models/hello-run:invoke", json={"input": {"message": "x"}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    # a private input for a run whose model names clusters by id: the copies go to the mapped regions' clients
    # (the new fleet refused `region south is not served` before any GPU job was admitted)
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {},
            "clusters": {
                "north": {"region": "eu-north1", "project": "p1"},
                "south": {"region": "eu-south1", "project": "p2"},
            },
        },
    )
    monkeypatch.setattr(
        execution, "tenant_config", lambda ns, region="eu-north1": {"identities": {}, "execution_profiles": {}}
    )
    ided = catalog.normalise(
        {
            **models_mod.to_entry(
                {
                    "id": "byid",
                    "kind": "job",
                    "image": "img",
                    "command": "x",
                    "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]},
                },
                managed_by="api",
            ),
            "version": 1,
        }
    )
    ided["regions"] = ["south", "north"]
    monkeypatch.setattr(catalog, "get", lambda name: ided if name == "byid" else None)
    assert c.put("/v1/bindings/by-id-input", json={"data": {"manifest.json": "{}"}}, headers=H).status_code == 200
    seen.clear()
    r = c.post(
        "/v1/models/byid:invoke",
        json={
            "input": {},
            "execution": {
                "io_mode": "application",
                "private_input_ref": "by-id-input",
                "placement": {"allowed_clusters": ["south"]},
            },
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    assert "eu-south1" in seen and "south" not in seen and ("tenant-demo", f"{r.json()['id']}-private") in fake.secrets
    assert (
        fake.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/regions"]
        == "south"
    )  # the dispatcher still gets the id
    uid = fake.jobs[("tenant-demo", op)]["metadata"].get("uid", "")
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {uid: "south"})
    seen.clear()
    got = c.get(f"/v1/operations/{op}", headers=H)
    assert got.status_code == 200 and "eu-south1" in seen and "south" not in seen
    # the authenticated record is stamped with the canonical cluster id (the Workload's admitting cluster), the region stays
    # the Nebius region: the application keys identities and allowed_clusters by the configured id (new fleet, 2026-10-12)
    acked = {}
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(attempts, "mark_cancelled", lambda operation: None)
    monkeypatch.setattr(attempts, "verify_runner_token", lambda token: ("eu-south1", "tenant-demo", f"{op}-pq52n"))
    monkeypatch.setattr(
        attempts,
        "record",
        lambda region, ns, pod_, body: acked.update({**body, "region": region}) or {**body, "region": region},
    )
    ack = c.post(
        "/internal/attempts",
        json={"operation": op, "status": "started", "gpus": 1},
        headers={"Authorization": "Bearer t"},
    )
    assert (
        ack.status_code == 200
        and ack.json()["cluster"] == "south"
        and ack.json()["region"] == "eu-south1"
        and acked["cluster"] == "south"
    )
    rows = [
        {
            "operation": op,
            "pod": f"{op}-pq52n",
            "root": op,
            "tenant": "demo",
            "status": "started",
            "epoch": 1,
            "revision": 2,
            "gpus": 1,
            "region": "eu-south1",
            "cluster": "south",
            "started_at": "2026-10-12T19:53:52Z",
        }
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows)
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["cluster"] == "south" and got["region"] == "eu-south1"
    rows[0].pop("cluster")  # a row written before the column: the region maps to the worker's id
    assert c.get(f"/v1/operations/{op}", headers=H).json()["cluster"] == "south"
    # the inverse map: an id equal to the region wins, control entries never count, the hub alias stays without a map
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {},
            "clusters": {
                "hub": {"region": "eu-north1"},
                "control": {"region": "eu-north1", "control": True},
                "eu-west2": {"region": "eu-west2"},
            },
        },
    )
    assert (
        kube.cluster_id("eu-north1") == "hub"
        and kube.cluster_id("eu-west2") == "eu-west2"
        and kube.cluster_id(None) is None
    )
    monkeypatch.setattr(kube, "fleet", lambda: {"pools": {}})
    assert kube.cluster_id("eu-south1") == "eu-south1"
    # two worker clusters in one region: the map is ambiguous, a record without an admitting cluster is refused, a status shows unknown
    monkeypatch.setattr(
        kube,
        "fleet",
        lambda: {
            "pools": {},
            "clusters": {
                "a": {"region": "eu-x"},
                "b": {"region": "eu-x"},
                "control": {"region": "eu-x", "control": True},
            },
        },
    )
    with pytest.raises(LookupError):
        kube.cluster_id("eu-x")
    assert kube.cluster_id_or_none("eu-x") is None
    monkeypatch.setattr(attempts, "verify_runner_token", lambda token: ("eu-x", "tenant-demo", "op-gone-abc"))
    assert (
        c.post(
            "/internal/attempts",
            json={"operation": "op-gone", "status": "started", "gpus": 1},
            headers={"Authorization": "Bearer t"},
        ).status_code
        == 409
    )
    assert c.post(f"/v1/operations/{op}:cancel", headers=H).status_code == 200


def test_allowed_clusters_place_a_fleet_run_without_a_top_level_region(client, monkeypatch):
    """A fleet run names where it may land through execution.placement.allowed_clusters; the API's implicit default
    region (the first deployment) is no request and never contradicts it. The class is ranked among the allowed
    clusters only; an explicit region outside the list is the one error; nothing widens the list."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()  # regions eu-north1 (hub) and us-central1
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    import placement as placing

    # the module's autouse stub ranks nothing: this test needs the real ranking
    monkeypatch.setattr(
        placing,
        "choose_class",
        placing.choose_class.__wrapped__ if hasattr(placing.choose_class, "__wrapped__") else _real_choose_class,
    )
    ex = {"placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "allowed_clusters": ["us-central1"]}}
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "execution": ex}, headers=H)  # no region
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    assert job["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/regions"] == "us-central1"  # no widening
    assert job["metadata"]["labels"][f"{LABEL}/gpu-class"] == "rtx-pro-6000"  # ranked within us-central1 only
    assert json.loads(job["metadata"]["annotations"][f"{LABEL}/execution"])["placement"]["allowed_clusters"] == [
        "us-central1"
    ]
    r = c.post(
        "/v1/models/fold:invoke", json={"input": {}, "region": "us-central1", "execution": ex}, headers=H
    )  # explicit, consistent
    assert r.status_code == 202, r.text
    r = c.post(
        "/v1/models/fold:invoke", json={"input": {}, "region": "eu-north1", "execution": ex}, headers=H
    )  # explicit contradiction
    assert r.status_code == 400 and "not in allowed_clusters" in r.text
    both = {"placement": {"allowed_clusters": ["us-central1", "eu-north1"]}}
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "execution": both}, headers=H)
    assert r.status_code == 202 and set(
        fake.jobs[("tenant-demo", r.json()["id"])]["spec"]["template"]["metadata"]["annotations"][
            f"{LABEL}/regions"
        ].split(",")
    ) == {"us-central1", "eu-north1"}
    # a class without per-class images: the preference alone must pick the queue and the class list (every probe used
    # to land on the model's first class)
    plain = catalog.normalise(
        {
            **models_mod.to_entry(
                {
                    "id": "probe",
                    "kind": "job",
                    "image": "img:cuda",
                    "command": "nvidia-smi",
                    "gpu": {"count": 1, "classes": ["rtx-pro-6000", "l40s"]},
                },
                managed_by="api",
            ),
            "version": 1,
        }
    )
    monkeypatch.setattr(catalog, "get", lambda name: plain if name == "probe" else (m if name == "fold" else None))
    for pref, queue in (
        (["l40s"], "prefer-l40s"),
        (["rtx-pro-6000"], "prefer-rtx-pro-6000"),
        (None, "prefer-rtx-pro-6000"),
    ):
        body = (
            {
                "input": {},
                "execution": {"placement": {"gpu_preference": pref, "allowed_clusters": ["eu-north1", "us-central1"]}},
            }
            if pref
            else {"input": {}}
        )
        r = c.post("/v1/models/probe:invoke", json=body, headers=H)
        assert r.status_code == 202, r.text
        j = fake.jobs[("tenant-demo", r.json()["id"])]
        assert j["metadata"]["labels"]["kueue.x-k8s.io/queue-name"] == queue, (pref, j["metadata"]["labels"])
        assert j["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/gpu-classes"] == (
            ",".join(pref) if pref else "rtx-pro-6000,l40s"
        )
        terms = j["spec"]["template"]["spec"]["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"][0]["matchExpressions"]
        pools = next(t["values"] for t in terms if t["key"] == f"{LABEL}/pool")
        assert set(pools) == (
            {"l40s": {"l40s-spot-1x"}, "rtx-pro-6000": {"rtx6000-spot-1x"}}[pref[0]]
            if pref
            else {"l40s-spot-1x", "rtx6000-spot-1x"}
        )  # only the asked class's pools
        assert (
            f"{LABEL}/gpu-class" not in j["metadata"]["labels"]
        )  # no class pin for a single-image class (Kueue may still move within the list)
    # single-cluster mode (no fleet manager): runs happen here, so a list that excludes this region is an error
    monkeypatch.setattr(appmod, "FLEET_MANAGER", False)
    monkeypatch.setattr(reconciliation, "FLEET_MANAGER", False)
    r = c.post("/v1/models/fold:invoke", json={"input": {}, "execution": ex}, headers=H)
    assert r.status_code == 400 and "runs jobs in eu-north1 only" in r.text
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"placement": {"allowed_clusters": ["eu-north1"]}}},
        headers=H,
    )
    assert r.status_code == 202, r.text


@pytest.mark.parametrize("binding_available", [True, False])
def test_fallback_delivers_private_input_before_creating_successor(client, monkeypatch, binding_available):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    monkeypatch.setattr(catalog, "get", lambda name: _fold_model() if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    assert (
        c.put("/v1/bindings/private-fallback", json={"data": {"input.json": "private"}}, headers=H).status_code == 200
    )
    r = c.post(
        "/v1/models/fold:invoke",
        json={
            "input": {},
            "execution": {
                "io_mode": "application",
                "private_input_ref": "private-fallback",
                "placement": {"gpu_preference": ["rtx-pro-6000", "l40s"], "fallback_wait_s": 60},
            },
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    root = r.json()["id"]
    successor = f"{root}-r1"
    create = fake.create_namespaced_job

    def require_private_input(ns, body):
        assert fake.secrets[(ns, f"{successor}-private")]["data"] == {"input.json": "private"}
        return create(ns, body)

    monkeypatch.setattr(fake, "create_namespaced_job", require_private_input)
    if not binding_available:
        assert c.delete("/v1/bindings/private-fallback", headers=H).status_code == 204
    moved = appmod.fallback_switch()
    assert moved == ([successor] if binding_available else [])
    original = fake.jobs[("tenant-demo", root)]
    if binding_available:
        assert fake.secrets[("tenant-demo", f"{successor}-private")]["metadata"]["labels"][f"{LABEL}/operation"] == root
        assert original["metadata"]["annotations"][f"{LABEL}/successor"] == successor
    else:
        assert ("tenant-demo", successor) not in fake.jobs
        assert f"{LABEL}/successor" not in original["metadata"]["annotations"]


def test_resume_with_a_checkpoint_reference_needs_no_volume_and_may_change_class(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)  # after the client fixture's own fleet
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "region": "us-central1", "execution": {"recovery": {"on_infrastructure_loss": "fail"}}},
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    job = fake.jobs[("tenant-demo", op)]
    job["status"] = {"conditions": [{"type": "Failed", "status": "True", "message": "node lost"}]}
    fake.pvcs.pop(("tenant-demo", f"{op}-work"), None)  # the volume is gone with the region
    assert c.post(f"/v1/operations/{op}:resume", headers=H).status_code == 409
    assert c.post(f"/v1/operations/{op}:resume", json={"gpu_class": "l40s"}, headers=H).status_code == 400
    r = c.post(
        f"/v1/operations/{op}:resume",
        json={"checkpoint_ref": "s3://cust/ckpt/step-900", "gpu_class": "l40s"},
        headers=H,
    )
    assert r.status_code == 202, r.text
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert (
        main["image"].endswith("img:l40s")
        and {"name": "CHECKPOINT_REF", "value": "s3://cust/ckpt/step-900"} in main["env"]
    )
    assert (
        new["metadata"]["labels"][f"{LABEL}/gpu-class"] == "l40s"
        and new["metadata"]["annotations"][f"{LABEL}/pvc"] == f"{op}-r1-work"
    )
    assert new["spec"]["template"]["metadata"]["annotations"][f"{LABEL}/gpu-classes"] == "l40s"
    assert (
        "tenant-demo",
        f"{op}-r1-work",
    ) not in fake.pvcs  # fleet-placed: the dispatcher creates the fresh volume on the worker it picks


def test_resume_renders_the_pinned_revision(monkeypatch):
    import models

    old_spec = {**PER_CLASS, "command": "fold --v3"}
    current = catalog.normalise(
        {**models.to_entry({**PER_CLASS, "command": "fold --v4"}, managed_by="api"), "version": 4}
    )
    monkeypatch.setattr(db, "enabled", lambda: True)
    monkeypatch.setattr(
        db,
        "history",
        lambda mid, limit=50: [
            {"version": 4, "spec": {**PER_CLASS, "command": "fold --v4"}},
            {"version": 3, "spec": old_spec},
        ],
    )
    pinned = catalog.pinned(current, {f"{LABEL}/model-version": "3"})
    assert pinned["job"]["command"] == "fold --v3" and pinned["version"] == 3
    assert catalog.pinned(current, {f"{LABEL}/model-version": "4"}) is current
    assert catalog.pinned(current, {}) is current


def test_admitted_queued_run_names_its_cluster_phase_and_wait(client, monkeypatch):
    """docs/API.md: a run admitted on a worker whose node is still coming up shows the cluster, `phase` and a bounded
    reason from the pod the API lists (no Events), before any authenticated attempt exists; the QUEUED contract and the
    record-based `attempt_id` stay."""
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "for_operation", lambda root: [])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    monkeypatch.setattr(kube, "workload_reasons", lambda ns: {})
    r = c.post(
        "/v1/models/fold:invoke",
        json={"input": {}, "execution": {"placement": {"allowed_clusters": ["us-central1"]}}},
        headers=H,
    )
    op = r.json()["id"]
    uid = fake.jobs[("tenant-demo", op)]["metadata"].get("uid", "")
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "QUEUED"
        and got["phase"] == "waiting_for_admission"
        and got["cluster"] is None
        and got["attempt_id"] is None
    )
    # admitted on us-central1, no pod yet (the worker's copy is being created)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {uid: "us-central1"})
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "QUEUED"
        and got["cluster"] == "us-central1"
        and got["phase"] == "waiting_for_node"
        and "waiting for a node" in got["reason"]
    )
    # the pod exists and waits for a node (the pool scales from zero)
    pending = {
        "metadata": {
            "name": f"{op}-pq52n",
            "namespace": "tenant-demo",
            "labels": {"job-name": op, f"{LABEL}/tenant": "demo", f"{LABEL}/mode": "run"},
            "creationTimestamp": "2026-10-12T19:50:00Z",
        },
        "spec": {"containers": [{"name": "main", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
        "status": {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": "0/4 nodes are available: 1 Insufficient nvidia.com/gpu, 3 node(s) didn't match Pod's node affinity/selector. "
                    + "x" * 400,
                }
            ],
        },
    }
    fake.pods.append(pending)
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "QUEUED"
        and got["phase"] == "waiting_for_node"
        and got["reason"].startswith("0/4 nodes are available")
        and len(got["reason"]) <= 300
    )
    assert (
        got["cluster"] == "us-central1"
        and got["attempt_id"] is None
        and got["attempts"][-1]["attempt_id"] == f"{op}-pq52n"
    )
    # scheduled, the image is being pulled
    pending["status"] = {
        "phase": "Pending",
        "conditions": [{"type": "PodScheduled", "status": "True"}],
        "initContainerStatuses": [{"name": "fetch", "state": {"waiting": {"reason": "ContainerCreating"}}}],
        "containerStatuses": [{"name": "main", "state": {"waiting": {"reason": "PodInitializing"}}}],
    }
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["phase"] == "starting" and got["reason"] == "fetch: ContainerCreating"
    # running: no phase, no reason
    fake.pods.remove(pending)
    fake.pods.append(pod(op, "Running", started="2026-10-12T19:56:00Z"))
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert (
        got["status"] == "RUNNING"
        and got["phase"] is None
        and got["reason"] is None
        and got["cluster"] == "us-central1"
    )


def test_queued_operation_reports_the_workload_reason(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post("/v1/models/container-run:invoke", json={"input": {"image": "x", "command": "y"}}, headers=H)
    op = r.json()["id"]
    monkeypatch.setattr(
        kube,
        "workload_reasons",
        lambda ns: {
            f"uid-{op}": "Quota reserved failed: insufficient quota for nvidia.com/gpu in flavor rtx6000-spot-1x"
        },
    )
    got = c.get(f"/v1/operations/{op}", headers=H).json()
    assert got["status"] == "QUEUED" and "insufficient quota" in got["reason"]


def test_resume_replays_one_continuation_per_idempotency_key(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"input": {"image": "x", "command": "y"}, "region": "eu-north1"},
        headers=H,
    )
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {f"uid-{op}": "hub"})  # it ran on the hub ...
    fake.pvcs[("tenant-demo", f"{op}-work")] = {"metadata": {"name": f"{op}-work"}}  # ... and kept its volume
    h = {**H, "Idempotency-Key": "intent-7"}
    a = c.post(f"/v1/operations/{op}:resume", headers=h)
    b = c.post(f"/v1/operations/{op}:resume", headers=h)
    assert a.status_code == 202, a.text
    assert b.status_code == 200 and a.json()["id"] == b.json()["id"] and a.json()["id"].startswith(f"{op}-r-"), b.text
    conflict = c.post(f"/v1/operations/{op}:resume", json={"checkpoint_ref": "s3://other"}, headers=h)
    assert conflict.status_code == 409  # the same intent with different input
    other = c.post(f"/v1/operations/{op}:resume", headers={**H, "Idempotency-Key": "intent-8"})
    assert other.status_code == 202 and other.json()["id"] != a.json()["id"]
    assert len([k for k in fake.jobs if k[1].startswith(f"{op}-r")]) == 2


def test_resume_uses_the_acknowledged_checkpoint_pointer(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: TWO_CLASS_FLEET)
    m = _fold_model()
    monkeypatch.setattr(catalog, "get", lambda name: m if name == "fold" else None)
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    r = c.post(
        "/v1/models/fold:invoke",
        json={
            "input": {},
            "region": "us-central1",
            "execution": {"io_mode": "application", "recovery": {"on_infrastructure_loss": "fail"}},
        },
        headers=H,
    )
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {
        "conditions": [{"type": "Failed", "status": "True", "message": "node lost"}]
    }
    fake.pvcs.pop(("tenant-demo", f"{op}-work"), None)  # abrupt loss: the volume is gone with the node
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    monkeypatch.setattr(
        attempts,
        "for_operation",
        lambda root: [
            {
                "operation": op,
                "pod": f"{op}-a",
                "root": op,
                "tenant": "demo",
                "status": "started",
                "epoch": 1,
                "revision": 5,
                "checkpoint_ref": "app://ckpt/gen-3",
                "checkpoint_generation": 3,
                "gpus": 1,
            },
            {
                "operation": op,
                "pod": f"{op}-a",
                "root": op,
                "tenant": "demo",
                "status": "started",
                "epoch": 1,
                "revision": 4,
                "checkpoint_ref": "app://ckpt/gen-2",
                "checkpoint_generation": 2,
                "gpus": 1,
            },
        ],
    )
    assert attempts.latest_checkpoint(op)["checkpoint_ref"] == "app://ckpt/gen-3"
    r = c.post(
        f"/v1/operations/{op}:resume", json={"gpu_class": "l40s"}, headers=H
    )  # no checkpoint_ref: the acknowledged pointer
    assert r.status_code == 202, r.text
    new = fake.jobs[("tenant-demo", f"{op}-r1")]
    main = next(x for x in new["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert {"name": "CHECKPOINT_REF", "value": "app://ckpt/gen-3"} in main["env"] and main["image"].endswith("img:l40s")
    assert r.json()["execution"]["recovery"]["checkpoint_ref"] == "app://ckpt/gen-3"


def test_expired_operation_is_served_from_its_rows_and_superseded_handles_follow(
    client, admin_keys, fake_keys, monkeypatch
):
    c, fake = client
    from auth import Principal

    fake_keys.own_operation("op-gone", "demo", Principal("sk-good", "demo", KEY_INFO).owner, "hello-run")
    fake_keys.own_operation("op-old", "demo", Principal("sk-good", "demo", KEY_INFO).owner, "hello-run")
    monkeypatch.setattr(attempts, "enabled", lambda: True)
    rows = [
        {
            "operation": "op-gone",
            "pod": "op-gone-a",
            "root": "op-gone",
            "tenant": "demo",
            "status": "succeeded",
            "epoch": 1,
            "revision": 9,
            "export_state": "succeeded",
            "started_at": "2026-10-11T10:00:00Z",
            "ended_at": "2026-10-11T11:00:00Z",
            "gpus": 1,
            "region": "eu-north1",
        }
    ]
    monkeypatch.setattr(attempts, "for_operation", lambda root: rows if root == "op-gone" else [])
    monkeypatch.setattr(attempts, "_marks", lambda operation: None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    got = c.get("/v1/operations/op-gone", headers=H)
    assert (
        got.status_code == 200
        and got.json()["status"] == "SUCCEEDED"
        and got.json()["attempt_id"] == "op-gone-a"
        and "durable record" in got.json()["reason"]
    )
    assert c.get("/v1/operations/op-unknown", headers=H).status_code == 404
    # a superseded handle whose Job expired resolves through the database link
    monkeypatch.setattr(attempts, "_marks", lambda operation: (3, None, "op-gone") if operation == "op-old" else None)
    monkeypatch.setattr(attempts, "mark_successor", lambda operation, successor: None)
    assert c.get("/v1/operations/op-old", headers=H).json()["id"] == "op-gone"
    assert c.post("/v1/operations/op-gone:cancel", headers=H).status_code == 409
