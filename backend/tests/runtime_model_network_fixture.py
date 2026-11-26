"""Synthetic model chunks served over HTTP; all network faults belong to Toxiproxy."""

import json
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PARTS = ['{"summary":"首段中文🙂', '，恢复完成","evidence":["fixture:source"]}']


def stream(sid):
    # A synthetic Harness can reconnect its read-only stream without another prompt.
    # This does not assert that a proprietary Harness has the same retry policy.
    journal = Path("/run/qualification")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for index in range(2):
        if index:
            while not (journal / "continue").exists():
                time.sleep(0.05)
        while True:
            try:
                with opener.open(
                    os.environ["MODEL_FIXTURE_URL"] + f"/{index}", timeout=2
                ) as response:
                    text = response.read().decode("utf-8")
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                with (journal / "connection-failures").open("a") as output:
                    output.write(str(time.time()) + "\n")
                time.sleep(0.25)
        print(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": sid,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": text},
                        },
                    },
                }
            ),
            flush=True,
        )


class Model(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/0", "/1"):
            self.send_error(404)
            return
        data = PARTS[int(self.path[1:])].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Model).serve_forever()
