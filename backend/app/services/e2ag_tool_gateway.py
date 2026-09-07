"""Runtime E2AG enforcement for task-scoped remote MCP tool calls."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import A2ATask, Agent, E2AGToolGrant, EventLog, McpServer
from app.services.e2ag import append_audit_entry
from app.services.mcp_config import server_to_connection

SAFE_MCP_METHODS = frozenset({
    "initialize",
    "notifications/initialized",
    "ping",
    "tools/list",
})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def hash_grant_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tool_is_allowed(tool_name: str, patterns: list[str]) -> bool:
    return bool(tool_name) and any(fnmatchcase(tool_name, pattern) for pattern in patterns)


def authorize_mcp_request(method: str, params: Mapping[str, Any], allowed_tools: list[str]) -> tuple[bool, str]:
    if method in SAFE_MCP_METHODS:
        return True, "MCP_CONTROL_METHOD_ALLOWED"
    if method != "tools/call":
        return False, "MCP_METHOD_NOT_ALLOWED"
    tool_name = str(params.get("name") or "")
    if not tool_is_allowed(tool_name, allowed_tools):
        return False, "MCP_TOOL_NOT_GRANTED"
    return True, "MCP_TOOL_GRANTED"


def filter_tools_list_response(content: bytes, allowed_tools: list[str]) -> bytes:
    """Return only contract-authorized tools from a JSON-RPC tools/list result.

    If the upstream body is not a conventional JSON tools/list response, fail
    closed by returning an empty tool list instead of exposing the full registry.
    """
    is_sse = content.lstrip().startswith((b"event:", b"data:"))
    raw_payload = content
    if is_sse:
        data_lines = [
            line[5:].lstrip()
            for line in content.decode("utf-8", errors="replace").splitlines()
            if line.startswith("data:")
        ]
        raw_payload = "\n".join(data_lines).encode("utf-8")
    try:
        payload = json.loads(raw_payload)
        result = payload.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise ValueError("missing tools list")
        result["tools"] = [
            tool for tool in tools
            if isinstance(tool, dict)
            and tool_is_allowed(str(tool.get("name") or ""), allowed_tools)
        ]
        filtered = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, json.JSONDecodeError):
        filtered = json.dumps(
            {"jsonrpc": "2.0", "id": None, "result": {"tools": []}},
            separators=(",", ":"),
        ).encode("utf-8")
    if is_sse:
        return b"event: message\ndata: " + filtered + b"\n\n"
    return filtered


async def _append_event_audit(
    db: AsyncSession,
    event_log_id: str,
    *,
    trace_id: str,
    stage: str,
    outcome: str,
    evidence: Mapping[str, Any],
) -> None:
    if not event_log_id:
        return
    event_log = await db.get(EventLog, event_log_id)
    if event_log is None:
        return
    event_log.audit_chain = append_audit_entry(
        event_log.audit_chain or [],
        trace_id=trace_id,
        stage=stage,
        outcome=outcome,
        evidence=evidence,
    )


async def issue_task_mcp_config(
    db: AsyncSession,
    *,
    agent: Agent,
    task: A2ATask,
    servers: list[McpServer],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace supported remote MCP URLs with task-scoped E2AG proxy URLs.

    In enforce mode, unmediated stdio/SSE servers are withheld. Other modes keep
    the original connection so ablation behavior remains explicit.
    """
    mode = str(getattr(settings, "e2ag_mode", "enforce") or "enforce").lower()
    governance = (agent.capabilities or {}).get("governance", {})
    governance = governance if isinstance(governance, Mapping) else {}
    allowed_tools = list(governance.get("allowed_tools") or [])
    output: dict[str, Any] = {}
    omissions: list[dict[str, Any]] = []
    proxy_base = settings.e2ag_internal_base_url.rstrip("/")

    for server in servers:
        connection = server_to_connection(server)
        transport = connection.get("transport")
        name = str(server.name or server.id)
        if mode != "enforce":
            output[name] = connection
            continue
        if transport != "streamable_http":
            omissions.append({
                "mcp_server_id": server.id,
                "name": name,
                "transport": transport,
                "reason": "MCP_TRANSPORT_NOT_MEDIATED",
            })
            continue
        if not allowed_tools:
            omissions.append({
                "mcp_server_id": server.id,
                "name": name,
                "transport": transport,
                "reason": "MCP_ALLOWED_TOOLS_EMPTY",
            })
            continue

        raw_token = secrets.token_urlsafe(32)
        grant = E2AGToolGrant(
            token_hash=hash_grant_token(raw_token),
            trace_id=task.trace_id,
            task_id=task.id,
            event_log_id=task.context_id,
            agent_id=agent.id,
            mcp_server_id=server.id,
            allowed_tools=allowed_tools,
            status="active",
            expires_at=_now() + timedelta(seconds=settings.e2ag_tool_grant_ttl_seconds),
        )
        db.add(grant)
        await db.flush()
        output[name] = {
            "transport": "streamable_http",
            "url": f"{proxy_base}/api/internal/e2ag/mcp/{grant.id}",
            "headers": {"Authorization": f"Bearer {raw_token}"},
        }
        await _append_event_audit(
            db,
            task.context_id,
            trace_id=task.trace_id,
            stage="tool_grant",
            outcome="grant_issued",
            evidence={
                "grant_id": grant.id,
                "agent_id": agent.id,
                "task_id": task.id,
                "mcp_server_id": server.id,
                "allowed_tools": allowed_tools,
            },
        )

    for omission in omissions:
        await _append_event_audit(
            db,
            task.context_id,
            trace_id=task.trace_id,
            stage="tool_grant",
            outcome="grant_withheld",
            evidence={"agent_id": agent.id, "task_id": task.id, **omission},
        )
    await db.commit()
    return output, omissions


