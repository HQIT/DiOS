from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text
from app.services.e2ag_tool_gateway import authorize_mcp_request, issue_task_mcp_config
from app.models.tables import E2AGToolGrant
from app.services.e2ag import verify_audit_chain
from app.api.internal.e2ag_mcp import proxy_mcp_call
import test_e2ag_tool_gateway as fixture


POLICY = {"version": 1, "tools": {"ledger.record_push": {
    "resource": {"type": "string", "enum": ["repo/qame/report/a"]},
    "rows": {"type": "integer", "minimum": 1, "maximum": 100},
}}}
ARGS = {"resource": "repo/qame/report/a", "rows": 10}


class ArgumentConstraintUnitTests(unittest.TestCase):
    def check(self, args, policy=POLICY, tool="ledger.record_push"):
        return authorize_mcp_request("tools/call", {"name": tool, "arguments": args}, ["ledger.*"], policy)

    def test_valid_boundaries(self):
        for rows in (1, 10, 100):
            with self.subTest(rows=rows):
                self.assertTrue(self.check({**ARGS, "rows": rows})[0])

    def test_invalid_types_and_ranges(self):
        for rows in (True, False, 10.0, "10", None, -1, 0, 101, [], {}):
            with self.subTest(rows=rows):
                self.assertFalse(self.check({**ARGS, "rows": rows})[0])

    def test_resources_missing_and_unknown(self):
        for args in (None, [], {}, {"rows": 10}, {"resource": ARGS["resource"]},
                     {**ARGS, "other": 1}, {**ARGS, "resource": "repo/other/report/b"},
                     {**ARGS, "resource": ARGS["resource"] + "/"}, {**ARGS, "resource": [ARGS["resource"]]}):
            with self.subTest(args=args):
                self.assertFalse(self.check(args)[0])

    def test_unconfigured_tool_fails_closed(self):
        self.assertFalse(self.check(ARGS, tool="ledger.other")[0])

    def test_malformed_policy_fails_closed(self):
        for policy in ({}, [], {"version": True, "tools": {}}, {"version": 2, "tools": {}},
                       {**POLICY, "ignored_key": True}, {"version": 1, "tools": {"ledger.*": {}}},
                       {"version": 1, "tools": {"ledger.record_push": {"rows": {"type": "number"}}}},
                       {"version": 1, "tools": {"ledger.record_push": {"rows": {"type": "integer", "minimum": 3, "maximum": 1}}}}):
            with self.subTest(policy=policy):
                self.assertEqual((False, "MCP_ARGUMENT_CONSTRAINT_INVALID"), self.check(ARGS, policy))

    def test_legacy_none_preserves_tool_only_contract(self):
        self.assertTrue(self.check(None, None)[0])


class ArgumentConstraintIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.E2AGToolGatewayTests.asyncSetUp
    asyncTearDown = fixture.E2AGToolGatewayTests.asyncTearDown
    _fixtures = fixture.E2AGToolGatewayTests._fixtures

    async def configured(self, db, policy=POLICY):
        agent, server, event, task, old, _, _ = await self._fixtures(db)
        agent.capabilities = {"governance": {"allowed_tools": ["ledger.*"], "argument_constraints": copy.deepcopy(policy)}}
        await db.commit()
        config, omitted = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
        self.assertFalse(omitted)
        conn = config[server.name]
        grant = await db.get(E2AGToolGrant, conn["url"].rsplit("/", 1)[-1])
        return agent, server, event, task, grant, conn["headers"]["Authorization"][7:]

    async def invoke(self, db, grant, token, args):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "ledger.record_push", "arguments": args}}
        with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", fixture.FakeAsyncClient):
            return await proxy_mcp_call(grant.id, fixture.request_for(payload, token), db)

    async def test_snapshot_survives_agent_policy_widening(self):
        async with self.sessions() as db:
            agent, _, event, _, grant, token = await self.configured(db)
            widened = copy.deepcopy(POLICY)
            widened["tools"]["ledger.record_push"]["rows"]["maximum"] = 1000
            agent.capabilities = {"governance": {"allowed_tools": ["*"], "argument_constraints": widened}}
            await db.commit()
            async with self.sessions() as reader:
                saved = await reader.get(E2AGToolGrant, grant.id)
                self.assertEqual(POLICY, saved.argument_constraints)
            response = await self.invoke(db, grant, token, {**ARGS, "rows": 101})
            self.assertEqual(403, response.status_code)
            self.assertEqual([], fixture.FakeAsyncClient.calls)
            await db.refresh(event)
            self.assertTrue(verify_audit_chain(event.audit_chain))
            last = event.audit_chain[-1]
            self.assertEqual("deny", last["outcome"])
            self.assertEqual(64, len(last["evidence"]["argument_constraints_sha256"]))
            self.assertNotIn(ARGS["resource"], json.dumps(last))

    async def test_exact_arguments_forwarded_once(self):
        async with self.sessions() as db:
            _, _, event, _, grant, token = await self.configured(db)
            response = await self.invoke(db, grant, token, ARGS)
            self.assertEqual(200, response.status_code)
            self.assertEqual(1, len(fixture.FakeAsyncClient.calls))
            self.assertEqual(ARGS, fixture.FakeAsyncClient.calls[0]["json"]["params"]["arguments"])
            await db.refresh(event)
            self.assertTrue(verify_audit_chain(event.audit_chain))

    async def test_resource_denial_audited(self):
        async with self.sessions() as db:
            _, _, event, _, grant, token = await self.configured(db)
            response = await self.invoke(db, grant, token, {**ARGS, "resource": "repo/other"})
            self.assertEqual(403, response.status_code)
            self.assertFalse(fixture.FakeAsyncClient.calls)
            await db.refresh(event)
            self.assertEqual("MCP_ARGUMENT_ENUM_DENIED", event.audit_chain[-1]["evidence"]["reason"])

    async def test_invalid_config_withholds_grant(self):
        async with self.sessions() as db:
            agent, server, event, task, _, _, _ = await self._fixtures(db)
            for policy in (None, {}, [], {"version": 99, "tools": {}}):
                agent.capabilities = {"governance": {"allowed_tools": ["ledger.*"], "argument_constraints": policy}}
                config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
                self.assertFalse(config)
                self.assertEqual("MCP_ARGUMENT_CONSTRAINT_INVALID", omissions[0]["reason"])

    async def test_expired_grant_still_denies_valid_arguments(self):
        from datetime import datetime, timedelta, timezone
        async with self.sessions() as db:
            _, _, _, _, grant, token = await self.configured(db)
            grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
            response = await self.invoke(db, grant, token, ARGS)
            self.assertEqual("TOOL_GRANT_EXPIRED", json.loads(response.body)["error"]["data"]["reason"])
            self.assertFalse(fixture.FakeAsyncClient.calls)


class ArgumentConstraintMigrationTests(unittest.TestCase):
    def test_sqlite_existing_table_migration_is_idempotent(self):
        from app.db.database import _add_e2ag_argument_constraints
        engine = create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE e2ag_tool_grants (id TEXT PRIMARY KEY)"))
                conn.execute(text("INSERT INTO e2ag_tool_grants VALUES ('legacy')"))
                _add_e2ag_argument_constraints(conn)
                _add_e2ag_argument_constraints(conn)
                self.assertIn("argument_constraints", {c["name"] for c in inspect(conn).get_columns("e2ag_tool_grants")})
                self.assertIsNone(conn.execute(text("SELECT argument_constraints FROM e2ag_tool_grants")).scalar())
        finally:
            engine.dispose()
