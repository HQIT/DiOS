"""管理常驻 Agent 的单轮推理授权，供应商凭据只在 DiOS 查询。"""

import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.tables import Agent, AgentRuntime, LLMModel, ModelInferenceGrant
from app.services.docker_runner import _host_path

MODEL_PROXY_BASE_PATH = "/api/internal/model-proxy/v1"
MODEL_PROXY_PATH = MODEL_PROXY_BASE_PATH + "/chat/completions"
MODEL_GRANT_CHECK_PATH = MODEL_PROXY_BASE_PATH + "/grants/check"
MODEL_GRANT_HEADER = "X-DiAgent-Model-Grant"
MAX_MODEL_CALLS = 32
MAX_OUTPUT_TOKENS = 4096
GRANT_TTL_SECONDS = 300


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


async def check_model_workspace(db: AsyncSession, agent: Agent) -> None:
    """防止一次性任务的含密钥配置进入受管服务的挂载目录。"""
    if not settings.diagent_service_model_proxy:
        return
    workspace = Path(agent.workspace_path).resolve()
    root = settings.workspace_root.resolve()
    shared = [root / "skills", root / "cli", settings.shared_cli_root.resolve()]
    if any(_overlap(workspace, path.resolve()) for path in shared):
        raise RuntimeError("Agent workspace overlaps a shared mount used for managed inference")
    if settings.host_shared_cli_root and _overlap(Path(_host_path(workspace)), Path(settings.host_shared_cli_root)):
        raise RuntimeError("Agent workspace overlaps the shared CLI mount")
    others = select(Agent.workspace_path).where(Agent.id != agent.id, Agent.workspace_path != "")
    if agent.mode == "task":
        runtime = await db.get(AgentRuntime, agent.id)
        if runtime is not None and runtime.status == "running":
            raise RuntimeError("Stop the Agent service before dispatching a one-shot task in its workspace")
        others = others.where(Agent.mode == "service")
    else:
        if root not in workspace.parents:
            raise RuntimeError("Managed inference requires a dedicated Agent workspace under the DiOS workspace root")
        for filename in ("models.yaml", "mcp_servers.json"):
            path = workspace / filename
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"Move legacy {filename} out of the Agent workspace before starting managed inference")
        if next(workspace.glob("agent-task-*.json"), None) is not None:
            raise RuntimeError("Move legacy task configs out of the Agent workspace before starting managed inference")
    for other in (await db.scalars(others)).all():
        path = Path(other).resolve()
        if _overlap(path, workspace):
            raise RuntimeError("Managed inference requires non-overlapping Agent workspaces")


async def resolve_service_model(db: AsyncSession, name: str | None) -> LLMModel | None:
    """按 Agent 保存的 ID、显示配置名或供应商模型名查找。"""
    if not name:
        return None
    model = await db.get(LLMModel, name)
    if model is not None:
        return model
    model = await db.scalar(select(LLMModel).where(LLMModel.name == name))
    if model is not None:
        return model
    matches = (await db.scalars(select(LLMModel).where(LLMModel.model == name).limit(2))).all()
    return matches[0] if len(matches) == 1 else None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@asynccontextmanager
async def model_grant(db: AsyncSession, agent: Agent, model: LLMModel | None) -> AsyncIterator[dict[str, str]]:
    """签发限定模型与调用次数的单轮凭据，转发结束后撤销。"""
    if not settings.diagent_service_model_proxy:
        yield {}
        return
    if not settings.access_token:
        raise HTTPException(503, "Model proxy requires the DiOS control plane access token")
    if model is None or model.provider not in ("openai", "vllm"):
        raise HTTPException(400, "Agent requires an explicitly configured OpenAI-compatible model")
    token = secrets.token_urlsafe(32)
    digest = _token_hash(token)
    now = int(time.time())
    await db.execute(delete(ModelInferenceGrant).where(ModelInferenceGrant.expires_at <= now))
    db.add(ModelInferenceGrant(
        token_hash=digest, agent_id=agent.id, model_id=model.id, model_name=model.model,
        expires_at=now + GRANT_TTL_SECONDS, remaining_calls=MAX_MODEL_CALLS,
    ))
    await db.commit()
    try:
        headers = {MODEL_GRANT_HEADER: token}
        if model.context_length and model.context_length > 0:
            headers["X-DiAgent-Model-Context-Length"] = str(model.context_length)
        yield headers
    finally:
        # SSE 断开后仍有短窗口撤销凭据，数据库不可达时由 TTL 兜底。
        with anyio.move_on_after(5, shield=True):
            await db.execute(delete(ModelInferenceGrant).where(ModelInferenceGrant.token_hash == digest))
            await db.commit()


async def _read_grant(db: AsyncSession, authorization: str, model_name: str) -> ModelInferenceGrant:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or len(token) != 43:
        raise HTTPException(401, "Invalid model inference grant")
    grant = await db.get(ModelInferenceGrant, _token_hash(token))
    if grant is None or grant.expires_at <= int(time.time()):
        raise HTTPException(401, "Invalid or expired model inference grant")
    if model_name != grant.model_name:
        raise HTTPException(403, "Model is outside this inference grant")
    return grant


async def verify_model_grant(db: AsyncSession, authorization: str, model_name: str, agent_id: str) -> None:
    """核对常驻容器自己的 Agent ID，防止共用模型的 Agent 之间串用授权。"""
    grant = await _read_grant(db, authorization, model_name)
    if grant.agent_id != agent_id:
        raise HTTPException(403, "Agent is outside this inference grant")
    if grant.remaining_calls <= 0:
        raise HTTPException(429, "Model inference grant exhausted")


async def reserve_model_call(db: AsyncSession, authorization: str, model_name: str) -> LLMModel:
    """校验推理授权并原子扣减次数，避免并发请求超出授权额度。"""
    grant = await _read_grant(db, authorization, model_name)
    model = await db.get(LLMModel, grant.model_id)
    if model is None or model.model != grant.model_name or model.provider not in ("openai", "vllm"):
        raise HTTPException(403, "Model is no longer authorized")
    result = await db.execute(update(ModelInferenceGrant).where(
        ModelInferenceGrant.token_hash == grant.token_hash,
        ModelInferenceGrant.expires_at > int(time.time()),
        ModelInferenceGrant.remaining_calls > 0,
    ).values(remaining_calls=ModelInferenceGrant.remaining_calls - 1))
    if not result.rowcount:
        raise HTTPException(429, "Model inference grant exhausted")
    await db.commit()
    return model
