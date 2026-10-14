"""The lease client's projection (docs/API.md "Lease clients"): a model with `native_lease` gets the pod-bound token
file and the client's environment on both shapes (Knative InferenceService, multi-node LeaderWorkerSet); a model
without it gets nothing; on a fleet with a CA bundle the bundle is projected as a FILE beside the token (the
endpoint's ConfigMap, the standard SSL_CERT_FILE seam), whole, never as an environment value (api 0.11.77 rendered
the 467 KB bundle into `S2_CA_PEM`: above the kernel's single-argument bound, `exec: argument list too long`, the
container never started); the pinned shared client trusts it unchanged."""

import hashlib
import pathlib

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


CLIENT = pathlib.Path(__file__).resolve().parents[2] / "lease-client" / "s2lease.py"
CLIENT_SHA256 = "f96b274bd90dd2e29e2017b3cd71f570d83ee3c8357bbbac963d77092ea53885"  # the bytes model images carry
TRUST_FILE = "/var/run/secrets/serverless2/ca-bundle.pem"


def _bundle(n_bytes: int = 480_000) -> str:
    """A bundle the size of the live fleet's (the system roots plus the fleet CAs): many certificate blocks."""
    block = "-----BEGIN CERTIFICATE-----\n" + "MIIB" * 16 + "\n-----END CERTIFICATE-----\n"
    return block * (n_bytes // len(block) + 1)


def test_ca_bundle_is_projected_as_a_file_never_as_an_environment_value(monkeypatch):
    pem = _bundle()
    assert len(pem) > 467_369  # the live bundle that broke API77's native endpoints
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: pem)
    spec = {**PLAIN, "native_lease": "optional"}
    objs = g.native(g._entry(spec, test_api.FLEET), "hub")
    # one more object, first in the apply order (the pod's mount exists before the pod), the bundle whole
    assert objs[0]["kind"] == "ConfigMap"
    (cm,) = [d for d in objs if d["kind"] == "ConfigMap"]
    assert cm["metadata"]["name"] == "lease-example-trust" and cm["metadata"]["namespace"] == "models"
    assert cm["metadata"]["labels"]["app.kubernetes.io/name"] == "lease-example"  # delete_variants finds it
    assert cm["data"] == {"ca-bundle.pem": pem}
    seen = 0
    for _d, c, vols in _containers(objs):
        seen += 1
        env = _env(c)
        assert "S2_CA_PEM" not in env
        for k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "AWS_CA_BUNDLE", "S2_CA_FILE"):
            assert env[k] == TRUST_FILE
        assert sum(len(v or "") for v in env.values()) < 4096  # nothing bundle-sized in the environment
        (vol,) = [x for x in vols if x["name"] == "s2-lease"]
        token, trust = vol["projected"]["sources"]
        assert token["serviceAccountToken"]["path"] == "token"
        assert trust == {
            "configMap": {"name": "lease-example-trust", "items": [{"key": "ca-bundle.pem", "path": "ca-bundle.pem"}]}
        }
        assert {"name": "s2-lease", "mountPath": "/var/run/secrets/serverless2", "readOnly": True} in c["volumeMounts"]
    assert seen == 1
    # without native_lease the fleet's bundle is nobody's business: no ConfigMap, no variables
    for d in g.native(g._entry(PLAIN, test_api.FLEET), "hub"):
        assert d["kind"] != "ConfigMap"
    for _d, c, _v in _containers(g.native(g._entry(PLAIN, test_api.FLEET), "hub")):
        assert "SSL_CERT_FILE" not in _env(c) and "S2_CA_FILE" not in _env(c)
    # on a public fleet (no bundle): the token only, no file variables, no ConfigMap (API76 shape)
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: None)
    objs = g.native(g._entry(spec, test_api.FLEET), "hub")
    assert [d["kind"] for d in objs if d["kind"] == "ConfigMap"] == []
    for _d, c, vols in _containers(objs):
        assert not {"SSL_CERT_FILE", "S2_CA_FILE", "S2_CA_PEM"} & set(_env(c))
        (vol,) = [x for x in vols if x["name"] == "s2-lease"]
        assert len(vol["projected"]["sources"]) == 1
    # multi-node: leader and worker alike project the same file
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: pem)
    monkeypatch.setattr(mn.kube, "fleet", lambda: mn.POOLS)
    monkeypatch.setattr(mn.kube, "cluster_region", lambda c: c)
    e = models.to_entry({**mn.SPEC, "native_lease": "required"})
    objs = [
        d
        for v in models.variants(e, "us-central1")
        for d in render_endpoint.render(e, "us-central1", g.DOMAIN, g.IB_CLAIM_TEMPLATE, v, True)
    ]
    assert [d["kind"] for d in objs][0] == "ConfigMap" and len([d for d in objs if d["kind"] == "ConfigMap"]) == 1
    seen = 0
    for _d, c, vols in _containers(objs):
        seen += 1
        assert _env(c)["SSL_CERT_FILE"] == TRUST_FILE and "S2_CA_PEM" not in _env(c)
        (vol,) = [x for x in vols if x["name"] == "s2-lease"]
        assert vol["projected"]["sources"][1]["configMap"]["name"].endswith("-trust")
    assert seen == 2
    with pytest.raises(Exception, match="native_lease"):
        g._entry({**PLAIN, "native_lease": "always"}, test_api.FLEET)


