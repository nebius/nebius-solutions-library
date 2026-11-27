"""Exercise Keycloak HTTPS locally without sending real credentials."""

import http.server
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "lib/tls.sh"


class Handler(http.server.BaseHTTPRequestHandler):
    requests = 0

    def do_POST(self):
        type(self).requests += 1
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"verified")

    def log_message(self, *args):
        pass


class KeycloakTLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="keycloak tls ")
        root = Path(cls.directory.name)
        cls.ca = root / "local ca.pem"
        key = root / "key.pem"
        config = root / "openssl.cnf"
        config.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
            "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,digitalSignature,keyCertSign\n"
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(config),
                "-keyout",
                str(key),
                "-out",
                str(cls.ca),
            ],
            check=True,
            capture_output=True,
        )
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.ca, key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://127.0.0.1:{cls.server.server_port}/token"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def request(self, **settings):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "OSMO_KC_CA_BUNDLE",
                "OSMO_KC_SKIP_TLS_VERIFY",
                "CURL_CA_BUNDLE",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            }
        }
        env.update(settings)
        return subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; shift; keycloak_curl "$@"',
                "test",
                str(HELPER),
                "-sS",
                "--noproxy",
                "*",
                "--max-time",
                "5",
                "--data",
                "test-password",
                self.url,
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_untrusted_ca_rejected_before_post(self):
        before = Handler.requests
        result = self.request()
        self.assertEqual(result.returncode, 60, result.stderr)
        self.assertEqual(Handler.requests, before)

    def test_explicit_private_ca_accepted(self):
        result = self.request(OSMO_KC_CA_BUNDLE=str(self.ca))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "verified")

    def test_insecure_flag_refused(self):
        before = Handler.requests
        result = self.request(OSMO_KC_SKIP_TLS_VERIFY="true")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(Handler.requests, before)

    def test_missing_bundle_refused(self):
        result = self.request(OSMO_KC_CA_BUNDLE=str(self.ca) + ".missing")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
