"""管理对话和任务的推理授权，供应商凭据只在 DiOS 查询。"""

import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import HTTPException
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.config import settings
from app.models.tables import A2ATask, Agent, AgentRuntime, LLMModel, ModelInferenceGrant, TaskInferenceGrant, TaskInferenceRefresh
from app.services.docker_runner import _host_path
from app.services.e2ag_tool_gateway import (
    close_service_mcp_sessions, issue_service_mcp_grants, remove_service_mcp_grants, service_mcp_sessions,
)
from app.services.service_skills import INLINE_SKILL_DIR

MODEL_PROXY_BASE_PATH = "/api/internal/model-proxy/v1"
MODEL_PROXY_PATH = MODEL_PROXY_BASE_PATH + "/chat/completions"
MODEL_GRANT_CHECK_PATH = MODEL_PROXY_BASE_PATH + "/grants/check"
MODEL_GRANT_REFRESH_PATH = MODEL_PROXY_BASE_PATH + "/grants/refresh"
MODEL_GRANT_HEADER = "X-DiAgent-Model-Grant"
MAX_MODEL_CALLS = 32
GRANT_TTL_SECONDS = 300
DEFAULT_TASK_TIMEOUT_MS = 1_800_000
# Docker 分步创建、写入私有配置、启动容器，且每步都有独立超时。
TASK_GRANT_STARTUP_GRACE_SECONDS = 120
MAX_TASK_MODEL_CALLS = 128


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


async def check_model_workspace(db: AsyncSession, agent: Agent) -> None:
    """防止兼容模式写入其他受管 Agent 可见的工作区。"""
    if not (settings.diagent_service_model_proxy or settings.diagent_task_model_proxy):
        return
    managed = settings.diagent_task_model_proxy if agent.mode == "task" else settings.diagent_service_model_proxy
    workspace = Path(agent.workspace_path).resolve()
    root = settings.workspace_root.resolve()
    shared = [root / "skills", root / "cli", root / INLINE_SKILL_DIR, settings.shared_cli_root.resolve()]
    if any(_overlap(workspace, path.resolve()) for path in shared):
        raise RuntimeError("Agent workspace overlaps a shared mount used for managed inference")
    if settings.host_shared_cli_root and _overlap(Path(_host_path(workspace)), Path(settings.host_shared_cli_root)):
        raise RuntimeError("Agent workspace overlaps the shared CLI mount")
    others = select(Agent.workspace_path).where(Agent.id != agent.id, Agent.workspace_path != "")
    if agent.mode == "task":
        runtime = await db.get(AgentRuntime, agent.id)
        if runtime is not None and runtime.status == "running":
            raise RuntimeError("Stop the Agent service before dispatching a one-shot task in its workspace")
    if managed:
        if root not in workspace.parents:
            raise RuntimeError("Managed inference requires a dedicated Agent workspace under the DiOS workspace root")
        for filename in ("models.yaml", "mcp_servers.json"):
            path = workspace / filename
            if path.exists() or path.is_symlink():
                raise RuntimeError(f"Move legacy {filename} out of the Agent workspace before starting managed inference")
        if next(workspace.glob("agent-task-*.json"), None) is not None:
            raise RuntimeError("Move legacy task configs out of the Agent workspace before starting managed inference")
    else:
        other_mode = "service" if agent.mode == "task" else "task"
        others = others.where(Agent.mode == other_mode)
    for other in (await db.scalars(others)).all():
        path = Path(other).resolve()
        if _overlap(path, workspace):
            raise RuntimeError("Managed inference requires non-overlapping Agent workspaces")


async def resolve_agent_model(db: AsyncSession, name: str | None) -> LLMModel | None:
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


def has_task_model_grant() -> ColumnElement[bool]:
    """用于识别已准备模型配置的 A2A 任务，即使凭据已过期。"""
    return exists().where(TaskInferenceGrant.task_id == A2ATask.id)


async def _issue_grant(
    db: AsyncSession, agent_id: str, model: LLMModel | None, *, ttl: int, max_calls: int,
) -> tuple[ModelInferenceGrant, str]:
    if not settings.access_token:
        raise HTTPException(503, "Model proxy requires the DiOS control plane access token")
    if model is None or model.provider not in ("openai", "vllm"):
        raise HTTPException(400, "Agent requires an explicitly configured OpenAI-compatible model")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    await remove_service_mcp_grants(db)
    # 保留任务关联供启动恢复，直到对应任务收尾时删除。
    renewable = select(TaskInferenceGrant.token_hash).join(TaskInferenceRefresh, TaskInferenceRefresh.task_id == TaskInferenceGrant.task_id)
    await db.execute(delete(ModelInferenceGrant).where(
        ModelInferenceGrant.expires_at <= now, ModelInferenceGrant.token_hash.not_in(renewable),
    ))
    grant = ModelInferenceGrant(
        token_hash=_token_hash(token), agent_id=agent_id, model_id=model.id, model_name=model.model,
        expires_at=now + ttl, remaining_calls=max_calls,
    )
    db.add(grant)
    return grant, token


