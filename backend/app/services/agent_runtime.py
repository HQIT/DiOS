"""Agent Runtime Manager — 为 service 模式 Agent 管理独立的 DiAgent 容器。"""

import json
import logging
import os
import asyncio
from pathlib import Path

import yaml
import httpx
import docker
from docker.errors import NotFound, APIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Agent, AgentRuntime, LLMModel, McpServer
from app.services.docker_runner import get_client, _host_path

logger = logging.getLogger(__name__)

DOCKER_NETWORK = os.getenv("DIOS_DOCKER_NETWORK", "nana-os_default")


def _container_name(agent_id: str) -> str:
    return f"dios-agent-{agent_id}"


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
            mcp_list = [
                {"name": s.name, "command": s.command, "args": s.args or [], "env": s.env or {}}
                for s in mcp_servers
            ]
            mcp_path = workspace / "mcp_servers.json"
            mcp_path.write_text(json.dumps(mcp_list, ensure_ascii=False, indent=2))
            env["MCP_CONFIG_PATH"] = "/workspace/mcp_servers.json"

    # 确保共享目录存在：供 service/task 容器通过子挂载访问
    (settings.workspace_root / "skills").mkdir(parents=True, exist_ok=True)
    (settings.workspace_root / "cli").mkdir(parents=True, exist_ok=True)

    return env


def _start_container(agent: Agent, env: dict[str, str]) -> tuple[str, str]:
    """启动 DiAgent 服务容器，返回 (container_id, internal_url)。"""
    client = get_client()
    name = _container_name(agent.id)

    try:
        old = client.containers.get(name)
        old.remove(force=True)
    except NotFound:
        pass

    host_ws = _host_path(Path(agent.workspace_path))

    host_shared_skills = _host_path(settings.workspace_root / "skills")
    host_shared_cli = _host_path(settings.workspace_root / "cli")

    container = client.containers.run(
        image=settings.diagent_service_image,
        name=name,
        labels={"dios.agent_id": agent.id, "dios.type": "service"},
        environment=env,
        volumes={
            host_ws: {"bind": "/workspace", "mode": "rw"},
            host_shared_skills: {"bind": "/workspace/skills", "mode": "ro"},
            host_shared_cli: {"bind": "/workspace/cli", "mode": "ro"},
        },
        network=DOCKER_NETWORK,
        detach=True,
        auto_remove=False,
    )
    url = f"http://{name}:8000"
    logger.info("Started service container %s for agent %s at %s", container.short_id, agent.id, url)
    return container.id, url


async def _health_check(url: str, retries: int = 10, interval: float = 1.0) -> bool:
    for _ in range(retries):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get(f"{url}/health")
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False


async def ensure_running(agent_id: str, db: AsyncSession) -> str:
    """确保 Agent 的 DiAgent 服务容器正在运行，返回其 URL。"""
    runtime = await db.get(AgentRuntime, agent_id)

    if runtime and runtime.status == "running":
        client = get_client()
        try:
            c = client.containers.get(runtime.container_id)
            c.reload()
            if c.status == "running":
                return runtime.url
        except NotFound:
            pass
        runtime.status = "stopped"
        await db.commit()

    agent = await db.get(Agent, agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")

    env = await _sync_agent_workspace(agent, db)
    container_id, url = _start_container(agent, env)

    healthy = await _health_check(url)
    status = "running" if healthy else "error"

    if runtime:
        runtime.container_id = container_id
        runtime.url = url
        runtime.status = status
    else:
        runtime = AgentRuntime(agent_id=agent_id, container_id=container_id, url=url, status=status)
        db.add(runtime)

    await db.commit()

    if not healthy:
        raise RuntimeError(f"DiAgent container for agent {agent_id} failed health check")

    return url


async def stop_agent(agent_id: str, db: AsyncSession) -> None:
    runtime = await db.get(AgentRuntime, agent_id)
    if not runtime:
        return
    client = get_client()
    try:
        c = client.containers.get(runtime.container_id)
        c.stop(timeout=10)
        c.remove(force=True)
    except (NotFound, APIError):
        pass
    await db.delete(runtime)
    await db.commit()
