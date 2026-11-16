"""Agent Runtime Manager — 为 service 模式 Agent 管理独立的 DiAgent 容器。"""

import json
import logging
import os
import asyncio
from pathlib import Path

import yaml
import httpx

from app.services.http_internal import internal_client
import docker
from docker.errors import NotFound, APIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import A2ATask, Agent, AgentRuntime, LLMModel, McpServer
from app.services.docker_runner import get_client, _host_path
from app.services.model_proxy import MODEL_PROXY_BASE_PATH, check_model_workspace
from app.services.service_skills import service_skill_sources

logger = logging.getLogger(__name__)

DOCKER_NETWORK = os.getenv("DIOS_DOCKER_NETWORK", "nana-os_default")


def _container_name(agent_id: str) -> str:
    return f"dios-agent-{agent_id}"


def _skill_mount_label(sources: dict[str, Path]) -> str:
    return json.dumps({name: str(path) for name, path in sources.items()}, sort_keys=True)


async def _sync_agent_workspace(agent: Agent, db: AsyncSession, skill_sources: dict[str, Path]) -> dict[str, str]:
    """生成常驻服务环境，受管推理模式只下发代理地址。"""
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
        "DIOS_API": os.environ.get("DIOS_API_INTERNAL", "http://backend:8000"),
    })

    if settings.diagent_service_model_proxy:
        for key in ("LLM_MODELS_CONFIG_PATH", "LLM_OPENAI_API_KEY", "OPENAI_API_KEY", "DIAGENT_MODEL_API_KEY", "MCP_CONFIG_PATH"):
            env.pop(key, None)
        # Agent 环境中的全局访问令牌能越过单轮推理授权，受管服务只接收限权凭据。
        env = {key: value for key, value in env.items() if value.strip() != settings.access_token}
        env["DIAGENT_MODEL_PROXY_URL"] = env["DIOS_API"].rstrip("/") + MODEL_PROXY_BASE_PATH
        env["DIAGENT_MODEL_PROXY_AGENT_ID"] = agent.id
        env["SKILLS_DIR"] = "/workspace/skills"
        env["DIAGENT_SKILL_NAMES"] = json.dumps(list(skill_sources))
    else:
        env["LLM_MODELS_CONFIG_PATH"] = "/workspace/models.yaml"
        models = {}
        for llm in (await db.scalars(select(LLMModel))).all():
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
    if mcp_ids and not settings.diagent_service_model_proxy:
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


def _start_container(agent: Agent, env: dict[str, str], skill_sources: dict[str, Path]) -> tuple[str, str]:
    """启动 DiAgent 服务容器，返回 (container_id, internal_url)。"""
    client = get_client()
    name = _container_name(agent.id)
    host_ws = _host_path(Path(agent.workspace_path))
    host_shared_cli = settings.host_shared_cli_root or _host_path(settings.shared_cli_root)
    volumes = {
        host_ws: {"bind": "/workspace", "mode": "rw"},
        host_shared_cli: {"bind": "/workspace/cli", "mode": "ro"},
    }
    labels = {"dios.agent_id": agent.id, "dios.type": "service",
              "dios.model_proxy": "enabled" if settings.diagent_service_model_proxy else "disabled"}
    skills_root = (settings.workspace_root / "skills").resolve()
    if settings.diagent_service_model_proxy:
        for skill_name, source in skill_sources.items():
            volumes[_host_path(source)] = {"bind": f"/workspace/skills/{skill_name}", "mode": "ro"}
        labels["dios.skills"] = _skill_mount_label(skill_sources)
    else:
        volumes[_host_path(skills_root)] = {"bind": "/workspace/skills", "mode": "ro"}

    try:
        old = client.containers.get(name)
        old.remove(force=True)
    except NotFound:
        pass

    container = client.containers.run(
        image=settings.diagent_service_image,
        name=name,
        labels=labels,
        environment=env,
        volumes=volumes,
        network=DOCKER_NETWORK,
        detach=True,
        auto_remove=False,
    )
    url = f"http://{name}:8000"
    logger.info("Started service container %s for agent %s at %s", container.short_id, agent.id, url)
    return container.id, url


async def _health_check(url: str, retries: int = 20, interval: float = 1.0) -> bool:
    for _ in range(retries):
        try:
            async with internal_client(timeout=2) as client:
                resp = await client.get(f"{url}/health")
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False


async def ensure_running(agent_id: str, db: AsyncSession) -> str:
    """确保 Agent 的 DiAgent 服务容器正在运行，返回其 URL。"""
    if settings.diagent_service_model_proxy and not settings.access_token:
        raise RuntimeError("Managed inference requires DIOS_ACCESS_TOKEN")
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise ValueError(f"Agent {agent_id} not found")
    if settings.diagent_service_model_proxy and agent.mode != "service":
        raise RuntimeError("Managed inference requires a service-mode Agent")
    await check_model_workspace(db, agent)
    skill_sources = await service_skill_sources(agent.skills, db) if settings.diagent_service_model_proxy else {}
    runtime = await db.get(AgentRuntime, agent_id)

    if runtime and runtime.status == "running":
        client = get_client()
        try:
            c = client.containers.get(runtime.container_id)
            c.reload()
            if c.status == "running":
                managed = c.labels.get("dios.model_proxy") == "enabled"
                if managed != settings.diagent_service_model_proxy:
                    raise RuntimeError("Stop the existing Agent service before changing its inference mode")
                if managed and c.labels.get("dios.skills", "{}") != _skill_mount_label(skill_sources):
                    raise RuntimeError("Stop the existing Agent service before changing its Skills")
                return runtime.url
        except NotFound:
            pass
        runtime.status = "stopped"
        await db.commit()

    env = await _sync_agent_workspace(agent, db, skill_sources)
    container_id, url = _start_container(agent, env, skill_sources)

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


async def has_active_workspace_mount(agent_id: str, db: AsyncSession) -> bool:
    """确认工作区尚被常驻容器或已派发的任务占用，避免修改记录后丢失挂载归属。"""
    runtime = await db.get(AgentRuntime, agent_id)
    reference = runtime.container_id if runtime else _container_name(agent_id)
    try:
        container = await asyncio.to_thread(lambda: get_client().containers.get(reference))
        if container.status not in ("exited", "dead"):
            return True
    except NotFound:
        pass
    working_task = await db.scalar(select(A2ATask.id).where(
        A2ATask.agent_id == agent_id, A2ATask.status == "working",
    ).limit(1))
    return working_task is not None
