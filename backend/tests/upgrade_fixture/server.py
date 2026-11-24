"""Synthetic streaming protocol fixture; no model, credentials or business data."""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n')
        self.wfile.flush()
        for _ in range(600):
            if Path("/workspace/release").exists():
                break
            time.sleep(0.1)
        if "truncated" not in str(body):
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
