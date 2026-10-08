"""The Studio client names itself. Hosted Studio sits behind Cloudflare, whose
Browser Integrity Check answers Python's default `Python-urllib/3.x` signature
with 403 (error code 1010) before the request reaches Studio, so a connection
to https://driftstudio.dev/api failed on every call while loopback Studio
worked. Runs real requests against a local server and reads the header sent."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from shared import studio_client
from shared.studio_client import StudioClient


class StudioClientUserAgentTests(unittest.TestCase):
    def setUp(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self):
                seen.append({"ua": self.headers.get("User-Agent", ""),
                             "auth": self.headers.get("Authorization", "")})
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _reply
            do_POST = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.client = StudioClient()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def _call(self, method, payload=None, token=""):
        with patch.object(self.client, "api_base", return_value=self.base), \
                patch.object(self.client, "token", return_value=token):
            return self.client.request(method, "/api/health", payload=payload)

    def test_get_and_post_send_the_assist_user_agent(self):
        self.assertEqual(self._call("GET")[0], 200)
        self.assertEqual(self._call("POST", payload={"x": 1})[0], 200)
        for sent in self.seen:
            self.assertEqual(sent["ua"], studio_client.USER_AGENT)
            self.assertTrue(sent["ua"].startswith("drift-assist"))

    def test_default_python_signature_is_never_sent(self):
        self._call("GET")
        self.assertNotIn("Python-urllib", self.seen[0]["ua"])

    def test_bearer_token_still_sent_alongside(self):
        self._call("GET", token="secret-value")
        self.assertEqual(self.seen[0]["auth"], "Bearer secret-value")
        self.assertEqual(self.seen[0]["ua"], studio_client.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
