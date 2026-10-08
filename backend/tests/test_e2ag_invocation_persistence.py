"""Regression tests for durable intent and explicit uncertainty, not throughput."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.internal.e2ag_mcp import proxy_mcp_call
from app.db.database import Base
from app.models.tables import E2AGToolGrant, E2AGToolInvocation, EventLog
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_tool_gateway import record_tool_decision
import test_e2ag_tool_gateway as gateway_tests
from test_e2ag_tool_gateway import FakeAsyncClient, request_for


class InvocationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = gateway_tests.E2AGToolGatewayTests.asyncSetUp
    asyncTearDown = gateway_tests.E2AGToolGatewayTests.asyncTearDown
    _fixtures = gateway_tests.E2AGToolGatewayTests._fixtures

    def payload(self, tool="ledger.record_push"):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": {"private": "do-not-log"}}}

    async def records(self):
        async with self.sessions() as reader:
            invocations = (await reader.execute(select(E2AGToolInvocation))).scalars().all()
            event = await reader.get(EventLog, "event-tool")
            return invocations, event.audit_chain

    async def test_intent_commit_failure_prevents_upstream(self):
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch.object(db, "commit", side_effect=OperationalError("commit", {}, Exception("locked"))), \
                 patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(self.payload(), token), db)
            self.assertEqual(503, response.status_code)
            self.assertFalse(json.loads(response.body)["error"]["data"]["forwarded"])
            self.assertEqual([], FakeAsyncClient.calls)
        records, chain = await self.records()
        self.assertEqual([], records)
        self.assertFalse(any(x["stage"] == "tool_intent" for x in chain))

    async def test_result_commit_failure_retains_durable_unknown_and_no_resend(self):
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            grant_id, original_commit = grant.id, db.commit
            commits = 0
            async def failing_second_commit():
                nonlocal commits
                commits += 1
                if commits == 2:
                    raise OperationalError("commit", {}, Exception("locked"))
                await original_commit()
            with patch.object(db, "commit", failing_second_commit), \
                 patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant_id, request_for(self.payload(), token), db)
            self.assertEqual(503, response.status_code)
            data = json.loads(response.body)["error"]["data"]
            self.assertEqual("MCP_RESULT_PERSIST_FAILED", data["reason"])
            self.assertEqual(1, len(FakeAsyncClient.calls))
            self.assertFalse(data["automatic_retry"])
        records, chain = await self.records()
        self.assertEqual(1, len(records))
        self.assertEqual(data["invocation_id"], records[0].id)
        self.assertEqual("outcome_unknown", records[0].state)
        self.assertIsNone(records[0].upstream_status)
        self.assertTrue(verify_audit_chain(chain))
        self.assertEqual(1, sum(x["stage"] == "tool_intent" for x in chain))
        self.assertEqual(0, sum(x["stage"] == "tool_call" for x in chain))
        async with self.sessions() as reader:
            self.assertEqual(0, (await reader.get(E2AGToolGrant, grant_id)).call_count)

    async def test_intent_visible_to_independent_reader_before_forwarding(self):
        sessions = self.sessions
        class InspectClient(FakeAsyncClient):
            async def post(client, *args, **kwargs):
                async with sessions() as reader:
                    rows = (await reader.execute(select(E2AGToolInvocation))).scalars().all()
                    self.assertEqual(1, len(rows))
                    self.assertEqual("outcome_unknown", rows[0].state)
                return await super().post(*args, **kwargs)
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", InspectClient):
                response = await proxy_mcp_call(grant.id, request_for(self.payload(), token), db)
            self.assertEqual(200, response.status_code)
        records, chain = await self.records()
        self.assertEqual("response_received", records[0].state)
        self.assertEqual(records[0].id, response.headers["x-e2ag-invocation-id"])
        self.assertEqual(records[0].id, chain[-1]["evidence"]["invocation_id"])
        self.assertEqual(200, records[0].upstream_status)
        self.assertEqual(64, len(records[0].response_sha256))
        self.assertTrue(verify_audit_chain(chain))
        self.assertNotIn("do-not-log", json.dumps(chain))
        self.assertNotIn(token, json.dumps(chain))

    async def test_transport_timeout_retains_unknown_without_resend(self):
        class TimeoutClient(FakeAsyncClient):
            async def post(client, *args, **kwargs):
                await super().post(*args, **kwargs)  # may have executed upstream
                raise httpx.ReadTimeout("lost reply")
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", TimeoutClient):
                response = await proxy_mcp_call(grant.id, request_for(self.payload(), token), db)
            self.assertEqual(502, response.status_code)
            self.assertEqual(1, len(TimeoutClient.calls))
        records, chain = await self.records()
        self.assertEqual("outcome_unknown", records[0].state)
        self.assertEqual(0, sum(x["stage"] == "tool_call" for x in chain))

    async def test_cancellation_retains_committed_intent(self):
        class CancelClient(FakeAsyncClient):
            async def post(client, *args, **kwargs):
                raise asyncio.CancelledError()
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", CancelClient):
                with self.assertRaises(asyncio.CancelledError):
                    await proxy_mcp_call(grant.id, request_for(self.payload(), token), db)
        records, chain = await self.records()
        self.assertEqual(1, len(records))
        self.assertEqual("outcome_unknown", records[0].state)
        self.assertTrue(verify_audit_chain(chain))

    async def test_denied_call_has_no_intent_and_no_upstream(self):
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(self.payload("admin.delete_all"), token), db)
            self.assertEqual(403, response.status_code)
        records, chain = await self.records()
        self.assertEqual([], records)
        self.assertEqual([], FakeAsyncClient.calls)
        self.assertEqual("deny", chain[-1]["outcome"])

    async def test_same_rpc_id_is_not_treated_as_idempotency_key(self):
        async with self.sessions() as db:
            *_, grant, token, _ = await self._fixtures(db)
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                for _ in range(2):
                    self.assertEqual(200, (await proxy_mcp_call(grant.id, request_for(self.payload(), token), db)).status_code)
        records, _ = await self.records()
        self.assertEqual(2, len({r.id for r in records}))
        self.assertEqual(2, len(FakeAsyncClient.calls))

    async def test_file_sqlite_concurrent_appends_keep_all_entries(self):
        # Independent sessions, deliberately preloaded stale ORM event objects.
        # A file database is required: :memory: uses a single pooled connection.
        with tempfile.TemporaryDirectory() as root:
            engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(root) / "audit.db"),
                                         connect_args={"timeout": 10}, pool_size=5, max_overflow=10)
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as db:
                    *_, grant, _, _ = await self._fixtures(db)
                    grant_id = grant.id
                async def append(index):
                    async with sessions() as db:
                        event = await db.get(EventLog, "event-tool")
                        grant = await db.get(E2AGToolGrant, grant_id)
                        await asyncio.sleep(.01)
                        await record_tool_decision(db, grant, tool_name=str(index), method="tools/call",
                                                   allowed=False, reason="test_denied")
                        self.assertIsNotNone(event)
                await asyncio.gather(*(append(i) for i in range(32)))
                async with sessions() as db:
                    event = await db.get(EventLog, "event-tool")
                    calls = [x for x in event.audit_chain if x["stage"] == "tool_call"]
                    self.assertEqual(32, len(calls))
                    self.assertEqual(32, len({x["evidence"]["tool"] for x in calls}))
                    self.assertTrue(verify_audit_chain(event.audit_chain))
                    self.assertEqual(32, (await db.get(E2AGToolGrant, grant_id)).call_count)
            finally:
                await engine.dispose()


if __name__ == "__main__":
    unittest.main()
