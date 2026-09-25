#!/usr/bin/env python3
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _pattern(prefix: str, suffix: str) -> re.Pattern[str]:
    # Keep prohibited literal shapes out of the scanner's own source.
    return re.compile(prefix + suffix, re.IGNORECASE)


PROHIBITED = {
    "live project ID": _pattern("project-", r"e00[a-z0-9]{15}"),
    "live cluster ID": _pattern("mk8scluster-", r"e00[a-z0-9]{15}"),
    "live node-group ID": _pattern("mk8snodegroup-", r"e00[a-z0-9]{15}"),
    "live compute-instance ID": _pattern("computeinstance-", r"e00[a-z0-9]{15}"),
    "live cluster API host": _pattern(
        r"pu\.mk8scluster-", r"e00[a-z0-9]{15}\.[a-z0-9.-]*nebius\.cloud"
    ),
    "private registry tenant": _pattern(
        r"cr\.[a-z0-9-]+\.nebius\.cloud/", r"e00[a-z0-9]+"
    ),
    "workstation home path": _pattern(r"/home/", r"[A-Za-z_][A-Za-z0-9_-]*"),
    "private key": _pattern(
        "-----BEGIN ",
        r"(?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----\r?\n"
        r"[0-9a-z+/=\r\n]{40,}\r?\n-----END ",
    ),
    "GitHub token": _pattern(r"gh", r"[pousr]_[0-9a-z]{30,}"),
    "Slack token": _pattern(r"xox", r"[baprs]-[0-9a-z-]{10,}"),
    "AWS access key": _pattern("AK", r"IA[0-9A-Z]{16}"),
}
ALLOWED_CONTAINER_HOMES = {"/home/nvs", "/home/user"}


class PublicSurfaceTests(unittest.TestCase):
    def test_subtree_has_no_live_operational_or_credential_literals(self) -> None:
        self.assertIsNotNone(
            PROHIBITED["live cluster ID"].fullmatch(
                "mk8scluster-" + "e00" + "a" * 15
            )
        )
        self.assertIsNotNone(
            PROHIBITED["workstation home path"].fullmatch(
                "/home/" + "fixture-user"
            )
        )
        findings: list[str] = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            for label, pattern in PROHIBITED.items():
                matches = list(pattern.finditer(text))
                if label == "workstation home path":
                    matches = [
                        match
                        for match in matches
                        if match.group(0) not in ALLOWED_CONTAINER_HOMES
                    ]
                if matches:
                    findings.append(f"{path.relative_to(ROOT)}: {label}")
        self.assertEqual(findings, [], "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
