"""Execution options of a run (docs/JOBS.md "Execution options", services/api/execution.py): application-managed I/O,
workload identities, private inputs through bindings, execution profiles, placement narrowing, recovery policy, and the
runner token that never reaches the model's container."""

import json

import pytest
from fastapi import HTTPException

import catalog
import execution
import jobs
import kube
from config import LABEL, S3_ENV_SECRET
from test_api import FLEET, H, client, cluster  # noqa: F401  (sets the catalog path before the API modules load)

TENANT_CFG = {
    "identities": {"sci": {"audience": "sts.amazonaws.com", "env": {"AWS_ROLE_ARN": "arn:aws:iam::1:role/sci"}}},
    "execution_profiles": {"confidential-job-v1": {"shared_filesystem": False}, "open": {"shared_filesystem": True}},
}


@pytest.fixture(autouse=True)
def tenant_cfg(monkeypatch):
    monkeypatch.setattr(execution, "tenant_config", lambda ns, region="eu-north1": TENANT_CFG)
    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda c: "eu-north1" if c == "hub" else c)


def _ex(**kw):
    m = catalog.get("container-run")
    return execution.resolve(execution.ExecutionSpec(**kw), "tenant-demo", m)


def _run(ex=None, params=None, pvc=None):
    m = catalog.get("container-run")
    return jobs.build_run(
        "op-x-1",
        m,
        {"image": "img", "command": "run", "data": "", **(params or {})},
        "demo",
        None,
        None,
        None,
        None,
        "eu-north1",
        {"output_prefix": "" if (ex or {}).get("io_mode") == "application" else "s3://b/out"},
        ex=ex,
        pvc=pvc,
    )


def _containers(job):
    pod = job["spec"]["template"]["spec"]
    return pod, {c["name"]: c for c in pod["containers"]}, {c["name"]: c for c in pod.get("initContainers", [])}


def test_runner_token_reaches_the_uploader_only():
    pod, cs, inits = _containers(_run())
    assert pod["automountServiceAccountToken"] is False
    vol = next(v for v in pod["volumes"] if v["name"] == "runner-token")
    assert vol["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"] == 3600
    assert any(
        m["name"] == "runner-token" and m["mountPath"] == execution.RUNNER_TOKEN_DIR
        for m in cs["uploader"]["volumeMounts"]
    )
    assert not any(m["name"] == "runner-token" for m in cs["main"]["volumeMounts"])
    assert not any(m["name"] == "runner-token" for m in inits["fetch"]["volumeMounts"])


def test_unknown_execution_fields_are_refused(client):
    c, _ = client
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"input": {"image": "x", "command": "y"}, "execution": {"io_mode": "application", "bucket": "mine"}},
        headers=H,
    )
    assert r.status_code == 422
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"input": {"image": "x", "command": "y"}, "execution": {"io_mode": "sideways"}},
        headers=H,
    )
    assert r.status_code == 422


def test_application_io_has_no_bucket_credentials_and_no_fetch():
    job = _run(_ex(io_mode="application"))
    pod, cs, inits = _containers(job)
    assert "fetch" not in inits
    assert not any((e.get("secretRef") or {}).get("name") == S3_ENV_SECRET for e in cs["main"].get("envFrom", []))
    assert "envFrom" not in cs["uploader"]
    env = {e["name"]: e.get("value") for e in cs["uploader"]["env"]}
    assert env["UPLOAD_MODE"] == "record" and "OUTPUT_PREFIX" not in env
    assert json.loads(job["metadata"]["annotations"][f"{LABEL}/execution"]) == {"io_mode": "application"}
    assert jobs.normalise(job)["execution"] == {"io_mode": "application"}


def test_managed_io_keeps_env_secrets_next_to_the_bucket_credentials():
    m = dict(catalog.get("container-run"))
    m["job"] = {**m["job"], "envFrom": [{"secretRef": {"name": "my-secret"}}]}
    job = jobs.build_run(
        "op-x-2",
        m,
        {"image": "img", "command": "run"},
        "demo",
        None,
        None,
        None,
        None,
        "eu-north1",
        {"output_prefix": "s3://b/o"},
    )
    _, cs, _ = _containers(job)
    assert [e["secretRef"]["name"] for e in cs["main"]["envFrom"]] == ["my-secret", S3_ENV_SECRET]


