"""Submit one fixed out-of-scope capability request to the task MCP PEP.

This is a deterministic governance-regression vector.  A test harness, not a
model, selects the tool name and arguments.  The helper reads the ephemeral
task-scoped proxy configuration and emits only the authorization outcome; it
never prints the proxy URL or bearer token.  The expected result is a local
PEP denial before the upstream service is reached.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


OUT_OF_SCOPE_TOOL = "mark_effective_commit"
CANARY_REPO = "HQIT/e2ag-canary"
CANARY_AFTER_SHA = "82e6cef3a7a609fd1cc302e94d887ebfddc050f6"


def _load_proxy_connection() -> tuple[str, dict[str, str]]:
    task_config_path = Path(os.environ["TASK_CONFIG"])
    task_config = json.loads(task_config_path.read_text(encoding="utf-8"))
    mcp_path = Path(task_config["task"]["mcp_config_path"])
    mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
    if len(mcp_config) != 1:
        raise RuntimeError("expected exactly one task-scoped MCP connection")
    connection = next(iter(mcp_config.values()))
    return str(connection["url"]), {
        str(key): str(value) for key, value in connection.get("headers", {}).items()
    }


def main() -> int:
    url, headers = _load_proxy_connection()
    request_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "external-governance-regression",
            "method": "tools/call",
            "params": {
                "name": OUT_OF_SCOPE_TOOL,
                "arguments": {
                    "repo": CANARY_REPO,
                    "after_sha": CANARY_AFTER_SHA,
                    "effective": True,
                },
            },
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request_headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(
        url,
        data=request_body,
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status_code = response.status
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        body = json.loads(exc.read().decode("utf-8"))

    reason = (((body.get("error") or {}).get("data") or {}).get("reason"))
    result = {
        "status_code": status_code,
        "reason": reason,
        "requested_tool": OUT_OF_SCOPE_TOOL,
        "canary_repo": CANARY_REPO,
        "canary_after_sha": CANARY_AFTER_SHA,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if status_code == 403 and reason == "MCP_TOOL_NOT_GRANTED" else 1


if __name__ == "__main__":
    sys.exit(main())
