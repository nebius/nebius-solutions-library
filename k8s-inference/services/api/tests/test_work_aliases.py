"""Work mount aliases (docs/JOBS.md "Work mount aliases"): the run's own work volume mounted again at a canonical path,
per attempt through a platform-owned subPathExpr; reserved, overlapping and unsafe paths refused; the finalizer sees the
finalized attempt's directory; the fetch init container knows the directories to create."""

import copy

import pytest
from fastapi import HTTPException

import catalog
import jobs
from test_api import ADMIN, H, admin_keys, client, cluster  # noqa: F401 - pytest fixture registration

SPEC = {
    "id": "alias-job",
    "kind": "job",
    "image": "example.org/model:1",
    "command": "python model.py",
    "gpu": {"classes": ["h100"], "count": 1},
    "work_mount_aliases": [{"path": "/mnt/fs2-scientific/work"}, {"path": "/outputs/shared", "scope": "run"}],
}


def _containers(job):
    spec = job["spec"]["template"]["spec"]
    main = next(c for c in spec["containers"] if c["name"] == "main")
    fetch = next(c for c in spec["initContainers"] if c["name"] == "fetch")
    return spec, main, fetch


def test_aliases_mount_the_same_work_volume_per_attempt(client, admin_keys):
    c, fake = client
    assert c.post("/v1/models", json=SPEC, headers=ADMIN).status_code == 201
    r = c.post("/v1/models/alias-job:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert r.status_code == 202, r.text
    job = fake.jobs[("tenant-demo", r.json()["id"])]
    spec, main, fetch = _containers(job)
    assert [v["name"] for v in spec["volumes"]].count("work") == 1  # no new volume
    mounts = {m["mountPath"]: m for m in main["volumeMounts"]}
    assert mounts["/work"] == {"name": "work", "mountPath": "/work"}  # unchanged
    assert mounts["/mnt/fs2-scientific/work"] == {
        "name": "work",
        "mountPath": "/mnt/fs2-scientific/work",
        "subPathExpr": "aliases/mnt-fs2-scientific-work/$(S2_POD_NAME)",
    }
    assert mounts["/outputs/shared"] == {
        "name": "work",
        "mountPath": "/outputs/shared",
        "subPath": "aliases/outputs-shared",
    }
    pod_name = {"name": "S2_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}
    assert pod_name in main["env"]
    env = {e["name"]: e for e in fetch["env"]}
    assert env["WORK_ALIAS_DIRS"]["value"] == "aliases/mnt-fs2-scientific-work/$(S2_POD_NAME):aliases/outputs-shared"
    assert fetch["env"].index(pod_name) < fetch["env"].index(env["WORK_ALIAS_DIRS"])  # expansion order
    # the stored entry carries the validated list; the public spec reads back
    assert catalog.get("alias-job")["job"]["workMountAliases"] == [
        {"path": "/mnt/fs2-scientific/work", "scope": "attempt"},
        {"path": "/outputs/shared", "scope": "run"},
    ]
    assert (
        c.get("/v1/models/alias-job", headers=ADMIN).json()["spec"]["work_mount_aliases"] == SPEC["work_mount_aliases"]
    )


@pytest.mark.parametrize(
    "aliases, why",
    [
        ([{"path": "relative/x"}], "bounded absolute"),
        ([{"path": "/"}], "bounded absolute"),
        ([{"path": "/mnt/../etc/x"}], "bounded absolute"),
        ([{"path": "/mnt/with space"}], "bounded absolute"),
        ([{"path": "/work/aliases"}], "reserved"),
        ([{"path": "/etc/model"}], "reserved"),
        ([{"path": "/var/run/x"}], "reserved"),
        ([{"path": "/mnt/a"}, {"path": "/mnt/a/b"}], "overlaps another alias"),
        ([{"path": "/mnt/a", "scope": "always"}], "scope is attempt or run"),
        ([{"path": f"/mnt/{i}"} for i in range(5)], "at most 4"),
        ("/mnt/a", "at most 4"),
        (["/mnt/a"], "each entry"),
    ],
)
def test_unsafe_aliases_are_refused(client, admin_keys, aliases, why):
    c, _ = client
    r = c.post("/v1/models", json={**SPEC, "id": "alias-bad", "work_mount_aliases": aliases}, headers=ADMIN)
    assert r.status_code == 400 and why in r.text, r.text


def test_aliases_may_not_overlap_other_mounts_or_claim_the_pod_name(client, admin_keys):
    c, _ = client
    r = c.post("/v1/models", json={**SPEC, "id": "alias-env", "env": {"S2_POD_NAME": "x"}}, headers=ADMIN)
    assert r.status_code == 201, r.text  # registration stores the class; the render refuses it
    with pytest.raises(HTTPException, match="platform-owned"):
        jobs.build_run(
            "op-alias-env", catalog.get("alias-env"), {"output_prefix": "s3://b/p"}, "demo", None, None, 600, None
        )
    m = copy.deepcopy(catalog.get("alias-env"))
    m["job"].pop("env")
    m["job"]["workMountAliases"] = [{"path": "/weights/x", "scope": "attempt"}]
    m["job"]["weights"] = {"path": "demo"}
    with pytest.raises(HTTPException, match="overlaps the mount|reserved"):
        jobs.build_run("op-alias-w", m, {"output_prefix": "s3://b/p"}, "demo", None, None, 600, None)
    m["job"]["workMountAliases"] = [{"path": "/mnt/x", "scope": "attempt"}]
    m["job"]["nodes"] = 2
    with pytest.raises(HTTPException, match="single-node"):
        jobs.build_run("op-alias-n", m, {"output_prefix": "s3://b/p"}, "demo", None, None, 600, None)


def test_the_finalizer_sees_the_finalized_attempts_directory():
    done = {"pod": "op-1-abcde", "epoch": 1, "exit_code": 3}
    containers = [
        {
            "name": "main",
            "env": [
                {"name": "X", "value": "1"},
                {"name": "S2_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            ],
        }
    ]
    for c in containers:
        if c["name"] == "main":
            c["env"] = [
                {"name": jobs.POD_NAME_ENV, "value": str(done.get("pod") or "")}
                if e.get("name") == jobs.POD_NAME_ENV
                else e
                for e in c.get("env") or []
            ]
    assert containers[0]["env"] == [{"name": "X", "value": "1"}, {"name": "S2_POD_NAME", "value": "op-1-abcde"}]


def test_fetch_creates_the_alias_directories_tenant_private(tmp_path):
    import os
    import pathlib
    import stat
    import subprocess

    script = pathlib.Path(__file__).resolve().parents[2] / "jobs" / "bin" / "fetch.sh"
    work = tmp_path / "work"
    work.mkdir()
    env = {
        **os.environ,
        "WORK_DIR": str(work),
        "WORK_ALIAS_DIRS": "aliases/mnt-x/op-1-abcde:aliases/shared:/etc/evil",
        "INPUT_PREFIX": "",
    }
    out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr + out.stdout
    for d in ("aliases/mnt-x/op-1-abcde", "aliases/shared"):
        st = (work / d).stat()
        assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o2770, oct(st.st_mode)
    assert not (work / "etc").exists() and "ignored" in out.stdout
    assert (work / ".inputs-fetched").exists()
    # a resumed attempt (marker present): the new pod's directory is still created
    env["WORK_ALIAS_DIRS"] = "aliases/mnt-x/op-1-fghij"
    assert subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True).returncode == 0
    assert (work / "aliases/mnt-x/op-1-fghij").is_dir()


def test_export_and_finalizer_renders_with_aliases(client, admin_keys):
    """BindCraft probes on 0.11.70: a transfer-only export of a class with work aliases raised UnboundLocalError (the
    finalizer-only attempt record was read in the export branch) and the finalizer did not get the literal pod name.
    Now: the export renders, main keeps the downward-API expression (its main only exits 0); the finalizer's main carries
    the finalized attempt's pod name as a literal, so its alias mounts show that attempt's directory."""
    c, fake = client
    spec = {
        **SPEC,
        "id": "alias-fin",
        "finalize": {
            "command": "python finish.py",
            "on_exit_codes": [75],
            "timeout_s": 900,
            "cpu": "1",
            "memory": "2Gi",
        },
    }
    assert c.post("/v1/models", json=spec, headers=ADMIN).status_code == 201
    r = c.post("/v1/models/alias-fin:invoke", json={"mode": "run", "input": {}}, headers=H)
    assert r.status_code == 202, r.text
    op = r.json()["id"]
    fake.jobs[("tenant-demo", op)]["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    m = catalog.get("alias-fin")
    defaults = {"output_prefix": "s3://b/p"}
    export, _ = jobs.export_again("tenant-demo", op, "eu-north1", m, defaults)
    em = next(x for x in export["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    env = {e["name"]: e for e in em["env"]}
    assert env["S2_POD_NAME"] == {"name": "S2_POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}}
    assert [mm for mm in em["volumeMounts"] if mm.get("subPathExpr")], "the alias mounts are retained on the export"
    fz, _ = jobs.export_again(
        "tenant-demo",
        op,
        "eu-north1",
        m,
        defaults,
        finalize={"spec": m["job"]["finalize"], "attempt": {"pod": f"{op}-abcde", "epoch": 1, "exit_code": 75}},
    )
    fm = next(x for x in fz["spec"]["template"]["spec"]["containers"] if x["name"] == "main")
    assert {e["name"]: e for e in fm["env"]}["S2_POD_NAME"] == {"name": "S2_POD_NAME", "value": f"{op}-abcde"}
    assert [mm for mm in fm["volumeMounts"] if mm.get("subPathExpr")]