def test_workload_identity_runs_as_its_account_with_a_scoped_token():
    ex = _ex(workload_identity_ref="sci")
    assert ex["identity"] == {
        "ref": "sci",
        "service_account": "identity-sci",
        "audience": "sts.amazonaws.com",
        "env": {"AWS_ROLE_ARN": "arn:aws:iam::1:role/sci"},
    }
    pod, cs, _ = _containers(_run(ex))
    assert pod["serviceAccountName"] == "identity-sci"
    vol = next(v for v in pod["volumes"] if v["name"] == "workload-identity")
    assert vol["projected"]["sources"] == [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600, "audience": "sts.amazonaws.com"}}
    ]
    assert {"name": "workload-identity", "mountPath": execution.IDENTITY_DIR, "readOnly": True} in cs["main"][
        "volumeMounts"
    ]
    assert not any(m["name"] == "workload-identity" for m in cs["uploader"]["volumeMounts"])
    env = {e["name"]: e.get("value") for e in cs["main"]["env"]}
    assert (
        env["AWS_WEB_IDENTITY_TOKEN_FILE"] == f"{execution.IDENTITY_DIR}/token"
        and env["AWS_ROLE_ARN"] == "arn:aws:iam::1:role/sci"
    )
    with pytest.raises(HTTPException) as e:
        _ex(workload_identity_ref="other-tenants")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        _ex(workload_identity_ref="Not_A_Label")
    assert e.value.status_code == 400


def test_execution_profile_labels_the_pod_and_keeps_the_shared_filesystem_out():
    job = _run(_ex(profile_ref="confidential-job-v1"))
    assert job["spec"]["template"]["metadata"]["labels"][f"{LABEL}/execution-profile"] == "confidential-job-v1"
    with pytest.raises(HTTPException) as e:
        _run(_ex(profile_ref="confidential-job-v1"), {"data": "refdb"})
    assert e.value.status_code == 400
    assert _run(_ex(profile_ref="open"), {"data": "refdb"})  # a profile may allow it
    with pytest.raises(HTTPException) as e:
        _ex(profile_ref="nope")
    assert e.value.status_code == 403


def test_recovery_fail_turns_a_disruption_into_a_failed_job():
    job = _run(_ex(recovery={"on_infrastructure_loss": "fail", "checkpoint_ref": "s3://cust/ckpt/7"}))
    assert job["spec"]["backoffLimit"] == 0 and job["spec"]["podFailurePolicy"]["rules"][0]["action"] == "FailJob"
    _, cs, _ = _containers(job)
    assert {"name": "CHECKPOINT_REF", "value": "s3://cust/ckpt/7"} in cs["main"]["env"]
    assert _run(_ex())["spec"]["backoffLimit"] > 0


def test_placement_narrows_never_widens():
    m = {"name": "m", "gpu_classes": ["h100", "l40s"], "regions": ["eu-north1", "eu-south1"]}
    ex = execution.resolve(
        execution.ExecutionSpec(
            placement={"gpu_preference": ["l40s", "b200", "h100"], "allowed_clusters": ["eu-south1", "mars"]}
        ),
        "tenant-demo",
        m,
    )
    assert ex["placement"] == {"gpu_preference": ["l40s", "h100"], "allowed_clusters": ["eu-south1"]}
    pl = execution.narrow_placement(
        {"manager": True, "classes": ["h100", "l40s"], "regions": ["eu-north1", "eu-south1"]}, ex
    )
    assert pl["classes"] == ["l40s", "h100"] and pl["regions"] == ["eu-south1"] and pl["manager"] is True
    with pytest.raises(HTTPException):
        execution.resolve(execution.ExecutionSpec(placement={"gpu_preference": ["b200"]}), "tenant-demo", m)
    with pytest.raises(HTTPException):
        execution.resolve(execution.ExecutionSpec(placement={"allowed_clusters": ["mars"]}), "tenant-demo", m)