def managed_task_timeout_ms(raw: str | None) -> int:
    """解析同时交给推理授权与 DSH 任务进程的运行时限。"""
    if raw is None:
        return DEFAULT_TASK_TIMEOUT_MS
    text = str(raw).strip()
    invalid_timeout = "DIAGENT_TASK_TIMEOUT_MS must be an integer between 1 and 2147483647"
    if not text.isascii() or "_" in text:
        raise ValueError(invalid_timeout)
    try:
        value = float(int(text, 0)) if text.lower().startswith(("0b", "0o", "0x")) else float(text)
    except (ValueError, OverflowError):
        raise ValueError(invalid_timeout) from None
    # DSH 进程使用 Node.js 定时器，其上限是有符号 32 位整数。
    if not value.is_integer() or not 1 <= value <= 2_147_483_647:
        raise ValueError(invalid_timeout)
    return int(value)


async def issue_task_model_config(
    db: AsyncSession, task: A2ATask, model: LLMModel | None, *, timeout_ms: int,
) -> tuple[dict, dict | None]:
    """生成容器用的模型配置，使用指定任务和模型的短期凭据。由调用者在启动前提交。"""
    lifetime = (timeout_ms + 999) // 1000 + TASK_GRANT_STARTUP_GRACE_SECONDS
    ttl = min(lifetime, settings.diagent_task_model_access_ttl_seconds) if settings.diagent_task_model_refresh else lifetime
    grant, token = await _issue_grant(
        db, task.agent_id, model, ttl=ttl, max_calls=MAX_TASK_MODEL_CALLS,
    )
    db.add(TaskInferenceGrant(task_id=task.id, token_hash=grant.token_hash))
    model_config = {
        "provider": "openai",
        "model": grant.model_name,
        "base_url": settings.e2ag_internal_base_url.rstrip("/") + MODEL_PROXY_BASE_PATH,
        "api_key": token,
        **({"context_length": model.context_length} if model and model.context_length else {}),
    }
    if not settings.diagent_task_model_refresh:
        return model_config, None
    refresh_token = secrets.token_urlsafe(32)
    db.add(TaskInferenceRefresh(task_id=task.id, token_hash=_token_hash(refresh_token),
                                expires_at=grant.expires_at - ttl + lifetime))
    return model_config, {"refresh_token": refresh_token}


async def refresh_task_model_grant(db: AsyncSession, authorization: str) -> str:
    """事务内换发访问令牌，沿用原模型和任务额度。"""
    scheme, _, refresh_token = authorization.partition(" ")
    denied = HTTPException(401, "Invalid or expired task refresh grant")
    if scheme.lower() != "bearer" or len(refresh_token) != 43:
        raise denied
    refresh = await db.scalar(select(TaskInferenceRefresh).where(
        TaskInferenceRefresh.token_hash == _token_hash(refresh_token),
    ))
    if refresh is None:
        raise denied
    # 与取消/完成共用任务行写锁，避免终态提交后重新签发。
    active = await db.scalar(update(A2ATask).where(
        A2ATask.id == refresh.task_id, A2ATask.status.in_(("submitted", "working")),
    ).values(status=A2ATask.status).returning(A2ATask.agent_id))
    now = int(time.time())
    if active is None or refresh.expires_at <= now:
        raise denied
    binding = await db.get(TaskInferenceGrant, refresh.task_id)
    grant = await db.get(ModelInferenceGrant, binding.token_hash) if binding else None
    if grant is None or grant.agent_id != active:
        raise denied
    if grant.remaining_calls <= 0:
        raise HTTPException(429, "Model inference grant exhausted")
    token = secrets.token_urlsafe(32)
    grant.token_hash = binding.token_hash = _token_hash(token)
    grant.expires_at = min(refresh.expires_at, now + settings.diagent_task_model_access_ttl_seconds)
    await db.commit()
    return token


def task_inference_env(env: dict, model: LLMModel | None) -> dict:
    """保留业务环境变量，剔除模型凭据和控制面令牌的常用键及别名。"""
    blocked_keys = {"OPENAI_API_KEY", "LLM_OPENAI_API_KEY", "DIAGENT_MODEL_API_KEY",
                    "LLM_MODELS_CONFIG_PATH", "DIOS_ACCESS_TOKEN"}
    credentials = {value.strip() for value in (settings.access_token, model.api_key if model else None) if value}

    def is_credential(value: object) -> bool:
        if not isinstance(value, str):
            return False
        value = value.strip()
        scheme, _, token = value.partition(" ")
        return value in credentials or (scheme.lower() == "bearer" and token.strip() in credentials)

    return {key: value for key, value in env.items() if key.upper() not in blocked_keys and not is_credential(value)}


