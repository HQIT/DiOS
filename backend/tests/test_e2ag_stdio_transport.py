"""Real subprocess and pipe checks at the native PEP; not full Agent execution."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.api.internal.e2ag_mcp import proxy_mcp_call
from app.config import settings
from app.models.tables import E2AGToolInvocation, McpServer
from app.services.e2ag_stdio_transport import StdioPool, StdioExchangeError
from app.services.e2ag_tool_gateway import issue_task_mcp_config, revoke_task_grants
import test_e2ag_tool_gateway as gateway_tests
from test_e2ag_tool_gateway import request_for

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "experiments/e2ag/heterogeneity/stdio_receipt_tool.py"


class StdioPEPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await gateway_tests.E2AGToolGatewayTests.asyncSetUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.old_stdio = settings.e2ag_stdio_server_ids
        settings.e2ag_stdio_server_ids = ["mcp-remote"]
        self.pool = StdioPool(timeout=2)
        self.app = SimpleNamespace(state=SimpleNamespace(e2ag_stdio_pool=self.pool))

    async def asyncTearDown(self):
        await self.pool.close()
        settings.e2ag_stdio_server_ids = self.old_stdio
        self.temp.cleanup()
        await gateway_tests.E2AGToolGatewayTests.asyncTearDown(self)

    def records(self, name):
        path = self.folder / name
        return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    async def fixtures(self, db, fault="none"):
        agent, server, event, task, grant, token, omissions = await gateway_tests.E2AGToolGatewayTests._fixtures(self, db)
        server.transport, server.url, server.command = "stdio", "", sys.executable
        server.args = [str(TOOL), "--directory", str(self.folder), "--fault", fault]
        # Configure before issuing the stdio-specific grant.
        await revoke_task_grants(db, task.id)
        config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
        self.assertFalse(omissions)
        connection = config[server.name]
        self.assertEqual("streamable_http", connection["transport"])
        self.assertNotIn("command", connection)
        self.assertNotIn("args", connection)
        return connection["url"].rsplit("/", 1)[-1], connection["headers"]["Authorization"][7:], task, server

    async def call(self, db, grant, token, payload):
        request = request_for(payload, token)
        request.scope["app"] = self.app
        return await proxy_mcp_call(grant, request, db)

    async def initialize(self, db, grant, token):
        result = await self.call(db, grant, token, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "qualification", "version": "1"}}})
        self.assertEqual(200, result.status_code, result.body)
        result = await self.call(db, grant, token, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(202, result.status_code)

    def payload(self, name="ledger.record_push"):
        return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {"text": "真实 stdio"}}}

    async def test_same_child_initialize_notify_and_call(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            await self.initialize(db, grant, token)
            result = await self.call(db, grant, token, self.payload())
            self.assertEqual(200, result.status_code)
        messages = self.records("stdio-messages.jsonl")
        self.assertEqual(["initialize", "notifications/initialized", "tools/call"], [x["payload"]["method"] for x in messages])
        self.assertEqual(1, len({x["pid"] for x in messages}))
        self.assertEqual(1, len(self.records("artifacts.jsonl")))

    async def test_denied_tool_and_invalid_token_do_not_spawn(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            self.assertEqual(403, (await self.call(db, grant, token, self.payload("other.write"))).status_code)
            self.assertEqual(403, (await self.call(db, grant, "invalid", self.payload())).status_code)
        self.assertFalse(self.records("stdio-processes.jsonl"))

    async def test_tools_call_cannot_implicitly_start_session(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            response = await self.call(db, grant, token, self.payload())
            self.assertEqual(502, response.status_code)
            self.assertFalse(self.records("stdio-processes.jsonl"))

    async def test_revoked_grant_cannot_reconnect(self):
        async with self.sessions() as db:
            grant, token, task, _ = await self.fixtures(db)
            await self.initialize(db, grant, token)
            await self.pool.close_session(grant)
            await revoke_task_grants(db, task.id)
            response = await self.call(db, grant, token, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}})
            self.assertEqual(403, response.status_code)
        self.assertEqual(1, sum(r["phase"] == "started" for r in self.records("stdio-processes.jsonl")))
        self.assertFalse(self.records("tool-receipts.jsonl"))

    async def test_unlisted_stdio_still_withheld(self):
        async with self.sessions() as db:
            agent, _, _, task, *_ = await gateway_tests.E2AGToolGatewayTests._fixtures(self, db)
            server = McpServer(id="not-listed", name="other", transport="stdio", command=sys.executable, args=[])
            db.add(server)
            await db.commit()
            config, omissions = await issue_task_mcp_config(db, agent=agent, task=task, servers=[server])
            self.assertFalse(config)
            self.assertEqual("MCP_TRANSPORT_NOT_MEDIATED", omissions[0]["reason"])

    async def unknown_case(self, fault):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db, fault)
            await self.initialize(db, grant, token)
            response = await self.call(db, grant, token, self.payload())
            self.assertEqual(502, response.status_code, response.body)
            data = json.loads(response.body)["error"]["data"]
            self.assertEqual("unknown", data["execution_outcome"])
            self.assertFalse(data["automatic_retry"])
            calls = (await db.execute(select(E2AGToolInvocation).where(E2AGToolInvocation.method == "tools/call"))).scalars().all()
            self.assertEqual(1, len(calls))
            self.assertEqual("outcome_unknown", calls[0].state)
        self.assertEqual(1, len(self.records("artifacts.jsonl")))
        self.assertEqual(1, sum(r["phase"] == "started" for r in self.records("stdio-processes.jsonl")))

    async def test_exit_after_write_is_unknown_without_retry(self):
        await self.unknown_case("exit_after_write")

    async def test_timeout_after_write_is_unknown_without_retry(self):
        await self.unknown_case("timeout_after_write")

    async def test_malformed_response_is_unknown_without_retry(self):
        await self.unknown_case("malformed_response")

    async def test_wrong_response_id_is_unknown_without_retry(self):
        await self.unknown_case("wrong_id")

    async def test_intent_failure_prevents_pipe_write(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            await self.initialize(db, grant, token)
            with patch.object(db, "commit", side_effect=OperationalError("commit", {}, Exception("injected"))):
                response = await self.call(db, grant, token, self.payload())
            self.assertEqual(503, response.status_code)
            self.assertEqual("MCP_INTENT_PERSIST_FAILED", json.loads(response.body)["error"]["data"]["reason"])
        self.assertFalse(self.records("tool-receipts.jsonl"))

    async def test_result_failure_retains_unknown_after_single_write(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            await self.initialize(db, grant, token)
            original, commits = db.commit, 0
            async def fail_result():
                nonlocal commits
                commits += 1
                if commits == 2:
                    raise OperationalError("commit", {}, Exception("injected"))
                await original()
            with patch.object(db, "commit", fail_result):
                response = await self.call(db, grant, token, self.payload())
            self.assertEqual(503, response.status_code)
            self.assertEqual("MCP_RESULT_PERSIST_FAILED", json.loads(response.body)["error"]["data"]["reason"])
            calls = (await db.execute(select(E2AGToolInvocation).where(E2AGToolInvocation.method == "tools/call"))).scalars().all()
            self.assertEqual(["outcome_unknown"], [c.state for c in calls])
        self.assertEqual(1, len(self.records("artifacts.jsonl")))

    async def test_shutdown_reaps_child(self):
        async with self.sessions() as db:
            grant, token, *_ = await self.fixtures(db)
            await self.initialize(db, grant, token)
            process = self.pool.sessions[grant].process
            await self.pool.close()
            self.assertIsNotNone(process.returncode)
            self.assertFalse(self.pool.sessions)

    async def test_relative_executable_rejected(self):
        server = McpServer(id="relative", name="relative", transport="stdio", command="python", args=[])
        with self.assertRaises(StdioExchangeError):
            await self.pool.exchange("relative-grant", server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertFalse(self.records("stdio-processes.jsonl"))