def test_bindings_are_delivered_per_operation_and_serve_one_operation(client):
    c, fake = client
    r = c.put("/v1/bindings/cust-a", json={"data": {"AWS_ACCESS_KEY_ID": "AKIA", "manifest.json": "{}"}}, headers=H)
    assert (
        r.status_code == 200
        and r.json()["operation"] is None
        and r.json()["keys"] == ["AWS_ACCESS_KEY_ID", "manifest.json"]
    )
    assert c.get("/v1/bindings", headers=H).json()[0]["ref"] == "cust-a"
    r = c.post(
        "/v1/models/container-run:invoke",
        json={
            "input": {"image": "x", "command": "y"},
            "execution": {"io_mode": "application", "private_input_ref": "cust-a"},
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    copy = fake.secrets[("tenant-demo", f"{op}-private")]
    assert copy["data"]["AWS_ACCESS_KEY_ID"] == "AKIA" and copy["metadata"]["labels"][f"{LABEL}/operation"] == op
    assert fake.secrets[("tenant-demo", "binding-cust-a")]["metadata"]["annotations"][f"{LABEL}/operation"] == op
    job = fake.jobs[("tenant-demo", op)]
    pod = job["spec"]["template"]["spec"]
    assert next(v for v in pod["volumes"] if v["name"] == "private-input")["secret"]["secretName"] == f"{op}-private"
    main = next(x for x in pod["containers"] if x["name"] == "main")
    assert {"name": "private-input", "mountPath": execution.PRIVATE_DIR, "readOnly": True} in main["volumeMounts"]
    assert "AKIA" not in json.dumps(job)  # nothing confidential in the Job
    # a second operation cannot reuse the binding
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"name": "two", "input": {"image": "x", "command": "z"}, "execution": {"private_input_ref": "cust-a"}},
        headers=H,
    )
    assert r.status_code == 409
    # a refresh reaches the copy; a revocation empties it, then removes it
    c.put("/v1/bindings/cust-a", json={"data": {"AWS_ACCESS_KEY_ID": "AKIA2"}}, headers=H)
    assert fake.secrets[("tenant-demo", f"{op}-private")]["data"] == {"AWS_ACCESS_KEY_ID": "AKIA2"}
    assert c.delete("/v1/bindings/cust-a", headers=H).status_code == 204
    assert ("tenant-demo", f"{op}-private") not in fake.secrets and (
        "tenant-demo",
        "binding-cust-a",
    ) not in fake.secrets
    assert c.delete("/v1/bindings/cust-a", headers=H).status_code == 404
    r = c.post(
        "/v1/models/container-run:invoke",
        json={"name": "three", "input": {"image": "x", "command": "z"}, "execution": {"private_input_ref": "cust-a"}},
        headers=H,
    )
    assert r.status_code == 404


def test_application_io_refuses_bucket_prefixes_and_sync_calls_take_no_execution(client):
    c, _ = client
    r = c.post(
        "/v1/models/container-run:invoke",
        json={
            "input": {"image": "x", "command": "y", "input_prefix": "s3://b/in"},
            "execution": {"io_mode": "application"},
        },
        headers=H,
    )
    assert r.status_code == 400
    r = c.post(
        "/v1/models/llm-example:invoke",
        json={"input": {"prompt": "hi"}, "execution": {"io_mode": "application"}},
        headers=H,
    )
    assert r.status_code == 400


