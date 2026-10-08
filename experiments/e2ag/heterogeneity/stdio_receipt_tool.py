"""Permissive local MCP tool: records all calls, never hides a missed PEP check."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def append(path, record):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--fault", default="none")
    args = parser.parse_args()
    directory = args.directory
    append(directory / "stdio-processes.jsonl", {"pid": os.getpid(), "phase": "started"})
    initialized = False
    try:
        for raw in sys.stdin.buffer:
            payload = json.loads(raw.decode("utf-8"))
            append(directory / "stdio-messages.jsonl", {"pid": os.getpid(), "payload": payload})
            method = payload["method"]
            if method == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "isolated-receipt", "version": "1"}}
            elif method == "notifications/initialized":
                initialized = True
                continue
            elif not initialized:
                raise RuntimeError("NOT_INITIALIZED")
            elif method == "tools/list":
                result = {"tools": [{"name": name, "inputSchema": {"type": "object"}}
                                    for name in ["report.write", "other.write"]]}
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                params = payload["params"]
                append(directory / "tool-receipts.jsonl", {"method": method, **params})
                append(directory / "artifacts.jsonl", params)
                if args.fault == "exit_after_write":
                    os._exit(71)
                if args.fault == "timeout_after_write":
                    time.sleep(5)
                if args.fault == "malformed_response":
                    sys.stdout.buffer.write(b"not JSON\n"); sys.stdout.buffer.flush()
                    continue
                result = {"content": [{"type": "text", "text": "stored"}]}
            else:
                raise RuntimeError("UNSUPPORTED_METHOD")
            response_id = payload.get("id")
            if args.fault == "wrong_id" and method == "tools/call":
                response_id = "not-the-request-id"
            response = {"jsonrpc": "2.0", "id": response_id, "result": result}
            # Logging on stderr is valid and must not corrupt the message pipe.
            print("receipt tool handled " + method, file=sys.stderr, flush=True)
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
    finally:
        append(directory / "stdio-processes.jsonl", {"pid": os.getpid(), "phase": "exited"})


if __name__ == "__main__":
    main()
