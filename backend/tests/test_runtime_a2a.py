"""Official A2A client/HTTP contracts over durable Runtime ownership and E2AG."""

import asyncio
import json
import unittest
from unittest.mock import patch

import test_runtime_principals as principals
from a2a.client import ClientConfig, ClientFactory
from a2a.types import AgentCard, Message, TaskIdParams, TaskQueryParams
from app.api.os import runtime_a2a as api
from app.runtime.database import Database
from app.runtime.schema import runs
from app.runtime.work_auth_schema import work
from app.runtime.worker import Worker
from app.services.task_console import TaskConsole
from sqlalchemy import func, select
from test_runtime_worker import FakeAdapter


class RuntimeA2ATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await principals.PrincipalTests.asyncSetUp(self)
        self.app.include_router(api.router, prefix="/api/os")
        self.path = "/api/os/runtime/a2a/profiles/review"

    asyncTearDown = principals.PrincipalTests.asyncTearDown
    save = principals.PrincipalTests.save
    headers_for = principals.PrincipalTests.headers_for

    def params(self, message_id="message-one", **message):
        return {
            "message": {
                "kind": "message",
                "role": "user",
                "messageId": message_id,
                "contextId": "上下文-one",
                "parts": [
                    {
                        "kind": "data",
                        "data": {"goal": "检查边界条件", "sample_id": "first-item"},
                    }
                ],
                **message,
            },
            "configuration": {"blocking": False},
        }

    async def rpc(self, method, params, *, user="a", path=None, **wire):
        result = await self.client.post(
            path or self.path,
            headers=self.headers_for(user),
            json={
                "jsonrpc": "2.0",
                "id": "rpc-one",
                "method": method,
                "params": params,
                **wire,
            },
        )
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("no-store", result.headers["cache-control"])
        return result.json()

    async def submit(self, message_id="message-one", user="a"):
        response = await self.rpc("message/send", self.params(message_id), user=user)
        self.assertIn("result", response, response)
        return response["result"]

    async def count(self, table):
        async with self.db.read("one") as connection:
            return (
                await connection.execute(select(func.count()).select_from(table))
            ).scalar_one()

    async def test_official_client_card_submit_get_cancel(self):
        response = await self.client.get(
            self.path + "/.well-known/agent-card.json", headers=self.headers_for("a")
        )
        self.assertEqual(200, response.status_code, response.text)
        card = AgentCard.model_validate(response.json())
        self.assertEqual("0.3.0", card.protocol_version)
        self.assertEqual("https://github.com/HQIT/DiOS", card.provider.url)
        self.assertFalse(card.capabilities.streaming)
        self.assertFalse(card.capabilities.push_notifications)
        self.client.headers.update(self.headers_for("a"))
        client = ClientFactory(
            ClientConfig(httpx_client=self.client, streaming=False, polling=True)
        ).create(card)
        events = [
            event
            async for event in client.send_message(
                Message.model_validate(self.params()["message"])
            )
        ]
        task = events[0][0]
        self.assertEqual("task", task.kind)
        self.assertEqual("submitted", task.status.state)
        self.assertEqual("上下文-one", task.context_id)
        self.assertEqual(
            task.id, (await client.get_task(TaskQueryParams(id=task.id))).id
        )
        self.assertEqual(
            "canceled",
            (await client.cancel_task(TaskIdParams(id=task.id))).status.state,
        )
        self.assertEqual(1, await self.count(runs))
        self.assertEqual(1, await self.count(work))

    async def test_strict_wire_errors_and_unsupported_capabilities(self):
        for method, params, wire, code in [
            ("tasks/get", {"id": "absent"}, {"jsonrpc": "1.0"}, -32600),
            ("tasks/get", [], {}, -32602),
            ("tasks/get", {"id": "absent"}, {}, -32001),
            ("tasks/get", {"id": "absent", "historyLength": -1}, {}, -32602),
            ("tasks.get", {"id": "absent"}, {}, -32601),
            (
                "message/send",
                {**self.params(), "configuration": {"blocking": True}},
                {},
                -32004,
            ),
            ("message/send", self.params(taskId="some-task"), {}, -32004),
            (
                "message/send",
                self.params(parts=[{"kind": "text", "text": "arbitrary work"}]),
                {},
                -32602,
            ),
        ]:
            with self.subTest(method=method, code=code, wire=wire):
                response = await self.rpc(method, params, **wire)
                self.assertEqual(code, response["error"]["code"], response)
        for method, params in [
            ("message/stream", self.params()),
            ("tasks/resubscribe", {"id": "absent"}),
        ]:
            response = await self.client.post(
                self.path,
                headers=self.headers_for("a"),
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            self.assertIn('"code":-32004', response.text.replace(" ", ""))
        self.assertEqual(0, await self.count(runs))

    async def test_concurrent_replay_context_conflict_and_restart(self):
        first, second = await asyncio.gather(self.submit(), self.submit())
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(1, await self.count(work))
        conflict = await self.rpc("message/send", self.params(contextId="different"))
        self.assertEqual(-32602, conflict["error"]["code"])
        self.assertEqual("IDEMPOTENCY_CONFLICT", conflict["error"]["message"])
        new_db = Database("sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"))
        try:
            self.app.state.runtime_console = TaskConsole(
                new_db, "one", self.profiles, self.root / "objects", policy=self.policy
            )
            self.assertEqual(first["id"], (await self.submit())["id"])
            detail = await self.rpc("tasks/get", {"id": first["id"]})
            self.assertEqual("上下文-one", detail["result"]["contextId"])
        finally:
            self.app.state.runtime_console = self.console
            await new_db.close()
        self.assertEqual(1, await self.count(runs))

    async def test_response_lost_after_commit_reuses_work_and_binding(self):
        original = TaskConsole.detail

        async def lost(console, task_id):
            raise ConnectionError("private provider details must not be sent")

        with patch.object(TaskConsole, "detail", lost):
            response = await self.rpc("message/send", self.params())
            self.assertEqual(-32603, response["error"]["code"])
            self.assertNotIn("private provider", json.dumps(response))
        run = await self.submit()
        self.assertEqual("submitted", run["status"]["state"])
        self.assertEqual(1, await self.count(runs))
        self.assertEqual(1, await self.count(work))
        self.assertIs(TaskConsole.detail, original)

    async def test_auth_owner_share_cancel_and_profile_boundaries(self):
        self.assertEqual(
            401,
            (
                await self.client.get(self.path + "/.well-known/agent-card.json")
            ).status_code,
        )
        a, b = await self.submit(user="a"), await self.submit(user="b")
        self.assertNotEqual(a["id"], b["id"])
        for method in ("tasks/get", "tasks/cancel"):
            self.assertEqual(
                -32001,
                (await self.rpc(method, {"id": a["id"]}, user="b"))["error"]["code"],
            )
        self.directory["principals"]["b"]["objects"][a["id"]] = ["run.read"]
        self.save()
        self.assertIn("result", await self.rpc("tasks/get", {"id": a["id"]}, user="b"))
        self.assertEqual(
            -32001,
            (await self.rpc("tasks/cancel", {"id": a["id"]}, user="b"))["error"][
                "code"
            ],
        )
        self.assertFalse((await self.console.store.get(a["id"]))["cancel_requested"])
        rest = await principals.PrincipalTests.submit_as(self, "a", "rest-task")
        self.assertEqual(
            -32001, (await self.rpc("tasks/get", {"id": rest["id"]}))["error"]["code"]
        )

    async def test_historical_queries_survive_profile_removal_and_shared_reader(self):
        task = await self.submit()
        self.directory["principals"]["b"]["profiles"].clear()
        self.directory["principals"]["b"]["objects"][task["id"]] = ["run.read"]
        self.save()
        (self.profiles / "review.json").unlink()
        self.assertEqual(
            404,
            (
                await self.client.get(
                    self.path + "/.well-known/agent-card.json",
                    headers=self.headers_for("a"),
                )
            ).status_code,
        )
        for user in ("a", "b"):
            result = await self.rpc("tasks/get", {"id": task["id"]}, user=user)
            self.assertEqual(task["id"], result["result"]["id"])
        self.assertEqual(
            -32001,
            (await self.rpc("tasks/cancel", {"id": task["id"]}, user="b"))["error"][
                "code"
            ],
        )
        self.assertEqual(
            "canceled",
            (await self.rpc("tasks/cancel", {"id": task["id"]}))["result"]["status"][
                "state"
            ],
        )

    async def test_missing_version_and_boolean_request_id_are_invalid(self):
        for value in (
            {"id": 1, "method": "tasks/get", "params": {"id": "absent"}},
            {
                "jsonrpc": "2.0",
                "id": True,
                "method": "tasks/get",
                "params": {"id": "absent"},
            },
        ):
            response = await self.client.post(
                self.path, headers=self.headers_for("a"), json=value
            )
            self.assertEqual(-32600, response.json()["error"]["code"])

    async def test_malformed_json_and_utf8_preserve_null_error_identity(self):
        for body, code in [
            (b"{", -32700),
            (b"\xff", -32700),
            (b"[]", -32600),
            (b"null", -32600),
        ]:
            response = await self.client.post(
                self.path, headers=self.headers_for("a"), content=body
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual(code, response.json()["error"]["code"])
            self.assertIn("id", response.json())
            self.assertIsNone(response.json()["id"])

    async def test_e2ag_revoked_work_never_launches(self):
        task = await self.submit()
        detail = await self.console.detail(task["id"])
        self.assertEqual("subject:a", detail["authorization"]["subject"])
        self.directory["principals"]["a"]["enabled"] = False
        self.save()
        adapter = FakeAdapter()
        result = await Worker(
            self.console.store,
            adapter,
            "review",
            self.spec,
            self.console.provider,
            authorization_verifier=self.verifier,
        ).once()
        self.assertEqual("failed", result["state"])
        self.assertEqual(0, adapter.started)
        response = await self.client.post(
            self.path,
            headers=self.headers_for("a"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/get",
                "params": {"id": task["id"]},
            },
        )
        self.assertEqual(401, response.status_code)

    async def test_result_artifact_permissions_and_terminal_cancel(self):
        task = await self.submit()
        await principals.PrincipalTests.finish(self)
        result = (await self.rpc("tasks/get", {"id": task["id"]}))["result"]
        self.assertEqual("completed", result["status"]["state"])
        blob = result["artifacts"][0]
        uri = blob["parts"][0]["file"]["uri"]
        self.assertEqual(
            200, (await self.client.get(uri, headers=self.headers_for("a"))).status_code
        )
        self.assertEqual(
            404, (await self.client.get(uri, headers=self.headers_for("b"))).status_code
        )
        self.assertEqual(
            -32002,
            (await self.rpc("tasks/cancel", {"id": task["id"]}))["error"]["code"],
        )
        self.directory["principals"]["b"]["objects"][task["id"]] = ["run.read"]
        self.save()
        self.assertNotIn(
            "artifacts",
            (await self.rpc("tasks/get", {"id": task["id"]}, user="b"))["result"],
        )
        self.assertNotIn("storage_key", json.dumps(result))

    async def test_active_cancel_waits_for_exit_evidence_and_repeats_idempotently(self):
        task = await self.submit()
        store = self.console.store
        await store.register_worker("worker", "instance", "host", 123)
        attempt = await store.claim("worker", "instance")
        for _ in range(2):
            result = (await self.rpc("tasks/cancel", {"id": task["id"]}))["result"]
            self.assertEqual("working", result["status"]["state"])
            self.assertTrue(result["metadata"]["dios"]["cancelRequested"])
        self.assertEqual(
            1,
            sum(
                e["type"] == "run.cancel_requested"
                for e in (await self.console.detail(task["id"]))["events"]
            ),
        )
        await store.confirm_cancel(
            task["id"],
            attempt["id"],
            "worker",
            "instance",
            attempt["epoch"],
            "test:exit-confirmed",
        )
        result = (await self.rpc("tasks/get", {"id": task["id"]}))["result"]
        self.assertEqual("canceled", result["status"]["state"])

    async def test_unknown_execution_is_observed_without_duplicate_work(self):
        task = await self.submit()
        adapter = FakeAdapter(failure=True)
        with self.assertRaises(TimeoutError):
            await Worker(
                self.console.store,
                adapter,
                "review",
                self.spec,
                self.console.provider,
                authorization_verifier=self.verifier,
            ).once()
        execution = (adapter.running, adapter.stopped, adapter.started)
        unknown = (await self.rpc("tasks/get", {"id": task["id"]}))["result"]
        self.assertEqual("unknown", unknown["status"]["state"])
        self.assertEqual(
            "reconciliation_required", unknown["metadata"]["dios"]["runtimeState"]
        )
        self.assertEqual(task["id"], (await self.submit())["id"])
        self.assertEqual(1, await self.count(runs))
        self.assertEqual(execution, (adapter.running, adapter.stopped, adapter.started))
        self.assertFalse(adapter.removed)

    async def test_chunked_body_bound_and_invalid_input_do_not_enqueue(self):
        async def large_body():
            for _ in range(20):
                yield b"x" * 16384

        response = await self.client.post(
            self.path, headers=self.headers_for("a"), content=large_body()
        )
        self.assertEqual(413, response.status_code)
        value = self.params()
        value["message"]["parts"][0]["data"]["command"] = "forbidden"
        self.assertEqual(
            -32602, (await self.rpc("message/send", value))["error"]["code"]
        )
        self.assertEqual(0, await self.count(runs))
