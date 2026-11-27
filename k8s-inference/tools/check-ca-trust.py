#!/usr/bin/env python3
"""Check a PEM trust store against the October 2026 compromised CA paths, offline."""

import argparse
import hashlib
import re
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))
from tls_trust import validate_bundle  # noqa: E402


def check(bundle: Path) -> None:
    pem = bundle.read_text()
    validate_bundle(pem)
    fixtures = ROOT / "tests/ca-trust"
    for line in (fixtures / "SHA256SUMS").read_text().splitlines():
        expected, name = line.split()
        if hashlib.sha256((fixtures / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Certificate fixture checksum mismatch: {name}")
    chains = sorted(fixtures.glob("*-emudhra.we-have-a-problem.com.pem"))
    if len(chains) != 12:
        raise ValueError("Expected all twelve compromised certificate chains")
    with tempfile.TemporaryDirectory() as temporary:
        empty = Path(temporary) / "empty"
        empty.mkdir()
        flags = ["-CAfile", str(bundle.resolve()), "-CApath", str(empty)]
        help_text = subprocess.run(["openssl", "verify", "-help"], capture_output=True, text=True)
        if "-no-CAstore" in help_text.stdout + help_text.stderr:
            flags += ["-no-CAstore"]
        # Prove that this verifier can accept a legitimate trust anchor. This
        # prevents a broken executable/store from making all negative cases pass.
        anchors = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.DOTALL)
        control = Path(temporary) / "control.pem"
        for anchor in anchors:
            control.write_text(anchor + "\n")
            result = subprocess.run(["openssl", "verify", *flags, str(control)], capture_output=True, text=True)
            if result.returncode == 0:
                break
        else:
            raise ValueError("No positive trust-anchor control verified")
        for chain in chains:
            # Frozen within the test leaves' validity period; future expiry must
            # not turn an unsafe store into a passing test.
            result = subprocess.run(
                [
                    "openssl",
                    "verify",
                    *flags,
                    "-attime",
                    "1792584000",
                    "-purpose",
                    "sslserver",
                    "-untrusted",
                    str(chain),
                    str(chain),
                ],
                capture_output=True,
                text=True,
            )
            detail = result.stdout + result.stderr
            if result.returncode == 0:
                raise ValueError(f"Still trusts compromised chain: {chain.name}")
            if not re.search(r"error (20|21) at", detail):
                raise ValueError(f"Unexpected verification failure for {chain.name}: {detail.strip()}")
    print(f"PASS {bundle.name}: positive control accepted; all 12 compromised chains rejected")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    try:
        check(args.bundle)
    except (OSError, ValueError, ssl.SSLError) as error:
        parser.exit(1, f"FAIL {args.bundle.name}: {error}\n")
