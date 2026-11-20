"""Runtime E2AG enforcement for task-scoped remote MCP tool calls."""

from __future__ import annotations

import asyncio
import hashlib
import base64
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Any, Collection, Mapping
from uuid import uuid4

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import A2ATask, Agent, E2AGToolGrant, EventLog, McpServer, ModelInferenceGrant, ServiceMcpGrant
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


def _subagent_mcp_policy(capabilities: dict | None, tool_names: list[str]) -> None:
    if settings.e2ag_mode.lower() != "enforce":
        raise ValueError("Subagent MCP requires E2AG enforce mode")
    governance = (capabilities or {}).get("governance")
    allowed = governance.get("allowed_tools") if isinstance(governance, dict) else None
    if not isinstance(allowed, list) or not all(isinstance(pattern, str) for pattern in allowed):
        raise ValueError("Subagent MCP requires capabilities.governance.allowed_tools")
    if not tool_names or any(not tool_is_allowed(name, allowed) for name in tool_names):
        raise ValueError("Subagent MCP tool names must be allowed by the task Agent governance")


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
    normalize_names: bool = False,
    subagent_tool_names: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Replace supported remote MCP URLs with task-scoped E2AG proxy URLs.

    In enforce mode, unmediated stdio/SSE servers are withheld. Other modes keep
    the original connection so ablation behavior remains explicit.
    """
    mode = str(getattr(settings, "e2ag_mode", "enforce") or "enforce").lower()
    governance = (agent.capabilities or {}).get("governance", {})
    governance = governance if isinstance(governance, Mapping) else {}
    allowed_tools = list(governance.get("allowed_tools") or [])
    if subagent_tool_names is not None:
        _subagent_mcp_policy(agent.capabilities, subagent_tool_names)
        allowed_tools = subagent_tool_names
    output: dict[str, Any] = {}
    omissions: list[dict[str, Any]] = []
    proxy_base = settings.e2ag_internal_base_url.rstrip("/")

    for server in servers:
        connection = server_to_connection(server)
        transport = connection.get("transport")
        name = _dsh_server_name(server, output) if normalize_names else str(server.name or server.id)
        if normalize_names and name in output:
            omissions.append({"mcp_server_id": server.id, "name": name, "reason": "MCP_SERVER_NAME_COLLISION"})
            continue
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
    if not grant.task_id:
        if not settings.diagent_service_model_proxy or not settings.access_token:
            return None, "TOOL_GRANT_NOT_ACTIVE"
        link = await db.get(ServiceMcpGrant, grant.id)
        turn = await db.get(ModelInferenceGrant, link.model_token_hash) if link else None
        if turn is None or turn.agent_id != grant.agent_id or turn.expires_at <= int(_now().timestamp()):
            return None, "TOOL_GRANT_NOT_ACTIVE"
        return grant, "TOOL_GRANT_VALID"
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


def _service_tool_token(model_token: str, grant_id: str) -> str:
    # 以轮次凭据和工具授权 ID 派生独立的 bearer 值，服务端只需保存摘要。
    digest = hmac.digest(model_token.encode(), b"dios:mcp:" + grant_id.encode(), "sha256")
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _dsh_server_name(server: McpServer, existing: Collection[str]) -> str:
    """保留 DSH 支持的名称，必要时使用注册 ID。"""
    name = server.name
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name) and name not in existing:
        return name
    return server.id


async def issue_service_mcp_grants(db: AsyncSession, agent: Agent, turn: ModelInferenceGrant, model_token: str) -> None:
    """为常驻轮次创建与任务授权共用的 MCP 授权记录。"""
    governance = (agent.capabilities or {}).get("governance")
    patterns = governance.get("allowed_tools") if isinstance(governance, Mapping) else None
    if not agent.mcp_server_ids or not isinstance(patterns, list) or not patterns:
        return
    if not all(isinstance(pattern, str) and pattern for pattern in patterns):
        return
    servers = await streamable_mcp_servers(db, agent.mcp_server_ids)
    trace_id = uuid4().hex
    names = set()
    for server in servers:
        name = _dsh_server_name(server, names)
        if name in names:
            continue
        names.add(name)
        grant_id = uuid4().hex
        db.add(E2AGToolGrant(id=grant_id, token_hash=hash_grant_token(_service_tool_token(model_token, grant_id)),
                            trace_id=trace_id, task_id="", agent_id=agent.id, mcp_server_id=server.id,
                            allowed_tools=patterns, expires_at=datetime.fromtimestamp(turn.expires_at, timezone.utc)))
        db.add(ServiceMcpGrant(grant_id=grant_id, model_token_hash=turn.token_hash, server_name=name))


async def streamable_mcp_servers(db: AsyncSession, server_ids: list[str]) -> list[McpServer]:
    """查找 DSH 的受管 MCP 网关可以装配的服务。"""
    servers = await db.scalars(select(McpServer).where(McpServer.id.in_(server_ids)).order_by(McpServer.id))
    eligible = []
    for server in servers:
        try:
            connection = server_to_connection(server)
        except ValueError:
            continue
        if connection["transport"] == "streamable_http":
            eligible.append(server)
    return eligible


async def subagent_mcp_servers(
    db: AsyncSession, capabilities: dict | None, subagents: list[dict],
) -> dict[str, list[McpServer]]:
    """为每个启用 MCP 的角色核对治理名单与可受管的服务注册。"""
    assigned = {}
    for role in subagents:
        if "tool_selection" not in role or "mcp" not in role["tool_selection"]["tool_ids"]:
            continue
        _subagent_mcp_policy(capabilities, role["mcp_tools"])
        root_tools = (capabilities or {}).get("diagent_tools")
        if not isinstance(root_tools, list) or "shell" in root_tools:
            raise ValueError("Subagent MCP requires an explicit parent diagent_tools list without shell")
        ids = role["mcp_server_ids"]
        servers = await streamable_mcp_servers(db, ids)
        if len(servers) != len(ids):
            raise ValueError(f"Subagent {role['name']} MCP requires registered Streamable HTTP servers")
        assigned[role["name"]] = servers
    return assigned


async def service_mcp_endpoints(db: AsyncSession, turn: ModelInferenceGrant, authorization: str) -> dict[str, dict[str, str]]:
    """在轮次校验通过后下发网关标识及工具凭据。"""
    links = (await db.scalars(select(ServiceMcpGrant).where(
        ServiceMcpGrant.model_token_hash == turn.token_hash,
    ))).all()
    token = authorization.partition(" ")[2]
    return {link.server_name: {"grant_id": link.grant_id, "token": _service_tool_token(token, link.grant_id)}
            for link in links}


async def remove_service_mcp_grants(db: AsyncSession, model_token_hash: str | None = None) -> None:
    """清理指定轮次或已到期轮次的 MCP 授权。"""
    if model_token_hash is None:
        expired = select(ModelInferenceGrant.token_hash).where(ModelInferenceGrant.expires_at <= int(_now().timestamp()))
        predicate = ServiceMcpGrant.model_token_hash.in_(expired)
    else:
        predicate = ServiceMcpGrant.model_token_hash == model_token_hash
    ids = select(ServiceMcpGrant.grant_id).where(predicate)
    await db.execute(delete(E2AGToolGrant).where(E2AGToolGrant.id.in_(ids)))
    await db.execute(delete(ServiceMcpGrant).where(predicate))


async def service_mcp_sessions(db: AsyncSession, model_token_hash: str) -> list[tuple[str, dict[str, str]]]:
    """保留撤销后关闭上游已绑定会话所需的连接信息。"""
    sessions = await db.execute(select(McpServer.url, McpServer.headers, ServiceMcpGrant.upstream_session_id)
        .select_from(ServiceMcpGrant)
        .join(E2AGToolGrant, E2AGToolGrant.id == ServiceMcpGrant.grant_id)
        .join(McpServer, McpServer.id == E2AGToolGrant.mcp_server_id)
        .where(ServiceMcpGrant.model_token_hash == model_token_hash, E2AGToolGrant.status == "active",
               ServiceMcpGrant.upstream_session_id.is_not(None)))
    return [(url, {**{key: value for key, value in (headers or {}).items() if key.lower() != "mcp-session-id"},
                   "Mcp-Session-Id": session_id}) for url, headers, session_id in sessions if url and session_id]


async def close_service_mcp_sessions(sessions: list[tuple[str, dict[str, str]]]) -> None:
    """尝试通知上游释放会话，上游故障不会影响已撤销的授权。"""
    if not sessions:
        return
    async with httpx.AsyncClient(trust_env=False, timeout=2, follow_redirects=False) as client:
        async def close(url: str, headers: dict[str, str]) -> None:
            try:
                await client.delete(url, headers=headers)
            except (httpx.HTTPError, httpx.InvalidURL):
                pass

        await asyncio.gather(*(close(url, headers) for url, headers in sessions))


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