def test_resume_delivers_binding_before_job_is_admitted(client, monkeypatch):
    c, fake = client
    monkeypatch.setattr(kube, "workload_clusters", lambda ns: {})
    assert c.put("/v1/bindings/resume-input", json={"data": {"input.json": "private"}}, headers=H).status_code == 200
    r = c.post(
        "/v1/models/container-run:invoke",
        json={
            "input": {"image": "x", "command": "y"},
            "execution": {"io_mode": "application", "private_input_ref": "resume-input"},
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    root = r.json()["id"]
    fake.jobs[("tenant-demo", root)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    create = fake.create_namespaced_job

    def require_private_input(ns, body):
        name = body["metadata"]["name"]
        assert fake.secrets[(ns, f"{name}-private")]["data"] == {"input.json": "private"}
        return create(ns, body)

    monkeypatch.setattr(fake, "create_namespaced_job", require_private_input)
    r = c.post(f"/v1/operations/{root}:resume", json={"checkpoint_ref": "app://checkpoint/1"}, headers=H)
    assert r.status_code == 202, r.text
    assert ("tenant-demo", f"{r.json()['id']}-work") in fake.pvcs  # non-fleet portable resume gets a new volume


def test_cleanup_of_finished_attempt_preserves_live_successor_and_binding(client):
    c, fake = client
    assert c.put("/v1/bindings/cleanup-input", json={"data": {"input.json": "private"}}, headers=H).status_code == 200
    ex = {"private_input_ref": "cleanup-input"}
    owner = fake.secrets[("tenant-demo", "binding-cleanup-input")]["metadata"]["annotations"][
        execution.OWNER_ANNOTATION
    ]
    execution.deliver(ex, "op-root", "op-root", "tenant-demo", "demo", ["eu-north1"], owner=owner)
    execution.deliver(ex, "op-root-r1", "op-root", "tenant-demo", "demo", ["eu-north1"], owner=owner)
    execution.deliver(ex, "op-root-r2", "op-root", "tenant-demo", "demo", ["eu-north1"], owner=owner)
    execution.sweep_copies("tenant-demo", "op-root-r1")
    assert ("tenant-demo", "op-root-r1-private") not in fake.secrets
    execution.sweep_copies("tenant-demo", "op-root")
    assert ("tenant-demo", "op-root-private") not in fake.secrets
    assert fake.secrets[("tenant-demo", "op-root-r2-private")]["data"] == {"input.json": "private"}
    assert ("tenant-demo", "binding-cleanup-input") in fake.secrets
    execution.sweep_copies("tenant-demo", "op-root")  # idempotent


def test_capabilities_are_discoverable(client):
    c, _ = client
    ex = c.get("/v1/fleet", headers=H).json()["execution"]
    assert (
        ex["io_modes"] == ["managed", "application"]
        and ex["identities"] == ["sci"]
        and ex["execution_profiles"] == ["confidential-job-v1", "open"]
    )
    assert ex["identity_token_file"].endswith("/token") and "oidc_issuers" in ex


def test_multinode_fleet_run_without_execution_block_submits(client, monkeypatch):
    """A JobSet keeps its pod template under its replicated Job: the fleet path must not expect a Job's template."""
    c, fake = client
    from test_api import MULTINODE_FLEET

    monkeypatch.setattr(kube, "fleet", lambda: MULTINODE_FLEET)
    monkeypatch.setattr(kube, "cluster_region", lambda cl: "eu-north1" if cl == "hub" else cl)
    r = c.post(
        "/v1/models/distributed-run:invoke",
        json={
            "input": {"image": "x/y:1", "command": "echo", "nodes": 2, "gpus_per_node": 8, "interconnect": "required"},
            "region": "eu-north1",
        },
        headers=H,
    )
    assert r.status_code == 202, r.text
    js = fake.custom[("tenant-demo", "jobsets", r.json()["id"])]
    assert jobs.placement_regions(js) == ["eu-north1"] or jobs.placement_regions(js) == []


def test_bounded_retry_policy_counts_only_the_named_exits_within_the_same_job():
    """docs/JOBS.md "Retries": `execution.retry` (or the class's `retry`) renders at most max_attempts pods of the same
    Job: an exit in on_exit_codes (137/143 by default, the retained scientific policy) is counted against
    backoffLimit = max_attempts - 1, any other non-zero exit fails the Job at once, a disruption stays ignored (the
    platform's own replacement, never an attempt); a finalizer's FailJob codes stay first. The same Job, member and
    reservation: nothing is admitted again."""
    job = _run(_ex(retry={"max_attempts": 2}))
    assert job["spec"]["backoffLimit"] == 1
    rules = job["spec"]["podFailurePolicy"]["rules"]
    assert rules[0] == {"action": "Ignore", "onPodConditions": [{"type": "DisruptionTarget", "status": "True"}]}
    assert rules[1] == {
        "action": "Count",
        "onExitCodes": {"containerName": "main", "operator": "In", "values": [137, 143]},
    }
    assert rules[2] == {
        "action": "FailJob",
        "onExitCodes": {"containerName": "main", "operator": "NotIn", "values": [0, 137, 143]},
    }
    # custom codes, one attempt only: nothing is retried, every non-zero exit fails
    job = _run(_ex(retry={"max_attempts": 1, "on_exit_codes": [75, 137]}))
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["podFailurePolicy"]["rules"][1]["onExitCodes"]["values"] == [75, 137]
    # the default (no policy): the platform's own rules, unchanged
    plain = _run(_ex())
    assert plain["spec"]["backoffLimit"] == 3 and plain["spec"]["podFailurePolicy"]["rules"][1]["action"] == "Ignore"
    # recovery `fail` and a retry policy together: the disruption is fatal, the named exits still count
    job = _run(_ex(retry={"max_attempts": 2}, recovery={"on_infrastructure_loss": "fail"}))
    rules = job["spec"]["podFailurePolicy"]["rules"]
    assert rules[0]["action"] == "FailJob" and "onPodConditions" in rules[0] and job["spec"]["backoffLimit"] == 1
    with pytest.raises(Exception):
        _ex(retry={"max_attempts": 11})
    with pytest.raises(HTTPException):
        _ex(retry={"max_attempts": 2, "on_exit_codes": [0]})


def test_run_class_retry_policy_is_the_default_and_the_request_overrides_it(monkeypatch):
    """A run class registered with `retry: {max_attempts: 2}` renders the policy for every run of the class; a request's
    `execution.retry` overrides it; a finalizer's FailJob codes stay ahead of the retry rules."""
    import kube
    import models
    from test_api import FLEET

    monkeypatch.setattr(kube, "fleet", lambda: FLEET)
    spec = {
        "id": "fold-r",
        "kind": "job",
        "image": "img",
        "command": "fold",
        "gpu": {"count": 1, "classes": ["h100"]},
        "retry": {"max_attempts": 2},
        "finalize": {"command": "deliver", "on_exit_codes": [75]},
    }
    entry = models.to_entry(spec)
    assert entry["job"]["retry"] == {"maxAttempts": 2, "onExitCodes": [137, 143]}
    m = catalog.normalise(entry)
    job = jobs.build_run("op-fold-r", m, {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://b/o"})
    rules = job["spec"]["podFailurePolicy"]["rules"]
    assert job["spec"]["backoffLimit"] == 1
    assert rules[0] == {
        "action": "FailJob",
        "onExitCodes": {"containerName": "main", "operator": "In", "values": [75]},
    }  # delivery pending: the finalizer's, never a retry
    assert rules[2]["action"] == "Count" and rules[2]["onExitCodes"]["values"] == [137, 143]
    ex = execution.resolve(execution.ExecutionSpec(retry={"max_attempts": 3, "on_exit_codes": [143]}), "tenant-demo", m)
    job = jobs.build_run(
        "op-fold-r2", m, {}, "demo", None, None, None, None, "eu-north1", {"output_prefix": "s3://b/o"}, ex=ex
    )
    assert job["spec"]["backoffLimit"] == 2 and job["spec"]["podFailurePolicy"]["rules"][2]["onExitCodes"][
        "values"
    ] == [143]
    for bad in ({"max_attempts": 0}, {"max_attempts": 2, "on_exit_codes": []}, {"on_exit_codes": [137]}, "2"):
        with pytest.raises(HTTPException):
            models.to_entry({**spec, "retry": bad})
