"""Operator-declared parameter bounds on a run class (docs/JOBS.md "Parameters"): number min/max/integer, select
options and job.maxGpus are enforced at invoke on the effective values, before any reservation or Job, for the
operator catalog's templated classes; a malformed numeric is refused, never coerced; classes without bounds are
untouched; the admitted shapes keep the same model, grant and accounting."""

import copy

import pytest
from fastapi import HTTPException

import catalog
import jobs
import kube
from test_api import MULTINODE_FLEET, H, admin_keys, client, cluster, fake_keys  # noqa: F401

CLASS = {
    "id": "mpi-shape",
    "mode": "run",
    "protocol": "kubernetes-job",
    "gpu": {"count": "8", "classes": ["h100"]},
    "job": {
        "image": "{{image}}",
        "command": "mpirun gmx_mpi mdrun",
        "nodes": "{{nodes}}",
        "gpu": "{{gpus_per_node}}",
        "cpu": "8",
        "memory": "16Gi",
        "pvcSizeGi": 10,
        "interconnect": "none",
        "checkpoints": "local",
        "maxGpus": 16,
    },
    "parameters": [
        {"name": "image", "type": "string", "default": "registry.serverless2.local/nebius/gromacs:1"},
        {"name": "nodes", "type": "number", "default": 1, "min": 1, "max": 8, "integer": True},
        {"name": "gpus_per_node", "type": "select", "options": [1, 2, 4, 8], "default": 1},
        {"name": "threads", "type": "number", "default": 4, "min": 1, "max": 8, "integer": True},
        {"name": "seed", "type": "number", "default": 7},  # no bounds declared: as before
    ],
}


