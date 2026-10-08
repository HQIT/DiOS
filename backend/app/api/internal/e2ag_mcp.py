"""Task-scoped E2AG proxy for remote streamable HTTP MCP servers."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from app.db.database import get_db
from app.models.tables import McpServer
from app.services.e2ag_policy_errors import PolicyEvaluationError
from app.services.e2ag_http_transport import transport_options
from app.config import settings
from app.services.e2ag_stdio_transport import StdioExchangeError
from app.services.e2ag_tool_gateway import (
    authorize_mcp_request,
    begin_tool_invocation,
    finish_tool_invocation,
    filter_tools_list_response,
    record_grant_rejection,
    record_tool_decision,
    validate_grant,
)

router = APIRouter(prefix="/internal/e2ag/mcp", tags=["internal-e2ag"])


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _forward_headers(request: Request, server: McpServer) -> dict[str, str]:
    headers = dict(server.headers or {})
    for name in ("accept", "mcp-session-id", "last-event-id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _rpc_error(
    request_id: Any, code: int, message: str, data: dict[str, Any], *, status_code: int = 403,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
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

    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    params = params if isinstance(params, dict) else {}
    tool_name = str(params.get("name") or "") if method == "tools/call" else ""
    token = _bearer(request)
    try:
        grant, grant_reason = await validate_grant(db, grant_id, token)
    except PolicyEvaluationError:
        # Reauthenticate attribution, even if an adapter raised before checking
        # its credential. An invalid token must not pollute another event chain.
        await record_grant_rejection(
            db, grant_id, token, method=method, tool_name=tool_name, reason="POLICY_ENGINE_ERROR",
        )
        return _rpc_error(payload.get("id"), -32004, "Policy service unavailable",
                          {"reason": "POLICY_ENGINE_ERROR"}, status_code=503)
    if grant is None:
        await record_grant_rejection(
            db, grant_id, token, method=method, tool_name=tool_name, reason=grant_reason,
        )
        return _rpc_error(payload.get("id"), -32001, "E2AG tool grant rejected", {"reason": grant_reason})

    try:
        allowed, reason = authorize_mcp_request(
            method, params, grant.allowed_tools or [], grant.argument_constraints,
        )
    except PolicyEvaluationError:
        await record_grant_rejection(
            db, grant_id, token, method=method, tool_name=tool_name, reason="POLICY_ENGINE_ERROR",
        )
        return _rpc_error(payload.get("id"), -32004, "Policy service unavailable",
                          {"reason": "POLICY_ENGINE_ERROR"}, status_code=503)
    if not allowed:
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=False, reason=reason,
        )
        return _rpc_error(
            payload.get("id"), -32003, "E2AG policy denied MCP request",
            {"reason": reason, "trace_id": grant.trace_id, "tool": tool_name},
        )

    server = await db.get(McpServer, grant.mcp_server_id)
    is_stdio = server is not None and server.transport == "stdio" and server.id in settings.e2ag_stdio_server_ids
    if server is None or (not is_stdio and (server.transport == "stdio" or not server.url)):
        await record_tool_decision(
            db, grant, tool_name=tool_name, method=method, allowed=False,
            reason="MCP_UPSTREAM_MISSING",
        )
        return _rpc_error(payload.get("id"), -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})

    headers = _forward_headers(request, server)
    headers["content-type"] = "application/json"
    # Capture values before a rollback can expire ORM attributes. No database
    # transaction is held across upstream I/O after the intent is committed.
    upstream_url = server.url
    allowed_tools = list(grant.allowed_tools or [])
    try:
        invocation_id = await begin_tool_invocation(db, grant, payload)
    except (SQLAlchemyError, ValueError, RuntimeError):
        await db.rollback()
        return _rpc_error(payload.get("id"), -32005, "Invocation intent unavailable",
                          {"reason": "MCP_INTENT_PERSIST_FAILED", "forwarded": False}, status_code=503)

    try:
        if is_stdio:
            app = request.scope.get("app")
            pool = getattr(app.state, "e2ag_stdio_pool", None) if app is not None else None
            if pool is None:
                raise StdioExchangeError("STDIO_POOL_UNAVAILABLE")
            status, content = await pool.exchange(grant_id, server, payload)
            upstream = httpx.Response(status, content=content, headers={"content-type": "application/json"})
        else:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=30,
                follow_redirects=True,
                **transport_options(request),
            ) as client:
                upstream = await client.post(upstream_url, json=payload, headers=headers)
    except (httpx.HTTPError, StdioExchangeError):
        # A timeout is not evidence of non-execution. The committed intent is
        # deliberately left unknown; never retry an external operation here.
        return _rpc_error(payload.get("id"), -32002, "MCP upstream outcome unknown",
                          {"reason": "MCP_UPSTREAM_ERROR", "invocation_id": invocation_id,
                           "execution_outcome": "unknown", "automatic_retry": False}, status_code=502)

    response_content = upstream.content
    if method == "tools/list" and upstream.status_code < 400:
        response_content = filter_tools_list_response(
            upstream.content, allowed_tools,
        )

    try:
        await finish_tool_invocation(
            db, grant, invocation_id, tool_name=tool_name, method=method,
            reason=reason, upstream_status=upstream.status_code, response_content=upstream.content,
        )
    except (SQLAlchemyError, ValueError, RuntimeError):
        await db.rollback()
        return _rpc_error(payload.get("id"), -32005, "Invocation result persistence unavailable",
                          {"reason": "MCP_RESULT_PERSIST_FAILED", "invocation_id": invocation_id,
                           "execution_outcome": "unknown", "automatic_retry": False}, status_code=503)
    response_headers = {"x-e2ag-invocation-id": invocation_id}
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
    server = await db.get(McpServer, grant.mcp_server_id)
    if server is not None and server.transport == "stdio":
        return Response(status_code=405)
    if server is None or not server.url:
        return _rpc_error(None, -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})

    client = httpx.AsyncClient(trust_env=False, timeout=30, follow_redirects=True)
    try:
        upstream_request = client.build_request(
            "GET", server.url, headers=_forward_headers(request, server),
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
    server = await db.get(McpServer, grant.mcp_server_id)
    if server is not None and server.transport == "stdio":
        app = request.scope.get("app")
        pool = getattr(app.state, "e2ag_stdio_pool", None) if app is not None else None
        if pool is not None:
            await pool.close_session(grant_id)
        return Response(status_code=204)
    if server is None or not server.url:
        return _rpc_error(None, -32002, "MCP upstream unavailable", {"reason": "MCP_UPSTREAM_MISSING"})
    try:
        async with httpx.AsyncClient(
            trust_env=False, timeout=30, follow_redirects=True,
        ) as client:
            upstream = await client.delete(
                server.url, headers=_forward_headers(request, server),
            )
    except httpx.HTTPError:
        return _rpc_error(None, -32002, "MCP upstream error", {"reason": "MCP_UPSTREAM_ERROR"})
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )
