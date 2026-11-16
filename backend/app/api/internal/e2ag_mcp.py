"""Task-scoped E2AG proxy for remote streamable HTTP MCP servers."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import E2AGToolGrant, McpServer, ServiceMcpGrant
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


def _forward_headers(request: Request, server: McpServer, grant: E2AGToolGrant) -> dict[str, str]:
    headers = dict(server.headers or {})
    if not grant.task_id:
        headers = {name: value for name, value in headers.items() if name.lower() != "mcp-session-id"}
    for name in ("accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


async def _matches_service_session(db: AsyncSession, grant: E2AGToolGrant, request: Request, *, initialize: bool = False) -> bool:
    if grant.task_id:
        return True
    link = await db.get(ServiceMcpGrant, grant.id)
    requested = request.headers.get("mcp-session-id")
    if link is None or (initialize and (requested or link.upstream_session_id)):
        return False
    return link.upstream_session_id == requested


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
    if not await _matches_service_session(db, grant, request, initialize=method == "initialize"):
        return _rpc_error(payload.get("id"), -32001, "MCP session is outside this grant", {"reason": "MCP_SESSION_MISMATCH"})
    params = payload.get("params") or {}
    params = params if isinstance(params, dict) else {}
    tool_name = str(params.get("name") or "") if method == "tools/call" else ""
    allowed, reason = authorize_mcp_request(method, params, grant.allowed_tools or [])
    if not allowed:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=False, reason=reason,
        )
        if method == "server/discover":
            # 网关采用 initialize 握手。返回 JSON-RPC Method not found 让客户端回退，
            # 此探测仍经授权校验和审计，且留在网关本地。
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {"code": -32601, "message": "Method not found"},
                },
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

    headers = _forward_headers(request, server, grant)
    headers["content-type"] = "application/json"

    try:
        async with httpx.AsyncClient(
            trust_env=False,
            timeout=30,
            follow_redirects=bool(grant.task_id),
        ) as client:
            upstream = await client.post(server.url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=True,
            reason=f"MCP_UPSTREAM_ERROR:{type(exc).__name__}",
        )
        return _rpc_error(payload.get("id"), -32002, "MCP upstream error", {"reason": "MCP_UPSTREAM_ERROR"})

    response_content = upstream.content
    if not grant.task_id and method == "initialize" and upstream.is_success:
        session_id = upstream.headers.get("mcp-session-id")
        if session_id:
            if len(session_id) > 256:
                return _rpc_error(payload.get("id"), -32002, "Invalid MCP session ID", {"reason": "MCP_SESSION_INVALID"})
            binding = await db.execute(update(ServiceMcpGrant).where(
                ServiceMcpGrant.grant_id == grant.id, ServiceMcpGrant.upstream_session_id.is_(None),
            ).values(upstream_session_id=session_id))
            if not binding.rowcount:
                return _rpc_error(payload.get("id"), -32001, "MCP session already bound", {"reason": "MCP_SESSION_MISMATCH"})
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


@router.get("/{grant_id}")
async def proxy_mcp_stream(
    grant_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Relay the optional Streamable HTTP SSE channel under the same grant."""
    grant, grant_reason = await validate_grant(db, grant_id, _bearer(request))
    if grant is None:
        return _rpc_error(None, -32001, "E2AG tool grant rejected", {"reason": grant_reason})
    if not grant.task_id:
        # 常驻授权不开放独立 SSE 通道，轮次结束后不能继续接收上游事件。
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    server = await db.get(McpServer, grant.mcp_server_id)
    if server is None or not server.url:
        return _rpc_error(None, -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})

    client = httpx.AsyncClient(trust_env=False, timeout=30, follow_redirects=bool(grant.task_id))
    try:
        upstream_request = client.build_request(
            "GET", server.url, headers=_forward_headers(request, server, grant),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return _rpc_error(None, -32002, "MCP upstream error", {"reason": "MCP_UPSTREAM_ERROR"})

    response_headers = {}
    for name in ("mcp-session-id", "retry-after"):
        if name in upstream.headers:
            response_headers[name] = upstream.headers[name]
    if upstream.status_code >= 400:
        content = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=response_headers,
        )

    async def relay():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers=response_headers,
    )


@router.delete("/{grant_id}")
async def proxy_mcp_close(
    grant_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Forward session termination without granting a new tool action."""
    grant, grant_reason = await validate_grant(db, grant_id, _bearer(request))
    if grant is None:
        return _rpc_error(None, -32001, "E2AG tool grant rejected", {"reason": grant_reason})
    if not await _matches_service_session(db, grant, request):
        return _rpc_error(None, -32001, "MCP session is outside this grant", {"reason": "MCP_SESSION_MISMATCH"})
    if not grant.task_id and not request.headers.get("mcp-session-id"):
        return Response(status_code=405, headers={"Allow": "POST"})
    server = await db.get(McpServer, grant.mcp_server_id)
    if server is None or not server.url:
        return _rpc_error(None, -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})
    try:
        async with httpx.AsyncClient(
            trust_env=False, timeout=30, follow_redirects=bool(grant.task_id),
        ) as client:
            upstream = await client.delete(
                server.url, headers=_forward_headers(request, server, grant),
            )
    except httpx.HTTPError:
        return _rpc_error(None, -32002, "MCP upstream error", {"reason": "MCP_UPSTREAM_ERROR"})
    if not grant.task_id and upstream.is_success:
        grant.status = "revoked"
        await db.commit()
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
