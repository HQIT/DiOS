"""Private loopback MCP endpoint with observable read and outbound effects."""

import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(sys.argv[1])
lock = threading.Lock()


def record(value):
    with lock, (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value=None):
        body = b"" if value is None else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/sink":
            record({"effect": "outbound", "value": value})
            return self.reply(200, {"received": True})
        if self.path != "/mcp":
            return self.reply(404, {})
        method = value["method"]
        record({"method": method, "id": value.get("id"), "params": value.get("params")})
        if method == "initialize":
            if (root / "hold").exists():
                record({"barrier": "initialize"})
                deadline = time.monotonic() + 15
                while not (root / "release").exists():
                    if time.monotonic() >= deadline:
                        return self.reply(504, {})
                    time.sleep(0.01)
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "revocation-fixture", "version": "1"},
            }
        elif method == "notifications/initialized":
            return self.reply(202)
        elif method == "tools/call":
            params = value["params"]
            if params["name"] == "read":
                result_text = (root / "source.txt").read_text(encoding="utf-8")
                record({"effect": "read", "text": result_text})
            elif params["name"] == "send":
                payload = json.dumps(params["arguments"]).encode()
                request = urllib.request.Request(
                    f"http://127.0.0.1:{self.server.server_port}/sink",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    request, timeout=3
                ) as response:
                    result_text = response.read().decode()
            else:
                return self.reply(400, {})
            result = {
                "content": [{"type": "text", "text": result_text}],
                "isError": False,
            }
        else:
            return self.reply(400, {})
        self.reply(200, {"jsonrpc": "2.0", "id": value["id"], "result": result})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(json.dumps({"port": server.server_port, "pid": os.getpid()}), flush=True)
server.serve_forever()
