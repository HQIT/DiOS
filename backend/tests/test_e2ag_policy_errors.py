from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app.api.internal.e2ag_mcp import proxy_mcp_call
from app.models.tables import EventLog
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_policy_errors import PolicyEvaluationError
import test_e2ag_tool_gateway as fixture


class PolicyFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.E2AGToolGatewayTests.asyncSetUp
    asyncTearDown = fixture.E2AGToolGatewayTests.asyncTearDown
    _fixtures = fixture.E2AGToolGatewayTests._fixtures

    async def probe(self, point="validate_grant", *, bad_token=False, corrupt_event=False, audit_fails=False):
        async with self.sessions() as db:
            _, _, event, _, grant, token, _ = await self._fixtures(db)
            if corrupt_event:
                event.trace_id = "another-event-trace"
                await db.commit()
            before = len(event.audit_chain)
            payload = {"jsonrpc": "2.0", "id": 81, "method": "tools/call",
                       "params": {"name": "ledger.record_push", "arguments": {"private": "do-not-disclose"}}}
            request = fixture.request_for(payload, "invalid" if bad_token else token)
            with patch(f"app.api.internal.e2ag_mcp.{point}", side_effect=PolicyEvaluationError("private-engine-detail")), \
                 patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", fixture.FakeAsyncClient):
                if audit_fails:
                    with patch("app.services.e2ag_tool_gateway.record_tool_decision", side_effect=RuntimeError("audit unavailable")):
                        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                            await proxy_mcp_call(grant.id, request, db)
                    self.assertFalse(fixture.FakeAsyncClient.calls)
                    return
                response = await proxy_mcp_call(grant.id, request, db)
            self.assertEqual(503, response.status_code)
            self.assertEqual("POLICY_ENGINE_ERROR", json.loads(response.body)["error"]["data"]["reason"])
            self.assertFalse(fixture.FakeAsyncClient.calls)
            await db.refresh(event)
            await db.refresh(grant)
            added = event.audit_chain[before:]
            if bad_token or corrupt_event:
                self.assertEqual([], added)
                self.assertEqual(0, grant.call_count)
            else:
                self.assertEqual(1, len(added))
                self.assertEqual("tool_call", added[0]["stage"])
                self.assertEqual("deny", added[0]["outcome"])
                self.assertEqual("POLICY_ENGINE_ERROR", added[0]["evidence"]["reason"])
                self.assertEqual(1, grant.call_count)
                self.assertTrue(verify_audit_chain(event.audit_chain))
                async with self.sessions() as reader:
                    persisted = await reader.get(EventLog, event.id)
                    self.assertEqual(event.audit_chain, persisted.audit_chain)
            public = response.body.decode() + json.dumps(added)
            for secret in (token, "do-not-disclose", "private-engine-detail"):
                self.assertNotIn(secret, public)

    async def test_grant_engine_failure_is_audited_503(self):
        await self.probe()

    async def test_call_engine_failure_is_audited_503(self):
        await self.probe("authorize_mcp_request")

    async def test_invalid_token_cannot_pollute_audit(self):
        await self.probe(bad_token=True)

    async def test_corrupt_association_cannot_pollute_audit(self):
        await self.probe(corrupt_event=True)

    async def test_failed_audit_never_forwards(self):
        await self.probe(audit_fails=True)
