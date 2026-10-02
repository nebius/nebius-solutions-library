from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _DeleteHandler(BaseHTTPRequestHandler):
    received: dict[str, object] = {}

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks: list[bytes] = []
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            body = b"".join(chunks)
        else:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
        type(self).received = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(body),
        }
        payload = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Status",
                "status": "Success",
                "code": 200,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class KubectlRawDeleteTests(unittest.TestCase):
    def test_runner_refuses_without_explicit_coordination_gate(self) -> None:
        environment = os.environ.copy()
        environment.pop("OPENFOLD2_NEWNODE_COORDINATED", None)
        completed = subprocess.run(
            [
                str(Path(__file__).resolve().parents[1] / "run_newnode_benchmark.sh"),
                "offline-gate-test",
                "--execute",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        self.assertEqual(completed.returncode, 78)
        self.assertIn("explicit live handoff", completed.stderr)

    def test_filename_body_carries_uid_precondition_on_delete(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _DeleteHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                kubeconfig = root / "kubeconfig"
                options = root / "delete-options.json"
                kubeconfig.write_text(
                    "\n".join(
                        [
                            "apiVersion: v1",
                            "kind: Config",
                            "clusters:",
                            "- name: local",
                            "  cluster:",
                            f"    server: http://127.0.0.1:{server.server_port}",
                            "contexts:",
                            "- name: local",
                            "  context:",
                            "    cluster: local",
                            "    user: local",
                            "current-context: local",
                            "users:",
                            "- name: local",
                            "  user: {}",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                uid = "11111111-2222-3333-4444-555555555555"
                options.write_text(
                    json.dumps(
                        {
                            "apiVersion": "v1",
                            "kind": "DeleteOptions",
                            "propagationPolicy": "Foreground",
                            "preconditions": {"uid": uid},
                        }
                    ),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        "kubectl",
                        "--kubeconfig",
                        str(kubeconfig),
                        "--context",
                        "local",
                        "delete",
                        "--raw",
                        "/api/v1/namespaces/ns/pods/example",
                        "-f",
                        str(options),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(
                    _DeleteHandler.received["path"],
                    "/api/v1/namespaces/ns/pods/example",
                )
                self.assertEqual(
                    _DeleteHandler.received["body"],
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "propagationPolicy": "Foreground",
                        "preconditions": {"uid": uid},
                    },
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
