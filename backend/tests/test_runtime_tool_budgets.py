"""Atomic dispatch accounting, E2AG ordering and durable retry semantics."""

import asyncio
import os
import unittest
from unittest.mock import patch

import test_runtime_tool_invocations as tool_fixtures
import test_runtime_worker as fixtures
from app.runtime.capacity import Capacity
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.tool_budget_schema import bindings
from app.runtime.tool_budgets import bind, view
from app.runtime.tool_invocations import ToolInvocations


class ToolBudgetScenarios:
    postgres = False
    request = tool_fixtures.ToolInvocationTests.request
    approve = tool_fixtures.ToolInvocationTests.approve
    asyncTearDown = tool_fixtures.ToolInvocationTests.asyncTearDown

    async def asyncSetUp(self):
        original = RuntimeStore.claim

        async def first_claim(store, *args, **kwargs):
            await Capacity(store).put(
                "tools", {"kind": "scope"}, 20, 0, budget={"max_tool_calls": 2}
            )
            return await original(store, *args, **kwargs)

        url = os.environ.get("DIOS_TEST_POSTGRES_URL") if self.postgres else None
        with patch.object(RuntimeStore, "claim", first_claim):
            if url:
                with patch.object(fixtures, "Database", lambda _: Database(url)):
                    await tool_fixtures.ToolInvocationTests.asyncSetUp(self)
                self.url = url
            else:
                await tool_fixtures.ToolInvocationTests.asyncSetUp(self)

    async def usage(self):
        async with self.db.read(self.scope) as c:
            return await view(c, self.scope, self.run["id"])

    async def test_concurrent_connections_cannot_overspend_and_replay_is_free(self):
        items = [
            await self.approve(await self.request("call-" + str(n))) for n in range(6)
        ]
        dbs = [Database(self.url) for _ in items]
        gates = [
            ToolInvocations(
                RuntimeStore(db, self.scope),
                self.policy,
                self.gate.catalog,
                self.provider,
                self.transport,
                self.gate.approvers,
            )
            for db in dbs
        ]
        try:
            result = await asyncio.gather(
                *(
                    gate.dispatch(self.run["id"], item["id"], item["version"])
                    for gate, item in zip(gates, items)
                ),
                return_exceptions=True,
            )
            self.assertEqual(
                sum(isinstance(x, dict) and x["state"] == "succeeded" for x in result),
                2,
            )
            self.assertEqual(
                sum(
                    isinstance(x, RuntimeErrorCode)
                    and x.code == "TOOL_BUDGET_EXHAUSTED"
                    for x in result
                ),
                4,
            )
            self.assertEqual(len(self.transport.calls), 2)
            successful = next(x for x in result if isinstance(x, dict))
            await self.gate.dispatch(self.run["id"], successful["id"], 1)
            self.assertEqual(len(self.transport.calls), 2)
            value = await self.usage()
            self.assertEqual(
                (value["committed"], value["succeeded"], value["remaining"]), (2, 2, 0)
            )
        finally:
            for db in dbs:
                await db.close()

    async def test_policy_changes_restart_and_rebinding_keep_original_limit(self):
        item = await self.approve(await self.request())
        await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        await Capacity(self.store).put(
            "tools", {"kind": "scope"}, 20, 1, enabled=False, budget={}
        )
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db, self.scope)
        async with self.db.transaction(self.scope) as c:
            bound = await bind(c, self.scope, await self.store._run(c, self.run["id"]))
        self.assertEqual(bound["max_tool_calls"], 2)
        self.assertEqual((await self.usage())["committed"], 1)

    async def test_unknown_result_consumes_budget_and_recovery_does_not_recharge(self):
        self.transport.failure = True
        for key in ["first", "second"]:
            item = await self.approve(await self.request(key))
            result = await self.gate.dispatch(
                self.run["id"], item["id"], item["version"]
            )
            self.assertEqual(result["state"], "result_unknown")
            await self.gate.recover_receipt(self.run["id"], item["id"])
        third = await self.approve(await self.request("third"))
        with self.assertRaisesRegex(RuntimeErrorCode, "TOOL_BUDGET_EXHAUSTED"):
            await self.gate.dispatch(self.run["id"], third["id"], third["version"])
        self.assertEqual((await self.usage())["unresolved"], 2)
        self.assertEqual(len(self.transport.calls), 2)

    async def test_approval_and_revocation_denials_do_not_consume(self):
        item = await self.request()
        with self.assertRaisesRegex(RuntimeErrorCode, "TOOL_APPROVAL_REQUIRED"):
            await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        item = await self.approve(item)

        async def revoke():
            run = await self.store.get(self.run["id"])
            await self.store.revoke(run["id"], run["version"])

        self.transport.hook = revoke
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        self.assertEqual((await self.usage())["committed"], 0)
        self.assertEqual(self.transport.calls, [])

    async def test_dispatch_audit_failure_rolls_back_budget_and_approval(self):
        item = await self.approve(await self.request())
        with (
            patch(
                "app.runtime.tool_invocations.audit",
                side_effect=OSError("audit offline"),
            ),
            self.assertRaises(OSError),
        ):
            await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        self.assertEqual((await self.usage())["committed"], 0)
        current = await self.gate.get(self.run["id"], item["id"])
        self.assertEqual(current["approval_state"], "approved")
        self.assertEqual(self.transport.calls, [])

    async def test_zero_limit_blocks_legacy_run_at_first_dispatch(self):
        # Simulate an upgraded active Run which predates immutable budget bindings.
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                bindings.delete().where(bindings.c.run_id == self.run["id"])
            )
        await Capacity(self.store).put(
            "tools", {"kind": "scope"}, 20, 1, budget={"max_tool_calls": 0}
        )
        item = await self.approve(await self.request())
        with self.assertRaisesRegex(RuntimeErrorCode, "TOOL_BUDGET_EXHAUSTED"):
            await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        self.assertEqual((await self.usage())["maximum"], 0)
        self.assertEqual(self.transport.calls, [])

    async def test_new_attempt_keeps_consumed_calls(self):
        item = await self.approve(await self.request())
        await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        await self.store.mark_unknown(self.binding, "INTERRUPTED")
        run = await self.store.get(self.run["id"])
        await self.store.reconcile_absent(
            run["id"], run["version"], "confirmed-process-exit"
        )
        run = await self.store.get(self.run["id"])
        await self.store.retry(run["id"], run["version"])
        attempt = await self.store.claim(self.binding[2], self.binding[3])
        self.assertNotEqual(attempt["id"], self.attempt["id"])
        value = await self.usage()
        self.assertEqual((value["committed"], value["maximum"]), (1, 2))

    async def test_policy_validation_and_omission_preserve_budget(self):
        for bad in [-1, True, 1.5, 100001]:
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_EXECUTION_BUDGET"):
                await Capacity(self.store).put(
                    "tools", {"kind": "scope"}, 20, 1, budget={"max_tool_calls": bad}
                )
        saved = await Capacity(self.store).put("tools", {"kind": "scope"}, 20, 1)
        self.assertEqual(saved["budget"]["max_tool_calls"], 2)


