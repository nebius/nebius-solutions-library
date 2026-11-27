import os
import re
import ssl
import subprocess
import sys
from pathlib import Path

import certifi
import pytest

import tls_trust

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/ca-trust"


@pytest.fixture
def stores(tmp_path, monkeypatch):
    safe = Path(certifi.where()).read_text()
    system = tmp_path / "system.pem"
    system.write_text(safe)
    extra = tmp_path / "private"
    extra.mkdir()
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", safe, re.DOTALL)
    (extra / "fleet-ca.pem").write_text(certificates[0])
    output = tmp_path / "merged.pem"
    monkeypatch.setattr(tls_trust, "SYSTEM_BUNDLE", system)
    monkeypatch.setattr(tls_trust, "EXTRA_DIRECTORY", extra)
    monkeypatch.setattr(tls_trust, "OUTPUT_BUNDLE", output)
    monkeypatch.setattr(tls_trust, "_state", {"stamp": None, "explicit": None})
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    return system, extra, output, safe, certificates


def test_patched_system_does_not_reimport_stale_certifi(stores, monkeypatch):
    system, extra, output, safe, certificates = stores
    monkeypatch.setattr(certifi, "where", lambda: str(FIXTURES / "emsign-root.pem"))
    tls_trust.configure_trust()
    tls_trust.validate_bundle(output.read_text())
    assert os.environ["SSL_CERT_FILE"] == str(output)


def test_custom_bundle_is_authoritative(stores):
    system, extra, output, safe, certificates = stores
    system.write_text((FIXTURES / "emsign-root.pem").read_text())
    (extra / "ca.pem").write_text(certificates[1])
    # Terraform's aggregate must not be re-added after choosing ca.pem.
    (extra / "bundle.pem").write_text((FIXTURES / "aaa-root.pem").read_text())
    tls_trust.configure_trust()
    expected = {ssl.PEM_cert_to_DER_cert(c) for c in certificates[:2]}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(output))
    assert set(context.get_ca_certs(binary_form=True)) == expected


@pytest.mark.parametrize("root", ["emsign-root.pem", "aaa-root.pem"])
def test_rejects_both_compromised_signing_paths(stores, root):
    system, extra, output, safe, certificates = stores
    system.write_text(safe + (FIXTURES / root).read_text())
    with pytest.raises(ValueError, match="distrusted"):
        tls_trust.configure_trust()
    assert not output.exists()
    assert "SSL_CERT_FILE" not in os.environ


def test_refresh_tracks_public_roots_and_fails_closed(stores):
    system, extra, output, safe, certificates = stores
    tls_trust.configure_trust()
    original = output.read_text()
    system.write_text(safe + (FIXTURES / "aaa-root.pem").read_text())
    for _ in range(2):
        with pytest.raises(ValueError, match="distrusted"):
            tls_trust.configure_trust(refresh=True)
    assert output.read_text() == original
    system.write_text(certificates[1])
    tls_trust.configure_trust(refresh=True)
    assert output.read_text() != original


def test_explicit_store_remains_authoritative_on_refresh(stores, monkeypatch, tmp_path):
    system, extra, output, safe, certificates = stores
    explicit = tmp_path / "explicit.pem"
    explicit.write_text(certificates[1])
    monkeypatch.setenv("SSL_CERT_FILE", str(explicit))
    tls_trust.configure_trust()
    before = output.read_text()
    system.write_text((FIXTURES / "aaa-root.pem").read_text())
    tls_trust.configure_trust(refresh=True)
    assert output.read_text() == before
    explicit.write_text((FIXTURES / "emsign-root.pem").read_text())
    with pytest.raises(ValueError, match="distrusted"):
        tls_trust.configure_trust(refresh=True)


def test_removing_last_private_ca_removes_its_trust(stores):
    system, extra, output, safe, certificates = stores
    system.write_text(certificates[1])
    tls_trust.configure_trust()
    (extra / "fleet-ca.pem").unlink()
    tls_trust.configure_trust(refresh=True)
    assert output.read_text().strip() == certificates[1]


@pytest.mark.parametrize(
    "pem", ["", "not a certificate", "-----BEGIN CERTIFICATE-----\nbroken\n-----END CERTIFICATE-----"]
)
def test_invalid_bundle_fails(pem):
    with pytest.raises((ValueError, ssl.SSLError)):
        tls_trust.validate_bundle(pem)


def test_offline_chain_gate_accepts_patched_store(stores):
    system, extra, output, safe, certificates = stores
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/check-ca-trust.py"), str(system)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all 12 compromised chains rejected" in result.stdout


def test_offline_chain_gate_rejects_stale_store(stores):
    system, extra, output, safe, certificates = stores
    system.write_text(safe + (FIXTURES / "aaa-root.pem").read_text())
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/check-ca-trust.py"), str(system)], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "distrusted AAA Certificate Services" in result.stderr
