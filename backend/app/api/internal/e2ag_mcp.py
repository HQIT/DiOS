"""Task-scoped E2AG proxy for remote streamable HTTP MCP servers."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import McpServer
from app.services.e2ag_tool_gateway import (
    authorize_mcp_request,
    filter_tools_list_response,
    record_tool_decision,
    validate_grant,
)

router = APIRouter(prefix="/internal/e2ag/mcp", tags=["internal-e2ag"])


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _rpc_error(request_id: Any, code: int, message: str, data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message, "data": data},
        },
    )


@router.post("/{grant_id}")
async def proxy_mcp_call(
    grant_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error", {"reason": "MCP_JSON_INVALID"})
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Batch requests are not supported", {"reason": "MCP_BATCH_UNSUPPORTED"})

    grant, grant_reason = await validate_grant(db, grant_id, _bearer(request))
    if grant is None:
        return _rpc_error(payload.get("id"), -32001, "E2AG tool grant rejected", {"reason": grant_reason})

    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    params = params if isinstance(params, dict) else {}
    tool_name = str(params.get("name") or "") if method == "tools/call" else ""
    allowed, reason = authorize_mcp_request(method, params, grant.allowed_tools or [])
    if not allowed:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=False, reason=reason,
        )
        return _rpc_error(
            payload.get("id"), -32003, "E2AG policy denied MCP request",
            {"reason": reason, "trace_id": grant.trace_id, "tool": tool_name},
        )

    server = await db.get(McpServer, grant.mcp_server_id)
    if server is None or not server.url:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=False,
            reason="MCP_UPSTREAM_MISSING",
        )
        return _rpc_error(payload.get("id"), -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})

    headers = dict(server.headers or {})
    for name in ("accept", "mcp-session-id", "last-event-id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    headers["content-type"] = "application/json"

    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as client:
            upstream = await client.post(server.url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=True,
            reason=f"MCP_UPSTREAM_ERROR:{type(exc).__name__}",
        )
        return _rpc_error(payload.get("id"), -32002, "MCP upstream error", {"reason": "MCP_UPSTREAM_ERROR"})

    response_content = upstream.content
    if method == "tools/list" and upstream.status_code < 400:
        response_content = filter_tools_list_response(
            upstream.content, grant.allowed_tools or [],
        )

    await record_tool_decision(
        db, grant, tool_name=tool_name, method=method, allowed=True,
        reason=reason, upstream_status=upstream.status_code,
    )
    response_headers = {}
    for name in ("mcp-session-id", "retry-after"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]
    return Response(
        content=response_content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=response_headers,
    )
