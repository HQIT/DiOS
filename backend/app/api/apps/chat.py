"""Chat App API — 会话管理 + 消息持久化 + 通过 Runtime Manager 路由到 DiAgent。"""

import uuid
import httpx
import json
import logging
import re
import datetime as dt
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import Agent, ChatSession, ChatMessage
from app.services.agent_runtime import ensure_running
from app.services.http_internal import internal_client

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


class _OwnedStreamingResponse(StreamingResponse):
    def __init__(self, *args, owner, **kwargs):
        self.owner = owner
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        import anyio
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.owner.close("SERVICE_STREAM_INTERRUPTED")


def _extract_hil_pending(result_text: str) -> dict | None:
    """从工具输出文本中提取 pending_hil 结构化信息。"""
    if not result_text or "pending_hil" not in result_text:
        return None
    token = None
    method = None
    path = None
    message = None
    expires_in_seconds = 600
    token_match = re.search(r'"token"\s*:\s*"([^"]+)"', result_text, re.IGNORECASE)
    method_match = re.search(r'"method"\s*:\s*"([^"]+)"', result_text, re.IGNORECASE)
    path_match = re.search(r'"path"\s*:\s*"([^"]+)"', result_text, re.IGNORECASE)
    msg_match = re.search(r'"message"\s*:\s*"([^"]+)"', result_text, re.IGNORECASE)
    expires_match = re.search(r'"expires_in_seconds"\s*:\s*(\d+)', result_text, re.IGNORECASE)
    if token_match:
        token = token_match.group(1)
    if method_match:
        method = method_match.group(1)
    if path_match:
        path = path_match.group(1)
    if msg_match:
        message = msg_match.group(1)
    if expires_match:
        try:
            expires_in_seconds = int(expires_match.group(1))
        except Exception:
            expires_in_seconds = 600
    if not token:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(seconds=expires_in_seconds)
    return {
        "type": "pending",
        "token": token,
        "method": method or "",
        "path": path or "",
        "message": message or "high-risk action requires HIL confirmation",
        "created_at": now.isoformat(),
        "expires_in_seconds": expires_in_seconds,
        "expires_at": expires_at.isoformat(),
        "next_confirm_command": f"dios request --confirm-token {token}",
    }


@router.post("/completions")
async def chat_completions(request: Request, db: AsyncSession = Depends(get_db)):
    from app.services.managed_service_recovery import CallOwner
    from app.runtime.database import RuntimeErrorCode
    body = await request.json()
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "agent_id is required")
    if not await db.get(Agent, agent_id):
        raise HTTPException(404, "Agent not found")
    owner = CallOwner()
    try:
        call_id = await owner.begin(db, agent_id)
    except RuntimeErrorCode as exc:
        raise HTTPException(503, detail=exc.code) from None
    try:
        response = await _chat_completions(request, db, call_id=call_id)
    except BaseException as exc:
        await owner.close(exc.code if isinstance(exc, RuntimeErrorCode) else type(exc).__name__)
        raise
    if isinstance(response, StreamingResponse):
        original = response.body_iterator
        async def tracked():
            try:
                async for chunk in original:
                    yield chunk
            finally:
                try:
                    await original.aclose()
                finally:
                    await owner.close("SERVICE_STREAM_INCOMPLETE")
        response = _OwnedStreamingResponse(
            tracked(), owner=owner, status_code=response.status_code,
            headers=dict(response.headers), media_type=response.media_type,
            background=response.background,
        )
    else:
        await owner.close()
    return response


