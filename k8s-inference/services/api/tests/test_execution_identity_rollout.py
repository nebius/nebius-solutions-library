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


def _pod(ready=True, image_id="docker://img@sha256:abc"):
    cs = NS(name="kserve-container", ready=ready, image="img:5", image_id=image_id)
    return NS(
        metadata=NS(creation_timestamp=None, labels={}),
        status=NS(phase="Running", container_statuses=[cs], conditions=[]),
    )


def _isvc(created, ready, rolled, ready_cond=True):
    return {
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready_cond else "False"}],
            "components": {
                "predictor": {
                    "latestCreatedRevision": created,
                    "latestReadyRevision": ready,
                    "latestRolledoutRevision": rolled,
                }
            },
        }
    }


def test_rollout_state_reads_revisions_and_serving_images(monkeypatch):
    kube._isvc_cache.clear()
    diverged = _rollout_of(monkeypatch, _isvc("m-00005", "m-00004", "m-00004"), [_pod()])
    assert diverged["converged"] is False and diverged["latest_ready"] == "m-00004"
    assert diverged["serving_images"] == ["docker://img@sha256:abc"]
    converged_zero = _rollout_of(monkeypatch, _isvc("m-00005", "m-00005", "m-00005"), [])
    assert converged_zero["converged"] is True and converged_zero["serving_images"] == []  # zero-hot: converged
    converged = _rollout_of(monkeypatch, _isvc("m-00005", "m-00005", "m-00005"), [_pod(), _pod(ready=False)])
    assert converged["converged"] is True and len(converged["serving_images"]) == 1
    unknown = _rollout_of(monkeypatch, {"status": {}}, [])
    assert unknown["converged"] is False and unknown["latest_created"] is None


def _rollout_of(monkeypatch, isvc, pods):
    kube._isvc_cache.clear()
    api = NS(get_namespaced_custom_object=lambda g, v, ns, plural, name, **kw: isvc)
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
    assert r.status_code == 409 and "not converged" in r.json()["detail"] and "r-5" in r.json()["detail"], r.text
    assert not [k for k in cl.jobs if k[0] == NS_T]  # nothing admitted
    # converged at zero: admitted, the ready revision and image recorded on the call
    state["rollout"].update(
        {"latest_ready": "r-5", "latest_rolled_out": "r-5", "serving_images": [], "converged": True}
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
