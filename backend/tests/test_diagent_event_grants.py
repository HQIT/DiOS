"""验证 Agent 事件凭据仅能访问发布工具所需接口和获准事件。"""

import unittest
from hashlib import sha256
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import DiAgentEventGrant, Settings, settings
from app.db.database import Base, get_db
from app.models.tables import Agent, EventLog
from main import app


class DiAgentEventGrantTests(unittest.IsolatedAsyncioTestCase):
    def test_each_grant_requires_its_own_non_admin_credential(self):
        grant = DiAgentEventGrant(token_sha256=sha256(b"fixture-event-key").hexdigest(),
                                 source_patterns=["manual/*"], event_types=["manual.trigger"])
        with self.assertRaisesRegex(ValueError, "differ from DIOS_ACCESS_TOKEN") as error:
            Settings(access_token="fixture-event-key", diagent_event_grants={"reviewer": grant})
        self.assertNotIn("fixture-event-key", str(error.exception))
        with self.assertRaisesRegex(ValueError, "distinct token"):
            Settings(access_token="fixture-admin", diagent_event_grants={"writer": grant, "reviewer": grant})

    async def test_grant_is_limited_by_api_source_and_event_type(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            actor = Agent(name="Publisher")
            db.add(actor)
            await db.commit()

        async def test_db():
            async with sessions() as db:
                yield db

        self.addCleanup(app.dependency_overrides.pop, get_db)
        app.dependency_overrides[get_db] = test_db
        token = "fixture-scoped-event-token"
        grant = DiAgentEventGrant(token_sha256=sha256(token.encode()).hexdigest(), source_patterns=["manual/fixture"], event_types=["manual.trigger"])
        auth = {"Authorization": f"Bearer {token}"}
        request = {"source": "manual/fixture", "event_type": "manual.trigger", "data": {}}
        with (
            patch.object(settings, "access_token", "fixture-admin"),
            patch.object(settings, "diagent_event_grants", {actor.id: grant}),
            patch.object(settings, "e2ag_mode", "off"),
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dios") as client:
                for path in ("/api/os/events/catalog", "/api/os/connectors/source-patterns"):
                    self.assertEqual((await client.get(path, headers=auth)).status_code, 200)
                self.assertEqual((await client.get("/api/os/models", headers=auth)).status_code, 401)
                for change in ({"source": "manual/other"}, {"event_type": "cron.tick"}):
                    response = await client.post("/api/os/events/manual", json={**request, **change}, headers=auth)
                    self.assertEqual(response.status_code, 403)
                denied = await client.post("/api/os/events/manual", json=request, headers={"Authorization": "Bearer wrong"})
                self.assertEqual(denied.status_code, 401)
                allowed = await client.post("/api/os/events/manual", json=request, headers=auth)
                self.assertEqual(allowed.status_code, 200, allowed.text)
        async with sessions() as db:
            events = (await db.scalars(select(EventLog))).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].cloud_event["diagentid"], actor.id)
