"""DeepSeek Harness 0.2.2-rc.5 ACP metadata; authority stays in the DiOS gate."""

import re

from .acp import execute
from .acp_result import final_object
from .protocol import WorkloadError


def provider_error(error):
    """Decode the pinned DSH ACP error.data extension; never classify prose."""
    if error.code != -32603 or not isinstance(error.data, dict):
        return None
    code = error.data.get("code")
    if not isinstance(code, str):
        return None
    return {
        "RATE_LIMIT": "PROVIDER_RATE_LIMITED",
        "QUOTA": "PROVIDER_QUOTA_EXHAUSTED",
        "ACCOUNT_QUOTA": "PROVIDER_QUOTA_EXHAUSTED",
        "TIMEOUT": "PROVIDER_TIMEOUT",
        "TRANSPORT": "PROVIDER_NETWORK_ERROR",
    }.get(code)


def proxy_permission(call):
    name, arguments = call.get("title"), call.get("rawInput")
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"mcp__dios__[A-Za-z0-9_-]+", name)
        or not isinstance(arguments, dict)
    ):
        raise WorkloadError("ACP_NATIVE_OPERATION_DENIED")
    return {"name": name, "arguments": arguments}


def tool_activity(call):
    if call.get("status") not in {"completed", "failed"}:
        raise WorkloadError("ACP_TOOL_ACTIVITY_UNVERIFIED")
    mapped = proxy_permission(call)
    return {"name": mapped["name"], "rawInput": mapped["arguments"]}


async def dsh_execute(path, prompt, decode_result, **kwargs):
    return await execute(
        path,
        prompt,
        decode_result,
        activity_decoder=tool_activity,
        output_decoder=final_object,
        permission_decoder=proxy_permission,
        error_decoder=provider_error,
        **kwargs,
    )
