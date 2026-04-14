"""Chat App API — 会话管理 + 消息持久化 + 通过 Runtime Manager 路由到 DiAgent。"""

import uuid
import httpx
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import Agent, ChatSession, ChatMessage
from app.services.agent_runtime import ensure_running

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/completions")
async def chat_completions(request: Request, db: AsyncSession = Depends(get_db)):
    body = await request.json()
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "agent_id is required")

    agent = await db.get(Agent, agent_id)
    if not agent:
        raise HTTPException(404, "Agent not found")

    session_id = body.get("session_id")
    user_content = ""
    raw_messages = body.get("messages", [])
    if raw_messages:
        last = raw_messages[-1]
        if last.get("role") == "user":
            user_content = last["content"]

    if not user_content:
        raise HTTPException(400, "No user message provided")

    # 创建或获取 session
    if not session_id:
        session_id = uuid.uuid4().hex
        session = ChatSession(id=session_id, agent_id=agent_id, title=user_content[:60])
        db.add(session)
        await db.commit()
    else:
        session = await db.get(ChatSession, session_id)
        if not session:
            session = ChatSession(id=session_id, agent_id=agent_id, title=user_content[:60])
            db.add(session)
            await db.commit()

    # 加载历史消息
    history_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    history = history_result.scalars().all()

    messages = []
    if agent.system_prompt:
        messages.append({"role": "system", "content": agent.system_prompt})
    for h in history:
        msg = {"role": h.role, "content": h.content}
        if h.role == "assistant":
            msg["tool_calls"] = []
        messages.append(msg)
    messages.append({"role": "user", "content": user_content})

    # 存 user message
    db.add(ChatMessage(session_id=session_id, role="user", content=user_content))
    await db.commit()

    # 获取模型名
    model_name = agent.model or ""
    from app.models.tables import LLMModel
    if agent.model:
        llm_result = await db.execute(select(LLMModel).where(LLMModel.name == agent.model))
        llm = llm_result.scalar_one_or_none()
        if llm:
            model_name = llm.model

    # 确保 Agent 容器运行中
    try:
        diagent_url = await ensure_running(agent_id, db)
    except Exception as e:
        raise HTTPException(503, f"Failed to start agent service: {e}")

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": body.get("stream", True),
    }

    if payload["stream"]:
        async def _stream():
            collected: list[str] = []
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                    async with client.stream("POST", f"{diagent_url}/v1/chat/completions", json=payload) as resp:
                        if resp.status_code != 200:
                            error = await resp.aread()
                            yield f"data: {{\"error\": \"{error.decode()}\"}}\n\n"
                            return
                        async for line in resp.aiter_lines():
                            yield line + "\n"
                            if line.startswith("data: ") and not line.startswith("data: [DONE]"):
                                try:
                                    import json
                                    p = json.loads(line[6:])
                                    delta = p.get("choices", [{}])[0].get("delta", {}).get("content")
                                    if delta:
                                        collected.append(delta)
                                except Exception:
                                    pass
            finally:
                if collected:
                    async with get_db_session() as save_db:
                        save_db.add(ChatMessage(
                            session_id=session_id, role="assistant", content="".join(collected)
                        ))
                        await save_db.commit()

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Session-Id": session_id},
        )

    # 非流式
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        resp = await client.post(f"{diagent_url}/v1/chat/completions", json=payload)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, resp.text)
        result = resp.json()

    assistant_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if assistant_content:
        db.add(ChatMessage(session_id=session_id, role="assistant", content=assistant_content))
        await db.commit()

    return result


# ── 会话管理接口 ──

@router.get("/agents/{agent_id}/sessions")
async def list_sessions(agent_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ChatSession)
        .where(ChatSession.agent_id == agent_id)
        .order_by(ChatSession.updated_at.desc())
    )
    sessions = result.scalars().all()
    return [
        {"id": s.id, "title": s.title, "created_at": s.created_at.isoformat(), "updated_at": s.updated_at.isoformat()}
        for s in sessions
    ]


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at)
    )
    msgs = result.scalars().all()
    return [
        {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
        for m in msgs
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
    await db.execute(delete(ChatSession).where(ChatSession.id == session_id))
    await db.commit()


# 独立 session 用于流式回调中保存消息
from contextlib import asynccontextmanager
from app.db.database import async_session

@asynccontextmanager
async def get_db_session():
    async with async_session() as session:
        yield session
