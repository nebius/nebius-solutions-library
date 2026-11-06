"""Build the API trust file without restoring roots removed from another store."""

import hashlib
import os
import re
import ssl
import tempfile
import threading
from pathlib import Path

SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
EXTRA_DIRECTORY = Path("/etc/ssl/msp")
OUTPUT_BUNDLE = Path("/tmp/trust.pem")
# Public certificate fingerprints from https://emudhra.we-have-a-problem.com/
# Includes both cross-signing paths and the compromised intermediate certificates.
DISTRUSTED_CERTIFICATES = {
    "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d": "emSign Root TLS CA - G3",
    "f2edaaff3d9b701fb4d14c9f285f8c0bad90fc3bcc1fa07d47f4718edaba34a5": "emSign Root CS CA - G3",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4": "AAA Certificate Services",
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b": "emSign ECC Root CA - G3",
    "42c4accd12c4f59597925e1a08a7a90102608543809308d50ec427e118ba1493": "EM DV TLS CA - G3B",
    "45a883f349aedfd5f3a649b5498ffb121712ba8b448f3f2a91dbc5a766fcddbe": "EM DV TLS CA - G3B",
    "98b896625c0c230adeec3eb2cf6aad5001ecf9156a68644e82374ce5833bc25b": "EM OV TLS CA - G3B",
    "5f1ded4dd590af74631290cf073b122a2aac6a1fa479778a0bd43579bd832699": "EM OV TLS CA - G3B",
    "e7568fe6e12331f4fd7de61da4c7559d0f8eb192a218d4741a3ba75184333838": "EM EV TLS CA - G3B",
    "579480aea166290bd3c643589e795fc0d0e54e86505d180f1dff99efd3199512": "EM EV TLS CA - G3B",
    "0164d7ceae5bb909c272af151ff44b1177ae9b47d9424b94f1d9558c30934d19": "EM DV TLS CA - G3C",
    "62b7f9868263f72f6911fc78ceaf72c1979158b7368bf648683bb2aa39b02e72": "EM DV TLS CA - G3C",
    "cb6e15165ac802e9b3f993f16c25d41f4e81c21ebde71ae1b012ac00bf9c7c3f": "EM OV TLS CA - G3C",
    "7485b51c0ed192878fa1d2f948fb529ae1842cc5086c4b67f7be5b2039de9489": "EM OV TLS CA - G3C",
    "432ec25f0d28de9006d97bf752f17bbb2cd41f669933fcb072d9a1148d0261f3": "EM EV TLS CA - G3C",
    "fd9d5c2dbbd4995ab69b8d50c439d9527b00529199c2fd01b2174eb58f07021f": "EM EV TLS CA - G3C",
    "9e39de2881273acc225a149c72ba3e0f51424963669ab633ee1dc7df5db067b4": "EM OV CS CA - G3B",
    "a496e23ac0cab8155fc9b117b97eae368efcbb9041aec8804d5a308d2d01c2d8": "EM OV CS CA - G3B",
    "4b2460a466f95fd40113bcdcca2ceb29b3b093921de45474da7df48874c630b1": "EM OV CS CA - G3C",
    "84112972b8e59d14560ae7e7119654474aad119990cf3722bfc1e3b91615e1fd": "EM OV CS CA - G3C",
}
_CERTIFICATE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)
_state = {"stamp": None, "explicit": None}
_lock = threading.Lock()


def validate_bundle(pem: str) -> None:
    """Reject empty, malformed or known-compromised trust stores before use."""
    certificates = _CERTIFICATE.findall(pem)
    if not certificates:
        raise ValueError("Trust bundle contains no PEM certificates")
    for certificate in certificates:
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
        if fingerprint in DISTRUSTED_CERTIFICATES:
            raise ValueError(
                f"Trust bundle contains distrusted {DISTRUSTED_CERTIFICATES[fingerprint]}; update its source"
            )
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=pem)


def configure_trust(refresh: bool = False) -> None:
    """Use one public store plus mounted private CAs; an explicit store takes precedence.

    ca.pem is the complete operator-supplied bundle. bundle.pem is Terraform's
    aggregate of the same mounted files, so neither is appended a second time.
    """
    with _lock:
        explicit = os.environ.get("SSL_CERT_FILE")
        if explicit == str(OUTPUT_BUNDLE):
            explicit = _state["explicit"]
        extra = sorted(p for p in EXTRA_DIRECTORY.glob("*.pem") if p.name not in {"ca.pem", "bundle.pem"})
        custom = EXTRA_DIRECTORY / "ca.pem"
        if not extra and not custom.exists() and not explicit and _state["stamp"] is None:
            return
        if explicit:
            public = Path(explicit)
        elif custom.exists():
            public = custom
        elif SYSTEM_BUNDLE.exists():
            public = SYSTEM_BUNDLE
        else:
            import certifi

            public = Path(certifi.where())
        sources = list(dict.fromkeys([public, *extra]))
        stamp = tuple((str(p), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_size) for p in sources)
        if refresh and stamp == _state["stamp"] and OUTPUT_BUNDLE.exists():
            return
        pem = "\n".join(p.read_text().strip() for p in sources) + "\n"
        validate_bundle(pem)
        # A failed refresh leaves the previous complete file intact and raises;
        # callers must not continue the outbound request with obsolete trust.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=OUTPUT_BUNDLE.parent, prefix="trust-", delete=False) as out:
                temporary = Path(out.name)
                out.write(pem)
            os.replace(temporary, OUTPUT_BUNDLE)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        os.environ["SSL_CERT_FILE"] = str(OUTPUT_BUNDLE)
        _state.update(stamp=stamp, explicit=explicit)
