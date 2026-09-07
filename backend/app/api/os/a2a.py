"""A2A (Agent-to-Agent) 协议端点。

实现：
- GET  /api/os/a2a/.well-known/agent-card.json         DiOS 平台级 Card
- GET  /api/os/a2a/agents/{agent_id}/.well-known/agent-card.json  单个 Agent Card
- POST /api/os/a2a/agents/{agent_id}                    JSON-RPC 2.0
    methods: message/send, tasks/get, tasks/cancel
- GET  /api/os/a2a/tasks/{task_id}                      简化查询（非协议必须，便于调试）
- GET  /api/os/a2a/tasks                                列表（调试用）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import Agent, A2ATask
from app.models.schemas import A2ATaskOut
from app.services import a2a_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/a2a", tags=["a2a"])


def _base_url(request: Request) -> str:
    scheme = request.url.scheme
    netloc = request.url.netloc
    return f"{scheme}://{netloc}"


# ── Agent Card 端点 ─────────────────────────────────────────────────


@router.get("/.well-known/agent-card.json")
async def platform_agent_card(request: Request):
    return a2a_service.build_platform_agent_card(_base_url(request))


@router.get("/agents/{agent_id}/.well-known/agent-card.json")
async def agent_card(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return a2a_service.build_agent_card(agent, _base_url(request))


# ── JSON-RPC 2.0 端点 ──────────────────────────────────────────────


def _jsonrpc_error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def _jsonrpc_result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


@router.post("/agents/{agent_id}")
async def agent_jsonrpc(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """A2A JSON-RPC 2.0 端点。"""
    try:
        payload = await request.json()
    except Exception:
        return _jsonrpc_error(None, -32700, "Parse error")

    if not isinstance(payload, dict):
        return _jsonrpc_error(None, -32600, "Invalid Request (expect JSON object)")

    req_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(method, str):
        return _jsonrpc_error(req_id, -32600, "Invalid Request (method missing)")

    agent = await db.get(Agent, agent_id)
    if not agent:
        return _jsonrpc_error(req_id, -32001, f"Agent {agent_id} not found")

    try:
        if method in ("message/send", "message.send"):
            message = params.get("message") or {}
            context_id = params.get("contextId") or params.get("context_id") or ""
            if not isinstance(message, dict):
                return _jsonrpc_error(req_id, -32602, "Invalid params: message required")
            task = await a2a_service.send_message(
                db=db,
                agent_id=agent_id,
                message=message,
                context_id=context_id,
            )
            return _jsonrpc_result(req_id, a2a_service.task_to_a2a_dict(task))

        if method in ("tasks/get", "tasks.get"):
            task_id = params.get("id") or params.get("taskId")
            if not task_id:
                return _jsonrpc_error(req_id, -32602, "Invalid params: id required")
            task = await db.get(A2ATask, task_id)
            if not task:
                return _jsonrpc_error(req_id, -32001, f"Task {task_id} not found")
            if task.agent_id != agent_id:
                return _jsonrpc_error(req_id, -32001, "Task does not belong to this agent")
            return _jsonrpc_result(req_id, a2a_service.task_to_a2a_dict(task))

        if method in ("tasks/cancel", "tasks.cancel"):
            task_id = params.get("id") or params.get("taskId")
            if not task_id:
                return _jsonrpc_error(req_id, -32602, "Invalid params: id required")
            task = await db.get(A2ATask, task_id)
            if not task:
                return _jsonrpc_error(req_id, -32001, f"Task {task_id} not found")
            if task.agent_id != agent_id:
                return _jsonrpc_error(req_id, -32001, "Task does not belong to this agent")
            task = await a2a_service.cancel_task(db, task_id)
            return _jsonrpc_result(req_id, a2a_service.task_to_a2a_dict(task))

        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    except ValueError as e:
        return _jsonrpc_error(req_id, -32001, str(e))
    except Exception as e:
        logger.exception("JSON-RPC method %s failed", method)
        return _jsonrpc_error(req_id, -32603, f"Internal error: {type(e).__name__}: {e}")


# ── 查询端点（便于 UI/调试） ────────────────────────────────────────


@router.get("/tasks", response_model=dict)
async def list_tasks(
    agent_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func

    filters = []
    if agent_id:
        filters.append(A2ATask.agent_id == agent_id)
    if status:
        filters.append(A2ATask.status == status)

    count_stmt = select(func.count(A2ATask.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    query = select(A2ATask).order_by(A2ATask.created_at.desc())
    if filters:
        query = query.where(*filters)
    query = query.limit(limit).offset(offset)

    items = list((await db.execute(query)).scalars().all())

    return {
        "items": [A2ATaskOut.model_validate(it) for it in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/tasks/{task_id}", response_model=A2ATaskOut)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)):
    task = await db.get(A2ATask, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task
