from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.api.internal.e2ag_mcp import proxy_mcp_call
from app.services.a2a_service import _remove_task_secret_config
from app.config import settings
from app.db.database import Base
from app.models.tables import A2ATask, Agent, E2AGToolGrant, EventLog, McpServer
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_tool_gateway import (
    filter_tools_list_response,
    issue_task_mcp_config,
    revoke_task_grants,
    validate_grant,
)


def request_for(payload: dict, token: str) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/internal/e2ag/mcp/test",
            "raw_path": b"/api/internal/e2ag/mcp/test",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {token}".encode()),
                (b"content-type", b"application/json"),
            ],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
    )


class FakeAsyncClient:
    calls: list[dict] = []
    last_kwargs: dict = {}
    response_json: dict = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.__class__.last_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.__class__.calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            200,
            json={**self.__class__.response_json, "id": json.get("id")},
            headers={"content-type": "application/json", "mcp-session-id": "session-test"},
        )


class E2AGToolGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.old_mode = settings.e2ag_mode
        self.old_base = settings.e2ag_internal_base_url
        settings.e2ag_mode = "enforce"
        settings.e2ag_internal_base_url = "http://host.docker.internal:8000"
        FakeAsyncClient.calls = []
        FakeAsyncClient.last_kwargs = {}
        FakeAsyncClient.response_json = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    async def asyncTearDown(self):
        settings.e2ag_mode = self.old_mode
        settings.e2ag_internal_base_url = self.old_base
        await self.engine.dispose()

    async def _fixtures(self, db):
        agent = Agent(
            id="agent-tool",
            name="Tool Agent",
            mode="task",
            capabilities={"governance": {"allowed_tools": ["ledger.record_*"]}},
        )
        server = McpServer(
            id="mcp-remote",
            name="ledger",
            transport="streamable_http",
            url="https://upstream.example/mcp",
            headers={"X-Upstream-Key": "secret"},
        )
        event_log = EventLog(
            id="event-tool",
            source="github/acme/repo",
            event_type="git.push",
            subject="refs/heads/main",
            cloud_event={"specversion": "1.0", "id": "evt", "source": "github/acme/repo", "type": "git.push", "data": {}},
            matched_agent_ids=[agent.id],
            status="dispatched",
            trace_id="trace-tool",
            audit_chain=[],
        )
        task = A2ATask(
            id="task-tool",
            agent_id=agent.id,
            context_id=event_log.id,
            trace_id=event_log.trace_id,
            status="working",
            message={"role": "user", "parts": []},
        )
        db.add_all([agent, server, event_log, task])
        await db.commit()
        config, omissions = await issue_task_mcp_config(
            db, agent=agent, task=task, servers=[server],
        )
        auth = config[server.name]["headers"]["Authorization"]
        token = auth.removeprefix("Bearer ")
        grant_id = config[server.name]["url"].rsplit("/", 1)[-1]
        grant = await db.get(E2AGToolGrant, grant_id)
        return agent, server, event_log, task, grant, token, omissions

    async def test_grant_is_hashed_scoped_and_audited(self):
        async with self.sessions() as db:
            _, server, event_log, task, grant, token, omissions = await self._fixtures(db)
            self.assertEqual([], omissions)
            self.assertNotEqual(token, grant.token_hash)
            self.assertEqual(task.trace_id, grant.trace_id)
            self.assertEqual(server.id, grant.mcp_server_id)
            self.assertEqual(["ledger.record_*"], grant.allowed_tools)
            self.assertTrue(verify_audit_chain(event_log.audit_chain))
            invalid, reason = await validate_grant(db, grant.id, "wrong-token")
            self.assertIsNone(invalid)
            self.assertEqual("TOOL_GRANT_INVALID", reason)

    async def test_denied_tool_never_reaches_upstream(self):
        async with self.sessions() as db:
            _, _, _, _, grant, token, _ = await self._fixtures(db)
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "admin.delete_all", "arguments": {}},
            }
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
            parsed = json.loads(response.body)
            self.assertEqual(403, response.status_code)
            self.assertEqual("MCP_TOOL_NOT_GRANTED", parsed["error"]["data"]["reason"])
            self.assertEqual([], FakeAsyncClient.calls)
            await db.refresh(grant)
            self.assertEqual(1, grant.call_count)

    async def test_allowed_tool_is_forwarded_with_upstream_credentials(self):
        async with self.sessions() as db:
            _, _, event_log, _, grant, token, _ = await self._fixtures(db)
            payload = {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "ledger.record_push", "arguments": {"sha": "abc"}},
            }
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
            self.assertEqual(200, response.status_code)
            self.assertTrue(FakeAsyncClient.last_kwargs["follow_redirects"])
            self.assertEqual("session-test", response.headers["mcp-session-id"])
            self.assertEqual(1, len(FakeAsyncClient.calls))
            call = FakeAsyncClient.calls[0]
            self.assertEqual("https://upstream.example/mcp", call["url"])
            self.assertEqual("secret", call["headers"]["X-Upstream-Key"])
            await db.refresh(grant)
            await db.refresh(event_log)
            self.assertEqual(1, grant.call_count)
            self.assertEqual("ledger.record_push", event_log.audit_chain[-1]["evidence"]["tool"])
            self.assertTrue(verify_audit_chain(event_log.audit_chain))

    async def test_tools_list_exposes_only_granted_tools(self):
        async with self.sessions() as db:
            _, _, _, _, grant, token, _ = await self._fixtures(db)
            FakeAsyncClient.response_json = {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {
                    "tools": [
                        {"name": "ledger.record_push"},
                        {"name": "admin.delete_all"},
                    ],
                },
            }
            payload = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
            parsed = json.loads(response.body)
            self.assertEqual(
                ["ledger.record_push"],
                [tool["name"] for tool in parsed["result"]["tools"]],
            )

    async def test_tools_list_preserves_sse_framing(self):
        upstream = (
            b'event: message\n'
            b'data: {"jsonrpc":"2.0","id":3,"result":{"tools":['
            b'{"name":"ledger.record_push"},{"name":"admin.delete_all"}]}}\n\n'
        )
        filtered = filter_tools_list_response(upstream, ["ledger.*"])
        self.assertTrue(filtered.startswith(b"event: message\ndata: "))
        data = filtered.split(b"data: ", 1)[1].strip()
        parsed = json.loads(data)
        self.assertEqual(
            ["ledger.record_push"],
            [tool["name"] for tool in parsed["result"]["tools"]],
        )

    async def test_malformed_tools_list_fails_closed(self):
        filtered = filter_tools_list_response(b"not-json", ["ledger.*"])
        self.assertEqual([], json.loads(filtered)["result"]["tools"])

    async def test_expired_and_terminal_task_grants_are_rejected(self):
        async with self.sessions() as db:
            _, _, _, task, grant, token, _ = await self._fixtures(db)
            grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
            invalid, reason = await validate_grant(db, grant.id, token)
            self.assertIsNone(invalid)
            self.assertEqual("TOOL_GRANT_EXPIRED", reason)

            grant.status = "active"
            grant.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            task.status = "completed"
            await db.commit()
            invalid, reason = await validate_grant(db, grant.id, token)
            self.assertIsNone(invalid)
            self.assertEqual("TOOL_GRANT_TASK_TERMINAL", reason)

    async def test_task_grants_are_revoked_explicitly(self):
        async with self.sessions() as db:
            _, _, event_log, task, grant, token, _ = await self._fixtures(db)
            await revoke_task_grants(db, task.id)
            invalid, reason = await validate_grant(db, grant.id, token)
            self.assertIsNone(invalid)
            self.assertEqual("TOOL_GRANT_NOT_ACTIVE", reason)
            await db.refresh(event_log)
            self.assertEqual("grant_revoked", event_log.audit_chain[-1]["outcome"])
            self.assertTrue(verify_audit_chain(event_log.audit_chain))

    async def _assert_rejected_grant_audit(self, scenario, reason):
        async with self.sessions() as db:
            _, _, event, task, grant, token, _ = await self._fixtures(db)
            if scenario == "expired":
                grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif scenario == "revoked":
                await revoke_task_grants(db, task.id)
            elif scenario == "mismatch":
                task.trace_id = "other-task-trace"
            elif scenario == "missing_task":
                await db.delete(task)
            elif scenario == "terminal":
                task.status = "completed"
            await db.commit()
            await db.refresh(event)
            before = len(event.audit_chain)
            payload = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "ledger.record_push", "arguments": {"private": "do-not-log"}}}
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
            self.assertEqual(403, response.status_code)
            self.assertEqual(reason, json.loads(response.body)["error"]["data"]["reason"])
            self.assertEqual([], FakeAsyncClient.calls)
            await db.refresh(event)
            await db.refresh(grant)
            added = event.audit_chain[before:]
            calls = [entry for entry in added if entry["stage"] == "tool_call"]
            self.assertEqual(1, len(calls))
            self.assertEqual("deny", calls[0]["outcome"])
            self.assertEqual(reason, calls[0]["evidence"]["reason"])
            self.assertEqual(grant.task_id, calls[0]["evidence"]["task_id"])
            self.assertEqual("ledger.record_push", calls[0]["evidence"]["tool"])
            self.assertEqual(1, grant.call_count)  # authenticated attempt, not execution
            self.assertTrue(verify_audit_chain(event.audit_chain))
            serialized = json.dumps(added)
            self.assertNotIn(token, serialized)
            self.assertNotIn("do-not-log", serialized)
            # Evidence must survive a separate session, not merely the identity map.
            async with self.sessions() as reader:
                stored = await reader.get(EventLog, event.id)
                self.assertEqual(event.audit_chain, stored.audit_chain)

    async def test_expired_call_has_rejection_audit(self):
        await self._assert_rejected_grant_audit("expired", "TOOL_GRANT_EXPIRED")

    async def test_revoked_call_has_rejection_audit(self):
        await self._assert_rejected_grant_audit("revoked", "TOOL_GRANT_NOT_ACTIVE")

    async def test_mismatched_call_has_rejection_audit(self):
        await self._assert_rejected_grant_audit("mismatch", "TOOL_GRANT_TASK_MISMATCH")

    async def test_missing_task_call_has_rejection_audit(self):
        await self._assert_rejected_grant_audit("missing_task", "TOOL_GRANT_TASK_MISMATCH")

    async def test_terminal_task_call_has_rejection_audit(self):
        await self._assert_rejected_grant_audit("terminal", "TOOL_GRANT_TASK_TERMINAL")

    async def test_unverified_requests_do_not_pollute_grant_audit(self):
        async with self.sessions() as db:
            _, _, event, task, grant, token, _ = await self._fixtures(db)
            # A real token for a different grant must not authorize attribution.
            agent = await db.get(Agent, task.agent_id)
            server = await db.get(McpServer, grant.mcp_server_id)
            config, _ = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
            other_token = config[server.name]["headers"]["Authorization"].removeprefix("Bearer ")
            await revoke_task_grants(db, task.id)
            await db.refresh(event)
            before = list(event.audit_chain)
            payload = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "ledger.record_push", "arguments": {}}}
            for identity, presented in [(grant.id, "wrong-token"), (grant.id, ""), (grant.id, other_token), ("unknown-grant", token)]:
                with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                    response = await proxy_mcp_call(identity, request_for(payload, presented), db)
                self.assertEqual(403, response.status_code)
                self.assertEqual("TOOL_GRANT_INVALID", json.loads(response.body)["error"]["data"]["reason"])
            await db.refresh(event)
            await db.refresh(grant)
            self.assertEqual(before, event.audit_chain)
            self.assertEqual(0, grant.call_count)
            self.assertEqual([], FakeAsyncClient.calls)

    async def test_rejected_call_does_not_write_to_mismatched_event_trace(self):
        async with self.sessions() as db:
            _, _, event, task, grant, token, _ = await self._fixtures(db)
            await revoke_task_grants(db, task.id)
            event.trace_id = "other-event-trace"
            await db.commit()
            before = list(event.audit_chain)
            payload = {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "ledger.record_push"}}
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
            self.assertEqual(403, response.status_code)
            await db.refresh(event)
            await db.refresh(grant)
            self.assertEqual(before, event.audit_chain)
            self.assertEqual(0, grant.call_count)
            self.assertEqual([], FakeAsyncClient.calls)

    async def test_rejection_audit_failure_never_forwards(self):
        async with self.sessions() as db:
            _, _, _, task, grant, token, _ = await self._fixtures(db)
            await revoke_task_grants(db, task.id)
            payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ledger.record_push"}}
            with patch("app.services.e2ag_tool_gateway.record_tool_decision", AsyncMock(side_effect=RuntimeError("audit unavailable"))), \
                 patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                    await proxy_mcp_call(grant.id, request_for(payload, token), db)
            self.assertEqual([], FakeAsyncClient.calls)

    async def test_repeated_rejected_attempts_have_separate_audit_entries(self):
        async with self.sessions() as db:
            _, _, event, task, grant, token, _ = await self._fixtures(db)
            await revoke_task_grants(db, task.id)
            await db.refresh(event)
            before = len(event.audit_chain)
            payload = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "ledger.record_push"}}
            with patch("app.api.internal.e2ag_mcp.httpx.AsyncClient", FakeAsyncClient):
                for _ in range(2):
                    response = await proxy_mcp_call(grant.id, request_for(payload, token), db)
                    self.assertEqual(403, response.status_code)
            await db.refresh(event)
            await db.refresh(grant)
            added = event.audit_chain[before:]
            self.assertEqual(2, len(added))
            self.assertTrue(all(e["stage"] == "tool_call" and e["outcome"] == "deny" for e in added))
            self.assertTrue(verify_audit_chain(event.audit_chain))
            self.assertEqual(2, grant.call_count)
            self.assertEqual([], FakeAsyncClient.calls)

    async def test_task_secret_config_is_removed_by_exact_run_id(self):
        with tempfile.TemporaryDirectory() as root:
            config_dir = Path(root) / "config"
            config_dir.mkdir()
            target = config_dir / "mcp_servers_run-safe.json"
            neighbor = config_dir / "mcp_servers_other.json"
            target.write_text("secret", encoding="utf-8")
            neighbor.write_text("keep", encoding="utf-8")
            _remove_task_secret_config(root, "run-safe")
            self.assertFalse(target.exists())
            self.assertTrue(neighbor.exists())

    async def test_stdio_server_is_withheld_in_enforce_mode(self):
        async with self.sessions() as db:
            agent, _, _, task, _, _, _ = await self._fixtures(db)
            stdio = McpServer(
                id="mcp-stdio", name="local-shell", transport="stdio",
                command="python", args=["server.py"],
            )
            db.add(stdio)
            await db.commit()
            config, omissions = await issue_task_mcp_config(
                db, agent=agent, task=task, servers=[stdio],
            )
            self.assertEqual({}, config)
            self.assertEqual("MCP_TRANSPORT_NOT_MEDIATED", omissions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
