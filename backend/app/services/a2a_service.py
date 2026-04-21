"""A2A (Agent-to-Agent) 协议服务层。

实现 A2A 协议子集：
- Agent Card 生成（从 Agent 表推导能力信息）
- message/send: 发送消息，创建 A2ATask
  - service 模式 Agent: HTTP 转发到容器内的 A2A 端点
  - task 模式 Agent: DiOS 作为 Proxy，启动一次性容器执行
- tasks/get: 查询 Task 状态
- tasks/cancel: 取消运行中的 Task
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Agent, A2ATask, LLMModel, McpServer

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Agent Card 生成 ─────────────────────────────────────────────────


def build_agent_card(agent: Agent, base_url: str) -> dict[str, Any]:
    """根据 Agent 表记录构造 A2A Agent Card。

    A2A Agent Card 核心字段参考：
    https://github.com/google/A2A
    - name, description, url, version
    - capabilities: {streaming, pushNotifications, stateTransitionHistory}
    - skills: [{id, name, description, tags}]
    """
    caps = dict(agent.capabilities or {})
    caps.setdefault("streaming", False)
    caps.setdefault("pushNotifications", False)
    caps.setdefault("stateTransitionHistory", True)

    skills_out: list[dict[str, Any]] = []
    for skill_name in (agent.skills or []):
        skills_out.append({
            "id": skill_name,
            "name": skill_name,
            "description": "",
            "tags": [],
        })

    return {
        "name": agent.name,
        "description": agent.description or "",
        "url": f"{base_url.rstrip('/')}/api/os/a2a/agents/{agent.id}",
        "version": "0.1.0",
        "capabilities": caps,
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": skills_out,
        # DiOS 扩展字段（非 A2A 标准，但对调试有用）
        "provider": {
            "organization": "DiFlow",
            "product": "DiOS",
        },
        "_dios": {
            "agent_id": agent.id,
            "mode": agent.mode,
            "group": agent.group,
        },
    }


def build_platform_agent_card(base_url: str) -> dict[str, Any]:
    """DiOS 平台级 Agent Card（入口发现用），汇总可用能力。"""
    return {
        "name": "DiOS",
        "description": "DiFlow Intelligent Operation System - Agent-to-Agent gateway",
        "url": f"{base_url.rstrip('/')}/api/os/a2a",
        "version": "0.1.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [],
        "provider": {
            "organization": "DiFlow",
            "product": "DiOS",
        },
    }


# ── A2ATask 管理 ────────────────────────────────────────────────────


async def create_task(
    db: AsyncSession,
    agent_id: str,
    message: dict[str, Any],
    context_id: str = "",
) -> A2ATask:
    """在 DB 创建一个 A2ATask，初始状态 submitted。"""
    task = A2ATask(
        id=uuid.uuid4().hex,
        agent_id=agent_id,
        context_id=context_id,
        status="submitted",
        message=message,
        artifacts=[],
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


def task_to_a2a_dict(task: A2ATask) -> dict[str, Any]:
    """把 A2ATask 转为 A2A 协议返回格式。"""
    return {
        "id": task.id,
        "contextId": task.context_id or "",
        "status": {
            "state": task.status,
            "timestamp": task.updated_at.isoformat() if task.updated_at else None,
        },
        "artifacts": task.artifacts or [],
        "history": [task.message] if task.message else [],
        "error": task.error or None,
    }


# ── message/send 核心实现 ───────────────────────────────────────────


async def send_message(
    db: AsyncSession,
    agent_id: str,
    message: dict[str, Any],
    context_id: str = "",
) -> A2ATask:
    """A2A message/send 方法入口。
    - service 模式：HTTP 转发到运行中的 DiAgent 容器
    - task 模式：启动一次性容器，DiOS 作为 Proxy
    """
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    task = await create_task(db, agent_id=agent_id, message=message, context_id=context_id)

    try:
        if agent.mode == "service":
            await _send_to_service_agent(db, agent, task)
        else:
            await _send_to_task_agent(db, agent, task)
    except Exception as e:
        logger.exception("send_message failed for agent %s", agent_id)
        task.status = "failed"
        task.error = f"{type(e).__name__}: {e}"
        task.updated_at = _now()
        await db.commit()
        await db.refresh(task)

    return task


async def _send_to_service_agent(db: AsyncSession, agent: Agent, task: A2ATask) -> None:
    """service 模式：确保容器运行，HTTP 转发到 DiAgent OpenAI 兼容端点。

    当前 DiAgent service 容器暴露 `/v1/chat/completions`。这里把 A2A Message
    转为 OpenAI 请求，调用 DiAgent，回复作为 Artifact 写回 Task。
    未来 DiAgent 若实现原生 A2A 端点，可直接替换为透传 JSON-RPC。
    """
    import asyncio
    from app.services.agent_runtime import ensure_running

    task.status = "working"
    task.updated_at = _now()
    await db.commit()

    asyncio.create_task(_run_service_call(task.id, agent.id))


async def _run_service_call(task_id: str, agent_id: str) -> None:
    """后台执行 service 模式转发，避免阻塞 send_message 返回。"""
    import httpx
    from app.db.database import async_session
    from app.services.agent_runtime import ensure_running

    try:
        async with async_session() as db:
            task = await db.get(A2ATask, task_id)
            if not task:
                return
            url = await ensure_running(agent_id, db)

            agent = await db.get(Agent, agent_id)
            model_name = ""
            if agent and agent.model:
                m = await db.execute(select(LLMModel).where(LLMModel.name == agent.model))
                mobj = m.scalar_one_or_none()
                if mobj:
                    model_name = mobj.model

            text = _extract_text_from_message(task.message)
            openai_req: dict[str, Any] = {
                "messages": [{"role": "user", "content": text}],
                "stream": False,
            }
            if model_name:
                openai_req["model"] = model_name

            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{url}/v1/chat/completions", json=openai_req)
                resp.raise_for_status()
                data = resp.json()

            content = ""
            try:
                content = data["choices"][0]["message"]["content"] or ""
            except Exception:
                content = json.dumps(data, ensure_ascii=False)

            task = await db.get(A2ATask, task_id)
            if not task:
                return
            task.artifacts = [{
                "artifactId": f"{task_id}-reply",
                "name": "reply",
                "parts": [{"kind": "text", "text": content}],
            }]
            task.status = "completed"
            task.updated_at = _now()
            await db.commit()
    except Exception as e:
        logger.exception("service A2A call failed: task=%s", task_id)
        async with async_session() as db:
            task = await db.get(A2ATask, task_id)
            if task:
                task.status = "failed"
                task.error = f"{type(e).__name__}: {e}"
                task.updated_at = _now()
                await db.commit()


def _extract_text_from_message(message: dict[str, Any]) -> str:
    """把 A2A Message 的 parts 合并为文本。"""
    parts = message.get("parts") or []
    lines: list[str] = []
    for p in parts:
        if isinstance(p, dict):
            kind = p.get("kind") or p.get("type")
            if kind == "text":
                lines.append(str(p.get("text", "")))
            elif kind == "data":
                lines.append(json.dumps(p.get("data", {}), ensure_ascii=False, indent=2))
    return "\n\n".join(lines) if lines else json.dumps(message, ensure_ascii=False)


async def _send_to_task_agent(db: AsyncSession, agent: Agent, task: A2ATask) -> None:
    """task 模式 Proxy：把 message 作为 task 输入，启动一次性容器。"""
    import asyncio
    from app.services.docker_runner import (
        start_container,
        get_container_status,
        get_container_exit_code,
        remove_container,
    )

    if not agent.workspace_path:
        raise ValueError(f"Agent {agent.id} has no workspace_path")

    models_result = await db.execute(select(LLMModel))
    llm_models = list(models_result.scalars().all())

    mcp_override = None
    mcp_ids = getattr(agent, "mcp_server_ids", None) or []
    workspace = Path(agent.workspace_path)

    run_id = task.id[:12]

    if mcp_ids:
        mcp_result = await db.execute(select(McpServer).where(McpServer.id.in_(mcp_ids)))
        mcp_servers = list(mcp_result.scalars().all())
        if mcp_servers:
            mcp_list = [
                {"name": s.name, "command": s.command, "args": s.args or [], "env": s.env or {}}
                for s in mcp_servers
            ]
            config_dir = workspace / "config"
            config_dir.mkdir(parents=True, exist_ok=True)
            mcp_file = config_dir / f"mcp_servers_{run_id}.json"
            mcp_file.write_text(json.dumps(mcp_list, ensure_ascii=False, indent=2), encoding="utf-8")
            mcp_override = f"/workspace/config/mcp_servers_{run_id}.json"

    config = _build_proxy_task_config(
        agent=agent,
        llm_models=llm_models,
        message=task.message,
        task_id=task.id,
        run_id=run_id,
        mcp_config_path_override=mcp_override,
    )

    config_path = workspace / f"agent-task-{run_id}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "output" / "events" / run_id).mkdir(parents=True, exist_ok=True)

    container_id = start_container(run_id, workspace, extra_env=agent.env or {})
    logger.info(
        "Start task container: task_id=%s agent_id=%s run_id=%s container_id=%s context_id=%s",
        task.id, agent.id, run_id, container_id, task.context_id,
    )

    task.status = "working"
    task.updated_at = _now()
    await db.commit()

    asyncio.create_task(_poll_task_container(task.id, run_id, container_id, str(workspace)))


async def _poll_task_container(task_id: str, run_id: str, container_id: str, workspace_path: str) -> None:
    """后台轮询容器状态，完成后更新 A2ATask。"""
    import asyncio
    from app.db.database import async_session
    from app.services.docker_runner import (
        get_container_status,
        get_container_exit_code,
        remove_container,
    )

    while True:
        await asyncio.sleep(5)
        status = get_container_status(container_id)
        if status is None or status == "exited":
            break

    exit_code = get_container_exit_code(container_id)
    remove_container(container_id)

    artifacts: list[dict[str, Any]] = []
    result_file = Path(workspace_path) / "output" / "events" / run_id / "task_result.md"
    if result_file.exists():
        try:
            content = result_file.read_text(encoding="utf-8")
            artifacts.append({
                "artifactId": f"{task_id}-result",
                "name": "task_result.md",
                "parts": [{"kind": "text", "text": content}],
            })
        except Exception:
            pass

    async with async_session() as db:
        task = await db.get(A2ATask, task_id)
        if task is None:
            logger.warning("Task %s disappeared during polling", task_id)
            return
        task.artifacts = artifacts
        task.status = "completed" if exit_code == 0 else "failed"
        if exit_code != 0:
            task.error = f"container exit code {exit_code}"
        task.updated_at = _now()
        await db.commit()
    logger.info(
        "A2A task finished: task_id=%s run_id=%s exit=%s status=%s context_id=%s artifacts=%s",
        task_id,
        run_id,
        exit_code,
        "completed" if exit_code == 0 else "failed",
        task.context_id if task else "",
        len(artifacts),
    )


def _build_proxy_task_config(
    agent: Agent,
    llm_models: list[LLMModel],
    message: dict[str, Any],
    task_id: str,
    run_id: str,
    mcp_config_path_override: Optional[str] = None,
) -> dict[str, Any]:
    """把 A2A Message 转为 DiAgent task config（与 event_dispatcher 的格式对齐）。"""
    used_model = agent.model or ""

    def _resolve_model() -> LLMModel | None:
        for m in llm_models:
            if used_model and (m.name == used_model or m.id == used_model or m.model == used_model):
                return m
        return None

    models_section: dict[str, Any] = {"default_model": used_model, "models": {}}
    resolved = _resolve_model()
    if used_model and resolved is not None:
        m = resolved
        entry: dict[str, Any] = {
            "provider": m.provider,
            "model": m.model,
            "base_url": m.base_url,
        }
        if m.api_key:
            entry["api_key"] = m.api_key
        if m.display_name:
            entry["display_name"] = m.display_name
        if m.context_length:
            entry["context_length"] = m.context_length
        models_section["models"][used_model] = entry

    text_parts: list[str] = []
    for p in message.get("parts", []) or []:
        if isinstance(p, dict):
            kind = p.get("kind") or p.get("type")
            if kind == "text":
                text_parts.append(str(p.get("text", "")))
            elif kind == "data":
                text_parts.append(json.dumps(p.get("data", {}), ensure_ascii=False, indent=2))
    task_text = "\n\n".join(text_parts) if text_parts else json.dumps(message, ensure_ascii=False)
    capabilities = (agent.capabilities or {}) if isinstance(agent.capabilities, dict) else {}
    reasoning = capabilities.get("reasoning", {}) if isinstance(capabilities.get("reasoning"), dict) else {}
    subagents = capabilities.get("subagents", []) if isinstance(capabilities.get("subagents"), list) else []

    task_section: dict[str, Any] = {
        "task": task_text,
        "model": used_model,
        "temperature": 0.7,
        "workspace": "/workspace",
        "output": {
            "log_file": "task.log",
            "result_file": "task_result.md",
        },
        "output_dir": f"output/events/{run_id}",
        "trigger": {"mode": "once"},
        "recursion_limit": int(reasoning.get("recursion_limit") or 100),
    }
    if "max_tool_rounds" in reasoning:
        task_section["max_tool_rounds"] = reasoning.get("max_tool_rounds")
    if "middleware" in reasoning:
        task_section["middleware_config"] = reasoning.get("middleware")

    if agent.system_prompt:
        task_section["system_prompt"] = agent.system_prompt
    if agent.skills:
        task_section["skill_names"] = agent.skills

    mcp_path = mcp_config_path_override or getattr(agent, "mcp_config_path", None) or ""
    if mcp_path:
        task_section["mcp_config_path"] = mcp_path
    if subagents:
        converted: list[dict[str, Any]] = []
        for s in subagents:
            if not isinstance(s, dict):
                continue
            prompt = s.get("prompt") or s.get("system_prompt")
            if not (s.get("name") and s.get("description") and prompt):
                continue
            converted.append(
                {
                    "name": s.get("name"),
                    "description": s.get("description"),
                    "prompt": prompt,
                    "tools": s.get("tools", []),
                    "model": s.get("model"),
                    "task": s.get("task"),
                    "mcp_config_path": s.get("mcp_config_path"),
                    "skills_dir": s.get("skills_dir"),
                    "skill_names": s.get("skill_names"),
                }
            )
        if converted:
            task_section["subagents"] = converted

    return {"models": models_section, "task": task_section}


async def cancel_task(db: AsyncSession, task_id: str) -> A2ATask:
    """取消 Task。Phase 1C 先仅做状态变更，容器层面的中断在后续补全。"""
    task = await db.get(A2ATask, task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    if task.status in ("completed", "failed", "canceled"):
        return task
    task.status = "canceled"
    task.updated_at = _now()
    await db.commit()
    await db.refresh(task)
    return task


# ── 工具：CloudEvent -> A2A Message ────────────────────────────────


def cloudevent_to_a2a_message(event: dict[str, Any]) -> dict[str, Any]:
    """把 CloudEvent 封装为 A2A Message，用于 event_dispatcher 通过 A2A 投递。"""
    summary = (
        f"[Event type={event.get('type')} source={event.get('source')} "
        f"subject={event.get('subject', '')}]"
    )
    data = event.get("data", {}) or {}
    return {
        "role": "user",
        "messageId": uuid.uuid4().hex,
        "parts": [
            {"kind": "text", "text": summary},
            {"kind": "data", "data": data},
        ],
        "_source": {
            "kind": "cloudevent",
            "id": event.get("id") or "",
            "type": event.get("type") or "",
            "source": event.get("source") or "",
        },
    }
