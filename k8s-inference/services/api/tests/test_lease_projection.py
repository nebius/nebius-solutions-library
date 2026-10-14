"""The lease client's projection (docs/API.md "Lease clients"): a model with `native_lease` gets the pod-bound token
file and the client's environment on both shapes (Knative InferenceService, multi-node LeaderWorkerSet); a model
without it gets nothing; the CA bundle rides as a value only when the fleet has one; the apply shape is unchanged."""

import pytest

import models
import render_endpoint
import test_api
import test_multinode_endpoints as mn
import test_render_endpoint_golden as g

PLAIN = {
    "id": "lease-example",
    "kind": "endpoint",
    "image": "example.org/model:1",
    "gpu": {"classes": ["h100"], "count": 1},
}


def _containers(objects):
    for d in objects:
        if d["kind"] == "InferenceService":
            yield d, d["spec"]["predictor"]["containers"][0], d["spec"]["predictor"].get("volumes") or []
        if d["kind"] == "LeaderWorkerSet":
            for role in ("leaderTemplate", "workerTemplate"):
                t = d["spec"]["leaderWorkerTemplate"][role]["spec"]
                yield d, t["containers"][0], t.get("volumes") or []


def _env(c):
    return {e["name"]: e.get("value") for e in c.get("env") or []}


def test_lease_projection_on_both_shapes(monkeypatch):
    monkeypatch.setattr(render_endpoint, "PUBLIC_API_URL", "https://api.example.test")
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: None)
    spec = {**PLAIN, "native_lease": "required"}
    objs = g.native(g._entry(spec, test_api.FLEET), "hub")
    assert [d["kind"] for d in objs] == [d["kind"] for d in g.native(g._entry(PLAIN, test_api.FLEET), "hub")]
    seen = 0
    for _d, c, vols in _containers(objs):
        seen += 1
        env = _env(c)
        assert env["S2_API_URL"] == "https://api.example.test"
        assert env["S2_LEASE_TOKEN_FILE"] == "/var/run/secrets/serverless2/token"
        assert env["S2_LEASE_MODE"] == "required" and env["S2_LEASE_AUDIENCE"] == "serverless2"
        assert "S2_CA_PEM" not in env
        assert {"name": "s2-lease", "mountPath": "/var/run/secrets/serverless2", "readOnly": True} in c["volumeMounts"]
        (vol,) = [x for x in vols if x["name"] == "s2-lease"]
        (src,) = vol["projected"]["sources"]
        assert src["serviceAccountToken"] == {"path": "token", "audience": "serverless2", "expirationSeconds": 3600}
    assert seen == 1
    # multi-node: leader and worker alike
    monkeypatch.setattr(mn.kube, "fleet", lambda: mn.POOLS)
    monkeypatch.setattr(mn.kube, "cluster_region", lambda c: c)
    e = models.to_entry({**mn.SPEC, "native_lease": "optional"})
    objs = [
        d
        for v in models.variants(e, "us-central1")
        for d in render_endpoint.render(e, "us-central1", g.DOMAIN, g.IB_CLAIM_TEMPLATE, v, True)
    ]
    seen = 0
    for _d, c, vols in _containers(objs):
        seen += 1
        assert _env(c)["S2_LEASE_MODE"] == "optional"
        assert any(x["name"] == "s2-lease" for x in vols) and any(m["name"] == "s2-lease" for m in c["volumeMounts"])
    assert seen == 2


def test_without_native_lease_nothing_is_projected(monkeypatch):
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: None)
    for _d, c, vols in _containers(g.native(g._entry(PLAIN, test_api.FLEET), "hub")):
        assert not [k for k in _env(c) if k.startswith("S2_")]
        assert not [x for x in vols if x["name"] == "s2-lease"]
        assert not [m for m in c.get("volumeMounts") or [] if m["name"] == "s2-lease"]


def test_ca_bundle_rides_as_a_value_when_the_fleet_has_one(monkeypatch):
    monkeypatch.setattr(
        render_endpoint, "trust_bundle", lambda: "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"
    )
    spec = {**PLAIN, "native_lease": "optional"}
    for _d, c, _v in _containers(g.native(g._entry(spec, test_api.FLEET), "hub")):
        assert _env(c)["S2_CA_PEM"].startswith("-----BEGIN CERTIFICATE-----")
    with pytest.raises(Exception, match="native_lease"):
        g._entry({**PLAIN, "native_lease": "always"}, test_api.FLEET)


def test_lifecycle_digest_is_observed_on_the_live_pod_and_states_are_explicit():
    import attempts

    rows = [
        {"operation": "op-1", "pod": "op-1-abc", "epoch": 1, "status": "started", "image_digest": None, "revision": 3}
    ]
    pod = {"metadata": {"name": "op-1-abc"}, "status": {"containerStatuses": [{"name": "main", "imageID": ""}]}}
    out = attempts.lifecycle("op-1", "op-1", rows, None, pods=[pod])
    assert out["image_digest"] is None and out["image_digest_state"] == "pending"
    pod["status"]["containerStatuses"][0]["imageID"] = "registry.serverless2.local/nebius/m@sha256:abc"
    out = attempts.lifecycle("op-1", "op-1", rows, None, pods=[pod])
    assert (
        out["image_digest"] == "registry.serverless2.local/nebius/m@sha256:abc"
        and out["image_digest_state"] == "observed"
    )
    rows[0]["image_digest"] = "registry.serverless2.local/nebius/m@sha256:rec"  # the record, once it has one, wins
    assert attempts.lifecycle("op-1", "op-1", rows, None, pods=[pod])["image_digest"].endswith(":rec")
    ended = [{**rows[0], "image_digest": None, "status": "failed"}]
    out = attempts.lifecycle("op-1", "op-1", ended, None, pods=[])
    assert out["image_digest"] is None and out["image_digest_state"] == "unknown"
    assert attempts.lifecycle("op-1", "op-1", [], None)["image_digest_state"] is None
