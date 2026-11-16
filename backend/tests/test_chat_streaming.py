"""通过 Chat HTTP 入口验证工具流和审批提示独立分帧。"""

import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import Base, get_db
from app.models.tables import Agent, ChatMessage, ChatSession
from main import app


class ChatStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        async def test_db():
            async with self.sessions() as db:
                yield db

        self.addCleanup(app.dependency_overrides.pop, get_db)
        app.dependency_overrides[get_db] = test_db

    async def test_tool_end_and_confirmation_reach_client_as_separate_frames(self):
        async with self.sessions() as db:
            agent = Agent(name="Reviewer", skills=["review"])
            db.add(agent)
            await db.flush()
            session = ChatSession(id="existing-review-session", agent_id=agent.id, title="Older review")
            db.add(session)
            db.add_all([
                ChatMessage(session_id=session.id, role="user", content="Call this draft Cedar"),
                ChatMessage(session_id=session.id, role="assistant", content="I will call it Cedar"),
            ])
            await db.commit()

        @asynccontextmanager
        async def save_session():
            async with self.sessions() as db:
                yield db

        command_confirmation = {"status": "pending_hil", "token": "0123456789_valid-token", "method": "POST"}
        tool_result = {"type": "tool_call_end", "tool_name": "shell", "result": json.dumps(command_confirmation)}
        upstream_frames = [
            'data: {"choices":[{"delta":{"reasoning_content":"Checking"}}]}',
            "event: tool_call\ndata: " + json.dumps(tool_result),
            "event: tool_call\ndata: " + json.dumps({**tool_result, "tool_name": "publish_event"}),
            'event: event_approval\ndata: {"event_id":"fixture-event"}',
            'data: {"choices":[{"delta":{"content":"Finished"}}]}',
            "data: [DONE]",
        ]

        def upstream(request):
            messages = json.loads(request.content)["messages"]
            self.assertEqual([(message["role"], message["content"]) for message in messages], [
                ("user", "Call this draft Cedar"), ("assistant", "I will call it Cedar"), ("user", "Run reviewer"),
            ])
            return httpx.Response(200, text="\n\n".join(upstream_frames) + "\n\n")

        with (
            patch.object(settings, "access_token", ""),
            patch.object(settings, "diagent_service_model_proxy", False),
            patch("app.api.apps.chat.ensure_running", new=AsyncMock(return_value="http://diagent")),
            patch("app.api.apps.chat.get_db_session", new=save_session),
            patch("app.api.apps.chat.internal_client", side_effect=lambda **opts: httpx.AsyncClient(transport=httpx.MockTransport(upstream), **opts)),
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dios") as client:
                response = await client.post("/api/apps/chat/completions", json={
                    "agent_id": agent.id, "session_id": session.id,
                    "messages": [{"role": "user", "content": "Run reviewer"}],
                })
        self.assertEqual(response.status_code, 200)
        blocks = [frame for frame in response.text.split("\n\n") if frame.strip()]
        self.assertEqual(blocks[0:2], upstream_frames[0:2])
        self.assertTrue(blocks[2].startswith("event: hil\ndata: "))
        self.assertEqual(json.loads(blocks[2].partition("data: ")[2])["token"], command_confirmation["token"])
        self.assertEqual(blocks[3:], upstream_frames[2:])
        async with self.sessions() as db:
            written = (await db.scalars(select(ChatMessage).where(
                ChatMessage.session_id == session.id, ChatMessage.role == "assistant",
            ).order_by(ChatMessage.created_at))).all()
            self.assertEqual([message.content for message in written], ["I will call it Cedar", "Finished"])

    async def test_chat_rejects_another_agents_session(self):
        async with self.sessions() as db:
            reviewer = Agent(name="Reviewer")
            writer = Agent(name="Writer")
            db.add_all([reviewer, writer])
            await db.flush()
            session = ChatSession(id="reviewer-session", agent_id=reviewer.id, title="Review")
            db.add(session)
            await db.commit()
        with (
            patch.object(settings, "access_token", ""),
            patch("app.api.apps.chat.ensure_running", new=AsyncMock(side_effect=RuntimeError("unexpected dispatch"))) as dispatch,
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dios") as client:
                response = await client.post("/api/apps/chat/completions", json={
                    "agent_id": writer.id, "session_id": session.id,
                    "messages": [{"role": "user", "content": "Write a draft"}],
                })
        self.assertEqual(response.status_code, 409)
        dispatch.assert_not_called()
