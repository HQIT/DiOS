import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.config import settings
from app.models.tables import Agent
from app.models.schemas import AgentCreate, AgentUpdate, AgentOut

router = APIRouter(prefix="/agents", tags=["agents"])

_SENSITIVE_KEYWORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def _mask_env(env: dict | None) -> dict:
    """对含敏感关键字的 env value 做脱敏展示（保留最后 4 位便于核对）。"""
    if not env:
        return {}
    masked: dict = {}
    for k, v in env.items():
        key_u = str(k).upper()
        if any(kw in key_u for kw in _SENSITIVE_KEYWORDS) and isinstance(v, str) and v:
            tail = v[-4:] if len(v) >= 4 else ""
            masked[k] = f"***{tail}"
        else:
            masked[k] = v
    return masked


def _agent_to_out(agent: Agent, *, reveal: bool = False) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "mode": agent.mode,
        "group": agent.group,
        "role": agent.role,
        "description": agent.description,
        "model": agent.model,
        "system_prompt": agent.system_prompt,
        "skills": agent.skills or [],
        "mcp_config_path": agent.mcp_config_path,
        "mcp_server_ids": agent.mcp_server_ids or [],
        "workspace_path": agent.workspace_path,
        "capabilities": agent.capabilities or {},
        "env": (agent.env or {}) if reveal else _mask_env(agent.env),
        "created_at": agent.created_at,
    }


@router.get("", response_model=list[AgentOut])
async def list_agents(
    group: str | None = Query(None),
    mode: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    query = select(Agent).order_by(Agent.created_at)
    if group:
        query = query.where(Agent.group == group)
    if mode:
        query = query.where(Agent.mode == mode)
    result = await db.execute(query)
    return [_agent_to_out(a) for a in result.scalars().all()]


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(body: AgentCreate, db: AsyncSession = Depends(get_db)):
    agent_id = uuid.uuid4().hex[:12]
    workspace = body.workspace_path.strip()
    if not workspace:
        workspace = str(settings.workspace_root / agent_id)
    Path(workspace).mkdir(parents=True, exist_ok=True)

    agent = Agent(
        id=agent_id,
        name=body.name,
        mode=body.mode,
        group=body.group,
        role=body.role,
        description=body.description,
        model=body.model,
        system_prompt=body.system_prompt,
        skills=body.skills,
        mcp_config_path=body.mcp_config_path,
        mcp_server_ids=getattr(body, "mcp_server_ids", []) or [],
        workspace_path=workspace,
        capabilities=getattr(body, "capabilities", {}) or {},
        env=getattr(body, "env", {}) or {},
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return _agent_to_out(agent, reveal=True)


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    reveal: bool = Query(False, description="是否明文返回 env（单机调试用）"),
    db: AsyncSession = Depends(get_db),
):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    return _agent_to_out(agent, reveal=reveal)


@router.put("/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: str, body: AgentUpdate, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    updates = body.model_dump(exclude_unset=True)
    # env 的 ***value 视为"未变更"，避免前端回传脱敏值覆盖真实凭据
    if "env" in updates and isinstance(updates["env"], dict):
        existing = agent.env or {}
        merged: dict = {}
        for k, v in updates["env"].items():
            if isinstance(v, str) and v.startswith("***"):
                merged[k] = existing.get(k, "")
            else:
                merged[k] = v
        updates["env"] = merged
    for field, value in updates.items():
        setattr(agent, field, value)
    await db.commit()
    await db.refresh(agent)
    return _agent_to_out(agent, reveal=True)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")
    await db.delete(agent)
    await db.commit()