async def revoke_task_model_grant(db: AsyncSession, task_id: str) -> None:
    """撤销指定任务的推理授权，随任务终态事务提交。"""
    token = select(TaskInferenceGrant.token_hash).where(TaskInferenceGrant.task_id == task_id)
    await db.execute(delete(ModelInferenceGrant).where(ModelInferenceGrant.token_hash.in_(token)))
    await db.execute(delete(TaskInferenceGrant).where(TaskInferenceGrant.task_id == task_id))
    await db.execute(delete(TaskInferenceRefresh).where(TaskInferenceRefresh.task_id == task_id))


@asynccontextmanager
async def model_grant(
    db: AsyncSession, agent: Agent, model: LLMModel | None, *, include_mcp: bool = False,
) -> AsyncIterator[dict[str, str]]:
    """签发限定模型与调用次数的单轮凭据，转发结束后撤销。"""
    if not settings.diagent_service_model_proxy:
        yield {}
        return
    turn, token = await _issue_grant(db, agent.id, model, ttl=GRANT_TTL_SECONDS, max_calls=MAX_MODEL_CALLS)
    digest = turn.token_hash
    if include_mcp:
        await issue_service_mcp_grants(db, agent, turn, token)
    await db.commit()
    try:
        headers = {MODEL_GRANT_HEADER: token}
        if model and model.context_length and model.context_length > 0:
            headers["X-DiAgent-Model-Context-Length"] = str(model.context_length)
        yield headers
    finally:
        # SSE 断开后仍有短窗口撤销凭据，数据库不可达时由 TTL 兜底。
        sessions = []
        revoked = False
        with anyio.move_on_after(5, shield=True):
            if include_mcp:
                sessions = await service_mcp_sessions(db, digest)
            await remove_service_mcp_grants(db, digest)
            await db.execute(delete(ModelInferenceGrant).where(ModelInferenceGrant.token_hash == digest))
            await db.commit()
            revoked = True
        if revoked and sessions:
            with anyio.move_on_after(3, shield=True):
                await close_service_mcp_sessions(sessions)


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


async def _task_scope(db: AsyncSession, grant: ModelInferenceGrant) -> str | None:
    return await db.scalar(select(TaskInferenceGrant.task_id).where(TaskInferenceGrant.token_hash == grant.token_hash))


async def verify_model_grant(db: AsyncSession, authorization: str, model_name: str, agent_id: str) -> ModelInferenceGrant:
    """核对常驻容器自己的 Agent ID，防止共用模型的 Agent 之间串用授权。"""
    grant = await _read_grant(db, authorization, model_name)
    if await _task_scope(db, grant) is not None:
        raise HTTPException(401, "Expected a service inference grant")
    if grant.agent_id != agent_id:
        raise HTTPException(403, "Agent is outside this inference grant")
    if grant.remaining_calls <= 0:
        raise HTTPException(429, "Model inference grant exhausted")
    return grant


async def reserve_model_call(db: AsyncSession, authorization: str, model_name: str) -> LLMModel:
    """校验推理授权并原子扣减次数，避免并发请求超出授权额度。"""
    grant = await _read_grant(db, authorization, model_name)
    task_id = await _task_scope(db, grant)
    if task_id is None:
        if not settings.diagent_service_model_proxy:
            raise HTTPException(401, "Service inference grants are disabled")
        active_task = ()
    else:
        if not settings.diagent_task_model_proxy:
            raise HTTPException(401, "Task inference grants are disabled")
        # 容器可以在派发请求写入 working 之前开始推理。
        active = exists().where(A2ATask.id == task_id, A2ATask.agent_id == grant.agent_id,
                                A2ATask.status.in_(("submitted", "working")))
        if not await db.scalar(select(active)):
            raise HTTPException(401, "Task inference grant is no longer active")
        active_task = (active,)
    model = await db.get(LLMModel, grant.model_id)
    if model is None or model.model != grant.model_name or model.provider not in ("openai", "vllm"):
        raise HTTPException(403, "Model is no longer authorized")
    grant_scope = (
        ModelInferenceGrant.token_hash == grant.token_hash,
        *active_task,
    )
    result = await db.execute(update(ModelInferenceGrant).where(
        *grant_scope,
        ModelInferenceGrant.expires_at > int(time.time()),
        ModelInferenceGrant.remaining_calls > 0,
    ).values(remaining_calls=ModelInferenceGrant.remaining_calls - 1))
    if not result.rowcount:
        # 校验和扣减之间可能发生过期或任务撤销。
        is_valid = await db.scalar(select(exists().where(
            *grant_scope, ModelInferenceGrant.expires_at > int(time.time()),
        )))
        if not is_valid:
            raise HTTPException(401, "Invalid or expired model inference grant")
        raise HTTPException(429, "Model inference grant exhausted")
    await db.commit()
    return model
