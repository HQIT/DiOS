"""Local HTTP provider used by real DSH; no external access or model credential."""

import asyncio
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import time

from dios_sdk.dsh import dsh_execute
from dios_sdk.protocol import WorkloadError, emit


class Endpoint(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions"
        assert request["model"] == "ecnu-max"
        outage = Path("/run/fixture/outage")
        if sys.argv[1] == "account-a" and outage.exists():
            code = {
                "PROVIDER_RATE_LIMITED": "rate_limit_exceeded",
                "PROVIDER_QUOTA_EXHAUSTED": "insufficient_quota",
            }[outage.read_text()]
            body = json.dumps(
                {
                    "error": {
                        "code": code,
                        "type": code,
                        "message": "Synthetic provider refusal",
                    }
                }
            ).encode()
            emit(
                {
                    "type": "output",
                    "text": json.dumps({"provider_http": 429, "code": code}),
                }
            )
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(body)
            return
        deadline = time.monotonic() + 60
        while not Path("/run/fixture/release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Synthetic provider barrier expired")
            time.sleep(0.05)
        emit({"type": "output", "text": json.dumps({"provider_http": 200})})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        result = {
            "summary": "合成任务已完成",
            "evidence": ["http-provider:" + os.environ["DIOS_ATTEMPT_ID"]],
        }
        for delta, finish in (
            (
                {
                    "role": "assistant",
                    "content": json.dumps(result, ensure_ascii=False),
                },
                None,
            ),
            ({}, "stop"),
        ):
            chunk = {
                "id": "synthetic",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "ecnu-max",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


async def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    patch = (
        Path("/opt/dsh-profile.yml")
        .read_text()
        .replace(
            "https://chat.ecnu.edu.cn/open/api/v1",
            f"http://127.0.0.1:{server.server_port}/v1",
        )
    )
    Path("/tmp/model.patch.yml").write_text(patch)
    command = [
        "/usr/local/bin/node",
        "/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js",
        "--profile",
        "acp",
        "--patch",
        "/tmp/model.patch.yml",
        "--patch",
        "/opt/dios/dsh-native-guard.patch.yml",
    ]
    config = Path("/tmp/provider-acp.json")
    config.write_text(
        json.dumps({"command": command, "cwd": "/tmp", "prompt_timeout": 120})
    )
    try:
        return await dsh_execute(
            config, "Return the synthetic report.", lambda value: value
        )
    except WorkloadError as error:
        emit({"type": "error", "code": error.code})
        return 7
    finally:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