async def _chat_completions(request: Request, db: AsyncSession, *, call_id):
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
        if session and session.agent_id != agent_id:
            raise HTTPException(409, "CHAT_SESSION_AGENT_MISMATCH")
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
        llm = await db.get(LLMModel, agent.model)
        if llm is None:
            llm_result = await db.execute(select(LLMModel).where(LLMModel.name == agent.model))
            llm = llm_result.scalar_one_or_none()
        if llm is None:
            llm_result = await db.execute(select(LLMModel).where(LLMModel.model == agent.model))
            llm = llm_result.scalar_one_or_none()
        if llm:
            model_name = llm.model

    # 确保 Agent 容器运行中
    try:
        diagent_url = await ensure_running(agent_id, db, call_id=call_id)
    except Exception as e:
        raise HTTPException(503, f"Failed to start agent service: {e}")

    payload = {
        "model": model_name,
        "messages": messages,
        "stream": body.get("stream", True),
    }
    # 声明了 skills 的 agent 默认启用内置协作工具（其余行为由 prompt + skills 约束）
    if agent.skills:
        payload["tool_selection"] = {"tool_ids": ["shell", "publish_event"]}
    capabilities = (agent.capabilities or {}) if isinstance(agent.capabilities, dict) else {}
    reasoning = capabilities.get("reasoning")
    if isinstance(reasoning, dict) and reasoning:
        payload["custom_fields"] = {"reasoning": reasoning}

    if payload["stream"]:
        async def _stream():
            from app.services import managed_admission
            upstream_done = False
            collected: list[str] = []
            invalid_result = False
            current_event: str | None = None
            try:
                await managed_admission.dispatch(call_id)
                async with internal_client(timeout=httpx.Timeout(300, connect=10)) as client:
                    async with client.stream("POST", f"{diagent_url}/v1/chat/completions", json=payload) as resp:
                        if resp.status_code != 200:
                            error = await resp.aread()
                            msg = error.decode(errors="replace")
                            yield f"data: {json.dumps({'error': msg}, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        async for line in resp.aiter_lines():
                            hil_pending = None
                            if line.startswith("data:") and line[5:].strip() == "[DONE]":
                                upstream_done = True
                            if line.startswith("event: "):
                                current_event = line[len("event: "):].strip()
                            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                                try:
                                    p = json.loads(line[5:].lstrip())
                                    choice = (p.get("choices") or [{}])[0]
                                    if "error" in p or choice.get("finish_reason") not in {None, "stop"}:
                                        invalid_result = True
                                    delta = choice.get("delta", {}).get("content")
                                    if isinstance(delta, str):
                                        collected.append(delta)
                                except Exception:
                                    invalid_result = True
                                if current_event == "tool_call":
                                    try:
                                        tool_evt = json.loads(line[5:].lstrip())
                                        if tool_evt.get("type") == "tool_call_end":
                                            hil_pending = _extract_hil_pending(str(tool_evt.get("result") or ""))
                                    except Exception:
                                        pass
                            # Persisted partial content includes the last emitted delta.
                            yield line + "\n"
                            if hil_pending:
                                yield "\nevent: hil\n"
                                yield f"data: {json.dumps(hil_pending, ensure_ascii=False)}\n\n"
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout) as e:
                logger.warning("chat stream interrupted: %s", e)
                yield f"data: {json.dumps({'error': f'stream interrupted: {type(e).__name__}'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception("chat stream failed")
                yield f"data: {json.dumps({'error': f'internal stream error: {type(e).__name__}'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                import anyio
                with anyio.CancelScope(shield=True):
                    from app.services import managed_service_results
                    content = "".join(collected)
                    await managed_service_results.retain(
                        call_id, "chat", session_id, content,
                        complete=upstream_done and not invalid_result and bool(content.strip()),
                        evidence={"transport": "sse", "upstream_done": upstream_done,
                                  "invalid_result": invalid_result},
                    )
                    await managed_service_results.apply(call_id)

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "X-Session-Id": session_id},
        )

    # 非流式
    from app.services import managed_admission
    await managed_admission.dispatch(call_id)
    async with internal_client(timeout=httpx.Timeout(300, connect=10)) as client:
        resp = await client.post(f"{diagent_url}/v1/chat/completions", json=payload)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, resp.text)
        result = resp.json()

    from app.services.managed_service_recovery import reply_content
    from app.runtime.database import RuntimeErrorCode
    from app.services import managed_service_results
    try:
        assistant_content = reply_content(result)
        complete = True
    except RuntimeErrorCode:
        assistant_content, complete = json.dumps(result, ensure_ascii=False), False
    await managed_service_results.retain(call_id, "chat", session_id, assistant_content,
                                        complete=complete, evidence={"transport": "http-json", "response": result})
    await managed_service_results.apply(call_id)
    if not complete:
        raise HTTPException(502, "SERVICE_RESULT_INCOMPLETE")

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


# ── Agent-initiated Delivery ──
# 主 Agent 完成外部事件处理后,主动把文案投递回 Chat 会话
# 对标 Hermes gateway/delivery.py


@router.post("/sessions/{session_id}/deliveries")
async def post_delivery(
    session_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Agent-initiated delivery: Agent 主动把一条消息投递到 Chat 会话.

    请求体:
        content: 消息文本 (必填)
        in_reply_to_task_id: 关联的 A2ATask id (可选, 便于前端定位来源)

    消息以 role="assistant" 存入 Chat 历史,前端按常规助手消息展示.
    """
    content = (body or {}).get("content")
    if not content or not isinstance(content, str):
        raise HTTPException(400, "content is required and must be a string")

    session = await db.get(ChatSession, session_id)
    if not session:
        raise HTTPException(404, f"ChatSession {session_id} not found")

    in_reply_to = (body or {}).get("in_reply_to_task_id") or ""

    msg = ChatMessage(
        session_id=session_id,
        role="assistant",
        content=content,
    )
    db.add(msg)
    # touch session updated_at 便于前端会话排序
    import datetime
    session.updated_at = datetime.datetime.now(datetime.timezone.utc)
    await db.commit()
    await db.refresh(msg)

    return {
        "id": msg.id,
        "session_id": session_id,
        "role": msg.role,
        "content": msg.content,
        "in_reply_to_task_id": in_reply_to,
        "created_at": msg.created_at.isoformat(),
    }


# 独立 session 用于流式回调中保存消息
from contextlib import asynccontextmanager
from app.db.database import async_session

@asynccontextmanager
async def get_db_session():
    async with async_session() as session:
        yield session
