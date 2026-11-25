"""Managed Agent service configuration and health; lifecycle via RuntimeAdapter."""
import asyncio
import json
import logging
import os
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Agent, AgentRuntime, LLMModel, McpServer
from app.runtime.database import RuntimeErrorCode
from app.services import managed_runtime
from app.services.http_internal import internal_client

logger = logging.getLogger(__name__)

async def _sync_agent_workspace(agent: Agent, db: AsyncSession) -> dict[str, str]:
    """为 Agent 的 workspace 生成 models.yaml 和 mcp_servers.json，返回环境变量 dict。"""
    workspace = Path(agent.workspace_path)
    workspace.mkdir(parents=True, exist_ok=True)
    # 业务凭据放最底层，系统 env 会覆盖同名 key（系统优先）
    env: dict[str, str] = {}
    for k, v in (agent.env or {}).items():
        if v is None:
            continue
        env[str(k)] = str(v)
    env.update({
        "AGENT_WORKSPACE": "/workspace",
        "LLM_MODELS_CONFIG_PATH": "/workspace/models.yaml",
        "DIOS_API": os.environ.get("DIOS_API_INTERNAL", "http://backend:8000"),
    })

    result = await db.execute(select(LLMModel))
    llms = result.scalars().all()
    models = {}
    for llm in llms:
        models[llm.model] = {
            "provider": llm.provider or "openai",
            "model": llm.model,
            "base_url": llm.base_url,
            **({"api_key": llm.api_key} if llm.api_key else {}),
        }
    (workspace / "models.yaml").write_text(yaml.dump({"models": models}, allow_unicode=True))

    if agent.system_prompt:
        env["AGENT_SYSTEM_PROMPT"] = agent.system_prompt

    mcp_ids = agent.mcp_server_ids or []
    if mcp_ids:
        mcp_result = await db.execute(select(McpServer).where(McpServer.id.in_(mcp_ids)))
        mcp_servers = list(mcp_result.scalars().all())
        if mcp_servers:
            from app.services.mcp_config import servers_to_mcp_config

            mcp_cfg = servers_to_mcp_config(mcp_servers)
            mcp_path = workspace / "mcp_servers.json"
            mcp_path.write_text(json.dumps(mcp_cfg, ensure_ascii=False, indent=2))
            env["MCP_CONFIG_PATH"] = "/workspace/mcp_servers.json"

    # 确保共享目录存在：供 service/task 容器通过子挂载访问
    (settings.workspace_root / "skills").mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "cli").mkdir(parents=True, exist_ok=True)

    return env


async def _health_check(url: str, retries: int = 20, interval: float = 1.0) -> bool:
    for _ in range(retries):
        try:
            async with internal_client(timeout=2) as client:
                if (await client.get(f"{url}/health")).status_code == 200:
                    return True
        except Exception:  # noqa: BLE001 - transient health observation
            pass
        await asyncio.sleep(interval)
    return False


async def ensure_running(agent_id: str, db: AsyncSession, *, call_id=None) -> str:
    from app.services.managed_admission import gate, validate_call
    if call_id:
        await validate_call(db, call_id, agent_id)
        return await _ensure_running(agent_id, db)
    async with gate(db):
        return await _ensure_running(agent_id, db)


async def _ensure_running(agent_id: str, db: AsyncSession) -> str:
    runtime_handle = managed_runtime.handle("service", agent_id)
    async with managed_runtime.exclusive(runtime_handle):
        runtime = await db.get(AgentRuntime, agent_id)
        if runtime and runtime.container_id != runtime_handle:
            raise RuntimeErrorCode("LEGACY_RUNTIME_REQUIRES_DRAIN")
        agent = await db.get(Agent, agent_id)
        if not agent or not agent.workspace_path:
            raise ValueError("Agent workspace is required")
        adapter = managed_runtime.managed_adapter()
        current = await adapter.observe(runtime_handle)
        if current.alive:
            if current.state != "running":
                raise RuntimeErrorCode("RUNTIME_NOT_READY")
            url = await adapter.endpoint(runtime_handle, 8000)
        else:
            if current.state not in {"absent", "exited", "dead"}:
                raise RuntimeErrorCode("RUNTIME_START_UNCONFIRMED")
            if current.state != "absent":
                await managed_runtime.retain_logs(runtime_handle, Path(agent.workspace_path), current.process_instance)
                await adapter.remove(runtime_handle)
            env = await _sync_agent_workspace(agent, db)
            # Persist intent before starting; an uncertain start remains inspectable.
            if runtime is None:
                runtime = AgentRuntime(agent_id=agent_id, container_id=runtime_handle, url="", status="starting")
                db.add(runtime)
            else:
                runtime.status = "starting"
            await db.commit()
            current = await adapter.start(runtime_handle, managed_runtime.launch_spec("service", Path(agent.workspace_path), env))
            url = await adapter.endpoint(runtime_handle, 8000)
        if runtime is None:
            runtime = AgentRuntime(agent_id=agent_id, container_id=runtime_handle, url=url, status="starting")
            db.add(runtime)
        healthy = await _health_check(url)
        runtime.url, runtime.status = url, "running" if healthy else "error"
        await db.commit()
        if not healthy:
            observed = await adapter.stop(runtime_handle)
            if observed.alive or observed.state not in {"exited", "dead"}:
                raise RuntimeErrorCode("RUNTIME_STOP_UNCONFIRMED")
            await managed_runtime.retain_logs(runtime_handle, Path(agent.workspace_path), observed.process_instance)
            raise RuntimeErrorCode("RUNTIME_HEALTH_FAILED")
        return url


async def stop_agent(agent_id: str, db: AsyncSession) -> None:
    from app.services.managed_admission import assert_not_restoring
    await assert_not_restoring(db)
    runtime_handle = managed_runtime.handle("service", agent_id)
    async with managed_runtime.exclusive(runtime_handle):
        runtime = await db.get(AgentRuntime, agent_id)
        if not runtime:
            return
        if runtime.container_id != runtime_handle:
            raise RuntimeErrorCode("LEGACY_RUNTIME_REQUIRES_DRAIN")
        adapter = managed_runtime.managed_adapter()
        observed = await adapter.stop(runtime_handle, grace_seconds=10)
        if observed.alive or observed.state not in {"absent", "exited", "dead"}:
            raise RuntimeErrorCode("RUNTIME_STOP_UNCONFIRMED")
        agent = await db.get(Agent, agent_id)
        if observed.state != "absent" and agent and agent.workspace_path:
            await managed_runtime.retain_logs(runtime_handle, Path(agent.workspace_path), observed.process_instance)
        await adapter.remove(runtime_handle)
        await db.delete(runtime)
        await db.commit()
