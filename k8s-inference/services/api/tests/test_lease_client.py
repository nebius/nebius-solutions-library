"""The thin shared worker client (services/lease-client/s2lease.py): attach before any GPU enqueue, release after the
worker's own settlement, same-tuple retries on transport errors only, fail closed on a refusal. Exercised against a fake
transport; the API side of the protocol is tests/test_leases.py and test_database.py."""

import importlib.util
import pathlib
import urllib.error

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "lease-client" / "s2lease.py"
spec = importlib.util.spec_from_file_location("s2lease", SRC)
s2lease = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s2lease)


class Transport:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((method, url, body, headers))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture
def token(tmp_path):
    f = tmp_path / "token"
    f.write_text("tok-1\n")
    return str(f)


def test_attach_release_round_trip(token, monkeypatch):
    t = Transport(
        [
            (
                200,
                {
                    "operation": "op-1",
                    "attempt": "a1",
                    "state": "attached",
                    "owner": {"tenant": "demo", "owner": "k1", "model": "m"},
                },
            ),
            (200, {"state": "released"}),
        ]
    )
    monkeypatch.setattr(s2lease.time, "sleep", lambda s: None)
    lease = s2lease.Lease("https://api.example/", token, "optional", transport=t)
    exe = lease.attach({"X-Serverless2-Operation": "op-1", "x-serverless2-attempt": "a1"}, worker_request_id="w-1")
    assert exe.owner == {"tenant": "demo", "owner": "k1", "model": "m"}
    assert exe.release("done", gpu_seconds=None)["state"] == "released"
    m, url, body, headers = t.calls[0]
    assert (m, url, body) == (
        "POST",
        "https://api.example/internal/executions/op-1:attach",
        {"attempt": "a1", "worker_request_id": "w-1"},
    )
    assert headers == {"Authorization": "Bearer tok-1"}
    assert t.calls[1][1:3] == (
        "https://api.example/internal/executions/op-1:release",
        {"attempt": "a1", "worker_request_id": "w-1", "outcome": "done", "gpu_seconds": None},
    )


def test_transport_errors_retry_the_same_tuple_and_a_refusal_is_final(token, monkeypatch):
    monkeypatch.setattr(s2lease.time, "sleep", lambda s: None)
    t = Transport([urllib.error.URLError("reset"), OSError("timeout"), (200, {"state": "attached"})])
    lease = s2lease.Lease("https://api.example", token, transport=t)
    assert lease.attach({"x-serverless2-operation": "op-1", "x-serverless2-attempt": "a1"}, "w-1") is not None
    assert len({c[2]["worker_request_id"] for c in t.calls}) == 1 and len(t.calls) == 3
    t = Transport([(409, {"detail": "native executions of op-1 are over (the caller finished and the join decided)"})])
    with pytest.raises(s2lease.LeaseRefused, match="are over"):
        s2lease.Lease("https://api.example", token, transport=t).attach(
            {"x-serverless2-operation": "op-1", "x-serverless2-attempt": "a1"}, "w-1"
        )
    assert len(t.calls) == 1  # a refusal is never retried
    t = Transport([OSError("down")] * 5)
    with pytest.raises(s2lease.LeaseUnavailable):
        s2lease.Lease("https://api.example", token, transport=t).attach(
            {"x-serverless2-operation": "op-1", "x-serverless2-attempt": "a1"}, "w-1"
        )


def test_direct_calls_carry_no_lease(token):
    t = Transport([])
    assert s2lease.Lease("https://api.example", token, "optional", transport=t).attach({}, "w-1") is None
    with pytest.raises(s2lease.LeaseRefused, match="required"):
        s2lease.Lease("https://api.example", token, "required", transport=t).attach(
            {"x-serverless2-operation": "op-1"}, "w-1"
        )
    assert t.calls == []


def test_from_env_and_cancel_poll(token, monkeypatch):
    monkeypatch.delenv("S2_API_URL", raising=False)
    assert s2lease.Lease.from_env() is None
    monkeypatch.setenv("S2_API_URL", "https://api.example")
    monkeypatch.setenv("S2_LEASE_TOKEN_FILE", token)
    monkeypatch.setenv("S2_LEASE_MODE", "required")
    t = Transport([(200, {"state": "attached"}), (200, {"state": "attached", "cancel_requested": True}), (503, {})])
    lease = s2lease.Lease.from_env(transport=t)
    assert lease.mode == "required" and lease.api_url == "https://api.example"
    exe = lease.attach({"x-serverless2-operation": "op-1", "x-serverless2-attempt": "a1"}, "w-1")
    assert exe.cancel_requested() is True
    assert exe.cancel_requested() is False  # unknown on a failed read: the worker keeps going
    assert t.calls[1][:2] == ("GET", "https://api.example/internal/executions/op-1")


def test_ssl_context_adds_the_projected_bundle_to_the_system_roots(monkeypatch, tmp_path):
    """A projected fleet CA is added to the system roots, never replacing them: S2_API_URL is publicly signed on a public
    fleet while the bundle may still be present (the async caller gets the same bundle)."""
    import subprocess

    system = len(s2lease.ssl.create_default_context().get_ca_certs())
    key, crt = tmp_path / "ca.key", tmp_path / "ca.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(crt),
            "-days",
            "2",
            "-subj",
            "/CN=serverless2 test fleet CA",
        ],
        check=True,
        capture_output=True,
    )
    s2lease._SSL = None
    monkeypatch.setenv("S2_CA_PEM", crt.read_text())
    ctx = s2lease._ssl_context()
    assert ctx.verify_mode == s2lease.ssl.CERT_REQUIRED
    assert len(ctx.get_ca_certs()) == system + 1  # the public roots stay, the fleet CA is one more
    assert any("serverless2 test fleet CA" in str(c.get("subject")) for c in ctx.get_ca_certs())
    s2lease._SSL = None
    monkeypatch.setenv("S2_CA_PEM", "not a certificate")
    assert len(s2lease._ssl_context().get_ca_certs()) == system  # garbage is ignored, the system roots stay
    s2lease._SSL = None
    monkeypatch.delenv("S2_CA_PEM")
    assert len(s2lease._ssl_context().get_ca_certs()) == system
    s2lease._SSL = None