def test_an_oversized_bundle_is_refused_whole_never_cut(monkeypatch):
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: _bundle(1_100_000))
    with pytest.raises(Exception, match="trust bundle"):
        g.native(g._entry({**PLAIN, "native_lease": "optional"}, test_api.FLEET), "hub")


def test_apply_and_delete_carry_the_trust_configmap(monkeypatch):
    """models.apply server-side applies the ConfigMap (core API, the API's field manager) before the pod's object and
    models.delete_rendered removes it with the rest; a 404 on delete is fine."""
    import kube

    pem = _bundle()
    monkeypatch.setattr(render_endpoint, "trust_bundle", lambda: pem)
    fake = test_api.FakeCluster()
    monkeypatch.setattr(kube, "core", lambda region="eu-north1": fake)
    monkeypatch.setattr(kube, "api", lambda region="eu-north1": fake)
    monkeypatch.setattr(models, "endpoint_domain", lambda region: g.DOMAIN)
    with g._fleet(test_api.FLEET):
        entry = models.to_entry({**PLAIN, "native_lease": "required"})
        applied = models.apply(entry, "hub")
        assert applied[0] == "ConfigMap/models/lease-example-trust"
        assert fake.configmaps[("models", "lease-example-trust")]["data"] == {"ca-bundle.pem": pem}
        assert ("models", "inferenceservices", "lease-example") in fake.custom
        gone = models.delete_rendered(entry, "hub")
    assert "ConfigMap/models/lease-example-trust" in gone and fake.configmaps == {}
    assert ("models", "inferenceservices", "lease-example") not in fake.custom


def test_pinned_client_trusts_the_projected_file_through_ssl_cert_file(tmp_path, monkeypatch):
    """The seam the fix relies on, end to end: the client bytes model images carry (unchanged, pinned by hash) call
    ssl.create_default_context(), which loads SSL_CERT_FILE; with the projected bundle named there, a hostname signed
    by the fleet's CA verifies, and without it the same call is refused (so the file, not luck, is what trusts)."""
    import http.server
    import importlib.util
    import json
    import shutil
    import ssl
    import subprocess
    import threading

    assert hashlib.sha256(CLIENT.read_bytes()).hexdigest() == CLIENT_SHA256
    if not shutil.which("openssl"):
        pytest.skip("openssl binary not available")
    key, crt = tmp_path / "ca.key", tmp_path / "ca.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(crt),
        ],
        check=True,
        capture_output=True,
    )
    # the bundle as the platform projects it: the full public roots first (nothing is replaced), the fleet CA appended
    import certifi

    bundle = tmp_path / "ca-bundle.pem"
    bundle.write_text(pathlib.Path(certifi.where()).read_text() + crt.read_text())

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"cancel_requested": False}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"https://localhost:{srv.server_address[1]}/internal/executions/op-1"
    spec = importlib.util.spec_from_file_location("s2lease_pinned", CLIENT)
    s2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s2)
    try:
        monkeypatch.delenv("S2_CA_PEM", raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
        s2._SSL = None
        assert s2._default_transport("GET", url, None, {}, 5) == (200, {"cancel_requested": False})
        # the public roots are still there (a mainstream root is in the file the context loaded)
        assert any(c.get("issuer") for c in s2._ssl_context().get_ca_certs())
        monkeypatch.delenv("SSL_CERT_FILE")
        s2._SSL = None
        with pytest.raises(Exception, match="CERTIFICATE_VERIFY_FAILED|certificate verify failed"):
            s2._default_transport("GET", url, None, {}, 5)
    finally:
        srv.shutdown()


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