class SQLiteToolBudgetTests(ToolBudgetScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_worker_socket_and_acp_proxy_share_dispatch_budget(self):
        from app.runtime.tool_channel_client import ToolChannelClient
        from app.runtime.worker_tool_channel import WorkerToolChannel

        channel = WorkerToolChannel(
            self.gate,
            self.binding,
            {
                "tools": {"search": "search"},
                "acp_names": {"mcp__dios__search": "search"},
            },
            os.getuid(),
        )
        try:
            await channel.open()
            await channel.bind_session("session-one")
            client = ToolChannelClient(str(channel.directory / "channel.sock"))
            for n in range(3):
                proxy = await client.request(
                    "permit_proxy",
                    {"name": "mcp__dios__search", "arguments": {"query": "evidence"}},
                )
                self.assertTrue(proxy["forward_to_gate"])
                item = await client.request(
                    "request",
                    {
                        "tool": "search",
                        "arguments": {"query": "evidence"},
                        "key": "socket-" + str(n),
                    },
                )
                await self.approve(item)
                if n < 2:
                    result = await client.request(
                        "advance", {"invocation_id": item["id"]}
                    )
                    self.assertEqual(result["state"], "succeeded")
                else:
                    with self.assertRaisesRegex(
                        RuntimeErrorCode, "TOOL_BUDGET_EXHAUSTED"
                    ):
                        await client.request("advance", {"invocation_id": item["id"]})
            self.assertEqual(len(self.transport.calls), 2)
        finally:
            await channel.close()

    async def test_backup_contains_budget_and_usage(self):
        from app.runtime.recovery import backup_sqlite, verify_backup

        item = await self.approve(await self.request())
        await self.gate.dispatch(self.run["id"], item["id"], item["version"])
        target = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, self.provider.root, target)
        manifest = await asyncio.to_thread(verify_backup, target)
        self.assertIn("runtime_tool_budget_schema", manifest["schemas"])


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL fixture disabled"
)
class PostgresToolBudgetTests(ToolBudgetScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class HttpToolBudgetTests(unittest.IsolatedAsyncioTestCase):
    request = ToolBudgetScenarios.request
    approve = ToolBudgetScenarios.approve
    usage = ToolBudgetScenarios.usage
    postgres = False
    test_real_http_dispatch_is_bounded = ToolBudgetScenarios.test_concurrent_connections_cannot_overspend_and_replay_is_free

    async def asyncSetUp(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from app.runtime.mcp_tool_transport import McpHttpTransport

        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                message = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                if "id" not in message:
                    self.send_response(202)
                    self.end_headers()
                    return
                if message["method"] == "initialize":
                    result = {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "budget-test", "version": "1"},
                    }
                else:
                    calls.append(message["id"])
                    result = {
                        "content": [{"type": "text", "text": "可核验的工具结果"}],
                        "isError": False,
                    }
                raw = json.dumps(
                    {"jsonrpc": "2.0", "id": message["id"], "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tool_url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        await ToolBudgetScenarios.asyncSetUp(self)
        self.transport = McpHttpTransport()
        self.transport.calls = calls
        self.gate.transport = self.transport

    async def asyncTearDown(self):
        await tool_fixtures.ToolInvocationTests.asyncTearDown(self)
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await asyncio.to_thread(self.thread.join)
