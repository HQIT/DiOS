"""Codex ACP 2.11.0 MCP metadata mapping; execution authority stays in DiOS."""

import re

from .acp import execute
from .acp_result import final_object
from .protocol import WorkloadError


def proxy_permission(call):
    raw = call.get("rawInput")
    if (
        not isinstance(raw, dict)
        or set(raw) != {"server", "tool", "arguments"}
        or raw["server"] != "dios"
        or not isinstance(raw["tool"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", raw["tool"])
        or not isinstance(raw["arguments"], dict)
    ):
        raise WorkloadError("ACP_NATIVE_OPERATION_DENIED")
    return {"name": "mcp__dios__" + raw["tool"], "arguments": raw["arguments"]}


def tool_activity(call):
    if call.get("status") not in {"completed", "failed"}:
        raise WorkloadError("ACP_TOOL_ACTIVITY_UNVERIFIED")
    mapped = proxy_permission(call)
    return {"name": mapped["name"], "rawInput": mapped["arguments"]}


async def codex_execute(path, prompt, decode_result, **kwargs):
    return await execute(
        path,
        prompt,
        decode_result,
        activity_decoder=tool_activity,
        output_decoder=final_object,
        permission_decoder=proxy_permission,
        **kwargs,
    )
