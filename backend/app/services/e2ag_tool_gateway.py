"""Runtime E2AG enforcement for task-scoped remote MCP tool calls."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.models.tables import A2ATask, Agent, E2AGToolGrant, E2AGToolInvocation, EventLog, McpServer
from app.services.e2ag import append_audit_entry
from app.services.e2ag_arguments import check_arguments, constraints_digest, valid_constraints
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


def authorize_mcp_request(
    method: str, params: Mapping[str, Any], allowed_tools: list[str],
    argument_constraints: dict | None = None,
) -> tuple[bool, str]:
    if method in SAFE_MCP_METHODS:
        return True, "MCP_CONTROL_METHOD_ALLOWED"
    if method != "tools/call":
        return False, "MCP_METHOD_NOT_ALLOWED"
    tool_name = str(params.get("name") or "")
    if not tool_is_allowed(tool_name, allowed_tools):
        return False, "MCP_TOOL_NOT_GRANTED"
    if argument_constraints is not None:
        return check_arguments(tool_name, params.get("arguments"), argument_constraints)
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
    # Flush our own pending changes first, then compare-and-swap the persisted
    # chain. A stale ORM identity must not overwrite another gateway's entry.
    await db.flush()
    for _ in range(8):
        old = (await db.execute(select(EventLog.audit_chain).where(EventLog.id == event_log_id))).scalar_one()
        chain = append_audit_entry(old or [], trace_id=trace_id, stage=stage,
                                   outcome=outcome, evidence=evidence)
        result = await db.execute(
            update(EventLog).where(EventLog.id == event_log_id, EventLog.audit_chain == old)
            .values(audit_chain=chain).execution_options(synchronize_session=False)
        )
        if result.rowcount == 1:
            set_committed_value(event_log, "audit_chain", chain)
            return
    raise RuntimeError("E2AG audit chain changed during bounded append")


async def begin_tool_invocation(db: AsyncSession, grant: E2AGToolGrant, payload: dict) -> str:
    """Commit intent before any upstream POST; failure must prevent forwarding."""
    event = await db.get(EventLog, grant.event_log_id)
    if event is None or event.trace_id != grant.trace_id:
        raise ValueError("E2AG invocation has no matching event trace")
    invocation_id = uuid.uuid4().hex
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                      separators=(",", ":")).encode()).hexdigest()
    params = payload.get("params")
    params = params if isinstance(params, dict) else {}
    method = str(payload.get("method") or "")
    tool_name = str(params.get("name") or "") if method == "tools/call" else ""
    db.add(E2AGToolInvocation(id=invocation_id, grant_id=grant.id, event_log_id=grant.event_log_id,
                             trace_id=grant.trace_id, task_id=grant.task_id, method=method,
                             tool_name=tool_name, request_sha256=digest, state="outcome_unknown"))
    await _append_event_audit(db, grant.event_log_id, trace_id=grant.trace_id,
                             stage="tool_intent", outcome="outcome_unknown",
                             evidence={"invocation_id": invocation_id, "grant_id": grant.id,
                                       "task_id": grant.task_id, "method": method, "tool": tool_name,
                                       "request_sha256": digest})
    await db.commit()
    return invocation_id


async def finish_tool_invocation(db: AsyncSession, grant: E2AGToolGrant, invocation_id: str,
                                 *, method: str, tool_name: str, reason: str,
                                 upstream_status: int, response_content: bytes) -> None:
    """Commit observed response and linked audit atomically, without resending."""
    result = await db.execute(update(E2AGToolInvocation).where(
        E2AGToolInvocation.id == invocation_id, E2AGToolInvocation.grant_id == grant.id,
        E2AGToolInvocation.state == "outcome_unknown",
    ).values(state="response_received", upstream_status=upstream_status,
             response_sha256=hashlib.sha256(response_content).hexdigest(), finished_at=_now()))
    if result.rowcount != 1:
        raise ValueError("E2AG invocation is missing or already finalized")
    await record_tool_decision(db, grant, tool_name=tool_name, method=method, allowed=True,
                               reason=reason, upstream_status=upstream_status,
                               invocation_id=invocation_id)


async def issue_task_mcp_config(
    db: AsyncSession,
    *,
    agent: Agent,
    task: A2ATask,
    servers: list[McpServer],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace supported remote MCP URLs with task-scoped E2AG proxy URLs.

    In enforce mode, only opt-in controlled stdio is mediated; other stdio/SSE
    servers are withheld. Other modes keep
    the original connection so ablation behavior remains explicit.
    """
    mode = str(getattr(settings, "e2ag_mode", "enforce") or "enforce").lower()
    governance = (agent.capabilities or {}).get("governance", {})
    governance = governance if isinstance(governance, Mapping) else {}
    allowed_tools = list(governance.get("allowed_tools") or [])
    # Missing field preserves the explicitly tool-only legacy contract. An
    # explicit null/malformed configuration must not silently disable checks.
    has_constraints = "argument_constraints" in governance
    argument_constraints = governance.get("argument_constraints")
    constraints_valid = not has_constraints or valid_constraints(argument_constraints)
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
        controlled_stdio = transport == "stdio" and server.id in settings.e2ag_stdio_server_ids
        if transport != "streamable_http" and not controlled_stdio:
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

        if not constraints_valid:
            omissions.append({"mcp_server_id": server.id, "name": name, "transport": transport,
                              "reason": "MCP_ARGUMENT_CONSTRAINT_INVALID"})
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
            argument_constraints=json.loads(json.dumps(argument_constraints)) if has_constraints else None,
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
                **({"argument_constraints_sha256": constraints_digest(argument_constraints)} if has_constraints else {}),
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
    invocation_id: str | None = None,
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
            **({"invocation_id": invocation_id} if invocation_id else {}),
            **({"argument_constraints_sha256": constraints_digest(grant.argument_constraints)}
               if grant.argument_constraints is not None else {}),
        },
    )
    await db.commit()


async def record_grant_rejection(
    db: AsyncSession,
    grant_id: str,
    raw_token: str,
    *,
    method: str,
    tool_name: str,
    reason: str,
) -> bool:
    """Attribute a rejected POST attempt only to an authenticated grant.

    This is evidence recording, never execution authorization. Unknown IDs or
    invalid tokens must not let an unauthenticated request append to someone
    else's event chain. A corrupt/missing event association is also unassigned.
    No bearer token, tool arguments, or supplied task/trace identifiers are logged.
    """
    if reason not in {
        "TOOL_GRANT_NOT_ACTIVE", "TOOL_GRANT_EXPIRED",
        "TOOL_GRANT_TASK_MISMATCH", "TOOL_GRANT_TASK_TERMINAL",
        "POLICY_ENGINE_ERROR",
    }:
        return False
    grant = await db.get(E2AGToolGrant, grant_id)
    if grant is None or not raw_token or not secrets.compare_digest(grant.token_hash, hash_grant_token(raw_token)):
        return False
    event = await db.get(EventLog, grant.event_log_id) if grant.event_log_id else None
    if event is None or event.trace_id != grant.trace_id:
        return False
    await record_tool_decision(
        db, grant, tool_name=tool_name, method=method, allowed=False, reason=reason,
    )
    return True


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