@pytest.fixture
def rig(client, admin_keys, fake_keys, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    entry = catalog.normalise(copy.deepcopy(CLASS))
    real = catalog.get
    monkeypatch.setattr(catalog, "get", lambda name: entry if name == "mpi-shape" else real(name))
    return c, fake


def _invoke(c, **params):
    return c.post("/v1/models/mpi-shape:invoke", json={"input": params}, headers=H)


def test_exact_shapes_within_the_bounds_run_under_the_same_model_grant_and_accounting(rig, fake_keys):
    c, fake = rig
    for nodes, gpus in ((1, 1), (2, 1), (2, 8)):
        r = _invoke(c, nodes=nodes, gpus_per_node=gpus)
        assert r.status_code == 202, (nodes, gpus, r.text)
        op = r.json()
        assert op["model"] == "mpi-shape" and op["nodes"] == nodes
        key = ("tenant-demo", op["id"])
        if nodes == 1:
            job = fake.jobs[key]
            main = next(x for x in job["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
            assert main["resources"]["limits"]["nvidia.com/gpu"] == str(gpus)
        else:
            js = fake.custom[("tenant-demo", "jobsets", op["id"])]
            spec = js["spec"]["replicatedJobs"][0]["template"]["spec"]
            assert spec["parallelism"] == nodes
            main = next(x for x in spec["template"]["spec"]["containers"] if x["name"] == "main")
            assert main["resources"]["limits"]["nvidia.com/gpu"] == str(gpus)
        assert ("admit", op["id"], 0.0, True) in fake_keys.calls  # the same key and ledger for every shape


@pytest.mark.parametrize(
    "params,hint",
    [
        ({"nodes": 9, "gpus_per_node": 1}, "above the maximum 8"),
        ({"nodes": 0, "gpus_per_node": 1}, "below the minimum 1"),
        ({"nodes": 2.5, "gpus_per_node": 1}, "an integer is required"),
        ({"nodes": "2", "gpus_per_node": 1}, "a finite number is required"),
        ({"nodes": True, "gpus_per_node": 1}, "a finite number is required"),
        ({"nodes": 2, "gpus_per_node": 3}, "not one of [1, 2, 4, 8]"),
        ({"nodes": 2, "gpus_per_node": "all"}, "not one of"),
        ({"nodes": 2, "gpus_per_node": True}, "not one of"),
        ({"nodes": 4, "gpus_per_node": 8}, "32 GPUs exceeds the class's maxGpus (16)"),
        ({"nodes": 8, "gpus_per_node": 4}, "exceeds the class's maxGpus (16)"),
        ({"nodes": 1, "gpus_per_node": 1, "threads": 16}, "threads: 16 is above the maximum 8"),
    ],
)
def test_out_of_bounds_and_malformed_shapes_are_refused_before_any_reservation_or_job(rig, fake_keys, params, hint):
    c, fake = rig
    before_jobs, before_sets = set(fake.jobs), set(fake.custom)
    r = _invoke(c, **params)
    assert r.status_code == 400, (params, r.text)
    assert hint in r.text, (params, r.text)
    assert set(fake.jobs) == before_jobs and set(fake.custom) == before_sets  # nothing rendered or created
    assert not [x for x in fake_keys.calls if x[0] in ("admit", "prepare")]  # nothing reserved


def test_an_unbounded_parameter_and_a_class_without_bounds_behave_as_before(rig, client, fake_keys):
    c, fake = rig
    assert _invoke(c, nodes=1, gpus_per_node=1, seed=123456789).status_code == 202  # no bounds declared on seed
    # the bundled distributed-run declares no bounds: the renderer's own rules alone (a shape no pool has: 400 there)
    body = {"input": {"image": "x/y:1", "command": "echo", "nodes": 2, "gpus_per_node": 8, "interconnect": "none"}}
    assert c.post("/v1/models/distributed-run:invoke", json=body, headers=H).status_code == 202
    assert (
        c.post(
            "/v1/models/distributed-run:invoke", json={"input": {**body["input"], "gpus_per_node": 2}}, headers=H
        ).status_code
        == 400
    )


def test_bounds_are_checked_on_the_effective_values_and_the_helpers_refuse_bad_declarations():
    params = [
        {"name": "n", "type": "number", "min": 1, "max": 4, "integer": True},
        {"name": "s", "type": "select", "options": ["a", "b"]},
    ]
    jobs.check_parameter_bounds(params, {"n": 2, "s": "a"})
    v = {"n": 3.0, "s": "b"}
    jobs.check_parameter_bounds(params, v)
    assert v["n"] == 3 and isinstance(v["n"], int)  # an integral float is accepted as the integer it is
    for bad in ({"n": 5}, {"n": 0}, {"n": 1.5}, {"n": "2"}, {"n": True}, {"s": "c"}, {"s": None, "n": float("inf")}):
        with pytest.raises(HTTPException) as e:
            jobs.check_parameter_bounds(params, bad)
        assert e.value.status_code == 400
    jobs.check_parameter_bounds(params, {})  # nothing provided: nothing to check (defaults are merged by the caller)
    jobs.check_gpu_bound({"maxGpus": 16}, {"nodes": 2, "gpu": 8}, "m")
    with pytest.raises(HTTPException):
        jobs.check_gpu_bound({"maxGpus": 16}, {"nodes": 3, "gpu": 8}, "m")
    with pytest.raises(HTTPException):
        jobs.check_gpu_bound({"maxGpus": "sixteen"}, {"nodes": 1, "gpu": 1}, "m")
    jobs.check_gpu_bound({}, {"nodes": 64, "gpu": 8}, "m")  # no bound declared


def test_a_nan_in_the_raw_request_body_is_refused_before_any_reservation(rig, fake_keys):
    """A JSON body carrying NaN (which Python's parser accepts) never reaches the renderer as a number."""
    c, fake = rig
    r = c.post(
        "/v1/models/mpi-shape:invoke",
        content=b'{"input": {"nodes": NaN, "gpus_per_node": 1}}',
        headers={**H, "Content-Type": "application/json"},
    )
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        assert "a finite number is required" in r.text
    assert not [x for x in fake_keys.calls if x[0] in ("admit", "prepare")] and not [
        k for k in fake.custom if "mpi-shape" in k[2]
    ]
