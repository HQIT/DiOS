"""HTTP client refuses truncated object bytes and preserves mutation identity."""

import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dios_sdk import TaskClient, WorkloadError


class ObjectClientTests(unittest.TestCase):
    def test_roundtrip_and_truncated_response_integrity(self):
        data = "中文🙂 artifact".encode()
        received = []
        truncated = False

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.endswith("/download"):
                    value = data[:-1] if truncated else data
                else:
                    value = json.dumps(
                        {
                            "descriptor": {
                                "size": len(data),
                                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                            }
                        }
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

            def do_PUT(self):
                received.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"revision":1}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = TaskClient(
                "http://127.0.0.1:" + str(server.server_port), "test-token"
            )
            self.assertEqual(client.download_object("file", "id"), data)
            truncated = True
            with self.assertRaisesRegex(WorkloadError, "ARTIFACT_DIGEST_MISMATCH"):
                client.download_object("file", "id")
            self.assertEqual(
                client.set_object_policy(
                    "file", "id", [], expected_revision=0, idempotency_key="same"
                ),
                {"revision": 1},
            )
            self.assertEqual(
                received,
                [{"grants": [], "expected_revision": 0, "idempotency_key": "same"}],
            )
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
