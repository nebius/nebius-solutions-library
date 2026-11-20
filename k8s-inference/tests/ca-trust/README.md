# CA trust regression fixtures

Public certificates only; no private keys are included.

The twelve chains were published at <https://emudhra.we-have-a-problem.com/chains.zip>
and captured on October 22, 2026. The archive SHA-256 is
`8a2edc81b6c51e3ad27d45ca1e441ec84c961e2dee0fc547f2a02c0f74315f95`.
The two trust anchors come from <https://emudhra.we-have-a-problem.com/roots.json>.
`SHA256SUMS` records the individual PEM files.

Run `python3 tools/check-ca-trust.py /path/to/bundle.pem` from the solution directory.
The check rejects known compromised certificates and anchors, verifies a positive
control from the supplied bundle, and rejects all twelve TLS chains. The negative
checks use a fixed verification time within the leaves' validity period so expiry
cannot hide a trust-store regression. No network access is required.