async def validate_grant(
    db: AsyncSession,
    grant_id: str,
    raw_token: str,
) -> tuple[E2AGToolGrant | None, str]:
    grant = await db.get(E2AGToolGrant, grant_id)
    if grant is None or not raw_token or not secrets.compare_digest(grant.token_hash, hash_grant_token(raw_token)):
        return None, "TOOL_GRANT_INVALID"
    if grant.status != "active":
        return None, "TOOL_GRANT_NOT_ACTIVE"
    if _aware(grant.expires_at) <= _now():
        grant.status = "expired"
        await _append_event_audit(
            db, grant.event_log_id, trace_id=grant.trace_id, stage="tool_grant",
            outcome="grant_expired",
            evidence={"grant_id": grant.id, "task_id": grant.task_id},
        )
        await db.commit()
        return None, "TOOL_GRANT_EXPIRED"
    task = await db.get(A2ATask, grant.task_id)
    if task is None or task.trace_id != grant.trace_id or task.agent_id != grant.agent_id:
        return None, "TOOL_GRANT_TASK_MISMATCH"
    if task.status in {"completed", "failed", "canceled"}:
        grant.status = "revoked"
        await _append_event_audit(
            db, grant.event_log_id, trace_id=grant.trace_id, stage="tool_grant",
            outcome="grant_revoked",
            evidence={
                "grant_id": grant.id,
                "task_id": grant.task_id,
                "reason": "task_terminal",
                "task_status": task.status,
            },
        )
        await db.commit()
        return None, "TOOL_GRANT_TASK_TERMINAL"
    return grant, "TOOL_GRANT_VALID"


async def record_tool_decision(
    db: AsyncSession,
    grant: E2AGToolGrant,
    *,
    tool_name: str,
    method: str,
    allowed: bool,
    reason: str,
    upstream_status: int | None = None,
) -> None:
    if method == "tools/call":
        from sqlalchemy import update

        await db.execute(
            update(E2AGToolGrant)
            .where(E2AGToolGrant.id == grant.id)
            .values(
                call_count=E2AGToolGrant.call_count + 1,
                last_used_at=_now(),
            )
        )
    await _append_event_audit(
        db,
        grant.event_log_id,
        trace_id=grant.trace_id,
        stage="tool_call",
        outcome="allow" if allowed else "deny",
        evidence={
            "grant_id": grant.id,
            "task_id": grant.task_id,
            "agent_id": grant.agent_id,
            "mcp_server_id": grant.mcp_server_id,
            "method": method,
            "tool": tool_name,
            "reason": reason,
            "upstream_status": upstream_status,
        },
    )
    await db.commit()


async def revoke_task_grants(db: AsyncSession, task_id: str) -> None:
    from sqlalchemy import select

    result = await db.execute(
        select(E2AGToolGrant).where(
            E2AGToolGrant.task_id == task_id,
            E2AGToolGrant.status == "active",
        )
    )
    for grant in result.scalars().all():
        grant.status = "revoked"
        await _append_event_audit(
            db, grant.event_log_id, trace_id=grant.trace_id, stage="tool_grant",
            outcome="grant_revoked",
            evidence={
                "grant_id": grant.id,
                "task_id": grant.task_id,
                "reason": "task_lifecycle",
            },
        )
    await db.commit()
