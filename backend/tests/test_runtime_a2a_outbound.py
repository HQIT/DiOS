"""Remote intent, ownership, recovery and destination control contracts."""

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
import test_runtime_task_console as console_fixtures
from a2a.types import Task
from app.runtime.a2a_transport import A2ATransport
from app.runtime.a2a_worker import A2AWorker
from app.runtime.database import RuntimeErrorCode
from app.runtime.fault_schema import circuits, plans
from app.runtime.schema import attempts
from app.runtime.store import digest, encoded
from app.runtime.worker import read_profile
from work_auth_fixtures import policy_fixture


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    async def test_revision_bound_a2a_worker_loads_frozen_endpoint(self):
        from app.runtime.execution_revisions import compile_profile, load_profile
        from app.runtime.revisions import Revisions
        saved = await Revisions(self.console.store.db, self.console.store.scope).create(
            'a2a-revision', compile_profile(self.profile), actor='qualification')
        ref = saved['revision']['id']
        original = self.console.store.submit
        async def submit_bound(*args, **kwargs):
            kwargs['revision_ref']=ref
            return await original(*args, **kwargs)
        with patch.object(self.console.store, 'submit', side_effect=submit_bound):
            run = await self.submit()
        self.profile['remote']['endpoint'] = 'https://changed.example/rpc'
        _, _, spec, _, _ = await load_profile(self.console.store.db, self.console.store.scope, ref)
        self.assertEqual(spec.endpoint, 'https://remote.example/rpc')
        worker = self.worker()
        worker.revision_ref = ref
        result = await worker.once()
        self.assertEqual(result['id'], run['id'])
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['input']['revision_ref'], ref)

    async def asyncSetUp(self):
        await console_fixtures.TaskConsoleTests.asyncSetUp(self)
        row = {
            "id": "input",
            "name": "Input",
            "parts": [{"kind": "text", "text": "Produce a verified example"}],
        }
        catalog = {
            "version": 1,
            "inputs": [{**row, "sha256": digest(row)}],
            "requires_e2ag": True,
            "result_schema": {
                "type": "object",
                "required": ["summary", "evidence"],
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "evidence": {"type": "array", "minItems": 1},
                },
            },
        }
        self.input_catalog.write_text(json.dumps(catalog))
        self.profile.pop("launch")
        self.profile.update(
            bridge="a2a-0.3-jsonrpc",
            governance={"mode": "e2ag"},
            remote={
                "endpoint": "https://remote.example/rpc",
                "card_url": "https://remote.example/card",
                "skill_id": "remote-task",
                "artifact_prefix": "https://remote.example/artifacts/",
            },
        )
        path = self.profiles / "review.json"
        path.write_text(json.dumps(self.profile))
        _, self.spec = read_profile(path)
        self.policy, _ = policy_fixture(self.root, "review", self.spec)
        self.console.policy = self.policy
        self.calls = []
        self.submit_count = 0
        self.fail_submit = False
        self.fail_get = False
        self.auto_complete = True
        self.cancel_pending = False
        self.started = asyncio.Event()
        self.query = asyncio.Event()
        self.remote = {
            "kind": "task",
            "id": "remote-task-1",
            "contextId": "remote-context",
            "status": {"state": "working"},
        }
        self.card = {
            "name": "Remote",
            "description": "Remote fixture",
            "version": "1",
            "protocolVersion": "0.3.0",
            "url": self.spec.endpoint,
            "capabilities": {"streaming": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [
                {"id": "remote-task", "name": "Task", "description": "Task", "tags": []}
            ],
        }

    asyncTearDown = console_fixtures.TaskConsoleTests.asyncTearDown

    def response(self, value, status=200, headers=None):
        return httpx.Response(
            status,
            stream=httpx.ByteStream(encoded(value)),
            headers=headers or {"content-type": "application/json"},
        )

    def complete(self, value=None):
        self.remote["status"] = {"state": "completed"}
        self.remote["artifacts"] = [
            {
                "artifactId": "result",
                "parts": [
                    {
                        "kind": "data",
                        "data": value
                        if value is not None
                        else {
                            "summary": "Remote result",
                            "evidence": ["fixture:verified"],
                        },
                    }
                ],
            }
        ]

    async def peer(self, request):
        self.calls.append((request.method, str(request.url)))
        if request.method == "GET":
            return self.response(self.card)
        body = json.loads(request.content)
        method = body["method"]
        if method == "message/send":
            self.submit_count += 1
            self.started.set()
            if self.fail_submit:
                raise httpx.ReadTimeout("response dropped", request=request)
        elif method == "tasks/get":
            self.query.set()
            if self.fail_get:
                raise httpx.ReadTimeout("query timed out", request=request)
            if self.auto_complete and self.remote["status"]["state"] == "working":
                self.complete()
        elif method == "tasks/cancel":
            if not self.cancel_pending:
                self.remote["status"] = {"state": "canceled"}
        else:
            raise AssertionError(method)
        return self.response(
            {"jsonrpc": "2.0", "id": body["id"], "result": self.remote}
        )

    def worker(self, **kwargs):
        return A2AWorker(
            self.console.store,
            "review",
            self.spec,
            self.console.provider,
            self.policy,
            poll_interval=0.05,
            timeout=5,
            transport_factory=lambda spec, **opts: A2ATransport(
                spec, transport=httpx.MockTransport(self.peer), **opts
            ),
            **kwargs,
        )

    async def test_shared_capacity_blocks_a2a_before_any_remote_request(self):
        from app.runtime.capacity import Capacity

        capacity = Capacity(self.console.store)
        await capacity.put("shared", {"kind": "scope"}, 0, 0)
        run = await self.submit()
        self.assertEqual((await self.worker().once())["state"], "idle")
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.console.store.get(run["id"]))["attempts"], [])
        await capacity.put("shared", {"kind": "scope"}, 1, 1)
        self.assertEqual((await self.worker().once())["state"], "succeeded")
        self.assertEqual(self.submit_count, 1)
        self.assertEqual((await capacity.status())["items"][0]["reserved_attempts"], 0)

    async def submit(self, key="one"):
        return await self.console.submit("review", "input", "remote task", key)

    async def expire(self, run_id):
        run = await self.console.store.get(run_id)
        async with self.db.transaction("one") as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == run["attempt_id"])
                .values(
                    lease_until=0,
                    remote={
                        **run["attempts"][0]["remote"],
                        "a2a_poll": {
                            **run["attempts"][0]["remote"].get("a2a_poll", {}),
                            "lease_until": 0,
                        },
                    },
                )
            )
        await self.console.store.expire()

    async def due(self, run_id):
        run = await self.console.store.get(run_id)
        attempt = run["attempts"][0]
        async with self.db.transaction("one") as c:
            await c.execute(
                plans.update().where(plans.c.run_id == run_id).values(retry_at=0)
            )
            await c.execute(
                circuits.update().where(circuits.c.scope == "one").values(retry_at=0)
            )
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    remote={
                        **attempt["remote"],
                        "a2a_poll": {
                            **attempt["remote"].get("a2a_poll", {}),
                            "retry_at": 0,
                        },
                    }
                )
            )

    async def test_real_sdk_executes_once_and_links_remote_result_without_fake_pid(
        self,
    ):
        run = await self.submit()
        result = await self.worker().once()
        self.assertEqual("succeeded", result["state"], result)
        self.assertEqual(1, self.submit_count)
        attempt = result["attempts"][0]
        remote = attempt["remote"]["a2a"]
        self.assertEqual("remote-task-1", remote["task_id"])
        self.assertEqual("remote-context", attempt["session_id"])
        self.assertIsNone(attempt["process_id"])
        self.assertIsNone(attempt["process_instance"])
        view = await self.console.detail(run["id"])
        self.assertEqual(1, sum(b["kind"] == "result" for b in view["artifacts"]))
        self.assertEqual("e2ag", view["authorization"]["mode"])
        self.assertEqual("idle", (await self.worker().once())["state"])

    async def test_submit_response_loss_stays_unknown_without_resending(self):
        self.fail_submit = True
        run = await self.submit()
        result = await self.worker().once()
        self.assertEqual("reconciliation_required", result["state"])
        self.fail_submit = False
        restarted = self.worker()
        await restarted.recover()
        await restarted.recover()
        self.assertEqual(1, self.submit_count)
        self.assertIsNone(
            (await self.console.store.get(run["id"]))["attempts"][0]["remote"]["a2a"][
                "task_id"
            ]
        )
        await self.submit("two")
        self.assertEqual("idle", (await restarted.once())["state"])

    async def test_query_timeout_recovers_same_task_without_model_resubmission(self):
        self.fail_get = True
        run = await self.submit()
        result = await self.worker().once()
        self.assertEqual("reconciliation_required", result["state"])
        self.assertEqual(
            "remote-task-1", result["attempts"][0]["remote"]["a2a"]["task_id"]
        )
        self.fail_get = False
        self.complete()
        await self.due(run["id"])
        await self.worker().recover()
        result = await self.console.store.get(run["id"])
        self.assertEqual("succeeded", result["state"], result)
        self.assertEqual(1, self.submit_count)
        self.assertEqual(1, len(result["attempts"]))

    async def test_response_receipt_recovers_after_database_write_failure(self):
        run = await self.submit()
        worker = self.worker()
        with patch.object(
            worker.execution, "observe", side_effect=OSError("database unavailable")
        ):
            result = await worker.once()
        self.assertEqual("reconciliation_required", result["state"])
        self.assertIsNone(result["attempts"][0]["remote"]["a2a"]["task_id"])
        self.complete()
        await self.due(run["id"])
        await self.worker().recover()
        self.assertEqual(
            "succeeded", (await self.console.store.get(run["id"]))["state"]
        )
        self.assertEqual(1, self.submit_count)

    async def test_worker_loss_queries_remote_after_lease_expiry(self):
        self.auto_complete = False
        run = await self.submit()
        worker = self.worker()
        process = asyncio.create_task(worker.once())
        await asyncio.wait_for(self.query.wait(), 3)
        process.cancel()
        await asyncio.gather(process, return_exceptions=True)
        await self.expire(run["id"])
        self.complete()
        current, attempt = await worker.snapshot(run["id"])
        for operation in (
            worker.tick(run["id"]),
            worker.execution.observe(
                current, attempt, Task.model_validate(self.remote)
            ),
            worker.execution.cancel_intent(current, attempt),
            worker.execution.finish(current, attempt, Task.model_validate(self.remote)),
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
                await operation
        await self.worker().recover()
        current = await self.console.store.get(run["id"])
        self.assertEqual("succeeded", current["state"])
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await self.console.store.heartbeat(
                *worker.binding(current, current["attempts"][0])
            )
        self.assertEqual(1, self.submit_count)

    async def test_cancel_requires_remote_terminal_and_restart_preserves_intent(self):
        self.auto_complete = False
        self.cancel_pending = True
        run = await self.submit()
        worker = self.worker()
        process = asyncio.create_task(worker.once())
        await asyncio.wait_for(self.query.wait(), 3)
        current = await self.console.store.get(run["id"])
        await self.console.store.cancel(run["id"], current["version"])
        for _ in range(60):
            current = await self.console.store.get(run["id"])
            if current["attempts"][0]["remote"]["a2a"]["cancel_requested_at"]:
                break
            await asyncio.sleep(0.05)
        self.assertNotIn(current["state"], {"cancelled", "succeeded"})
        process.cancel()
        await asyncio.gather(process, return_exceptions=True)
        await self.expire(run["id"])
        self.cancel_pending = False
        await self.worker().recover()
        self.assertEqual(
            "cancelled", (await self.console.store.get(run["id"]))["state"]
        )
        self.assertEqual(1, self.submit_count)

    async def test_revocation_cancels_remote_and_never_accepts_result(self):
        self.auto_complete = False
        run = await self.submit()
        process = asyncio.create_task(self.worker().once())
        await asyncio.wait_for(self.query.wait(), 3)
        current = await self.console.store.get(run["id"])
        await self.console.store.revoke(run["id"], current["version"])
        result = await asyncio.wait_for(process, 5)
        self.assertEqual("failed", result["state"], result["attempts"][0]["error"])
        self.assertEqual("AUTH_REVOKED", result["attempts"][0]["error"]["code"])
        self.assertEqual("canceled", self.remote["status"]["state"])

    async def test_invalid_result_is_retained_and_fails_schema(self):
        self.complete({"summary": "missing evidence"})
        run = await self.submit()
        result = await self.worker().once()
        self.assertEqual("failed", result["state"])
        self.assertEqual(
            "EVIDENCE_INSUFFICIENT", result["attempts"][0]["error"]["code"]
        )
        view = await self.console.detail(run["id"])
        self.assertTrue(view["artifacts"])
        self.assertFalse(any(a["kind"] == "result" for a in view["artifacts"]))

    async def test_card_redirect_and_cross_origin_artifact_never_receive_credentials(
        self,
    ):
        self.card["url"] = "https://attacker.example/rpc"
        await self.submit()
        result = await self.worker().once()
        self.assertEqual("failed", result["state"])
        self.assertEqual(0, self.submit_count)
        self.assertTrue(all("attacker" not in url for _, url in self.calls))
        transport = A2ATransport(
            self.spec, token="test-credential", transport=httpx.MockTransport(self.peer)
        )
        task = Task.model_validate(
            {
                **self.remote,
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "artifactId": "a",
                        "parts": [
                            {
                                "kind": "file",
                                "file": {
                                    "mimeType": "application/json",
                                    "uri": "https://attacker.example/result",
                                },
                            }
                        ],
                    }
                ],
            }
        )
        try:
            with self.assertRaisesRegex(RuntimeErrorCode, "A2A_DESTINATION_DENIED"):
                await transport.result(task)
        finally:
            await transport.aclose()

    async def test_capacity_is_atomic_across_worker_instances(self):
        self.fail_submit = True
        await self.submit("one")
        await self.submit("two")
        a, b = await asyncio.gather(self.worker().once(), self.worker().once())
        self.assertEqual({"idle", "reconciliation_required"}, {a["state"], b["state"]})
        self.assertEqual(1, self.submit_count)

    async def test_transport_rejects_redirect_oversize_and_wrong_rpc_id(self):
        for mode in ("redirect", "oversize", "wrong-id"):
            seen = []

            async def peer(request, mode=mode, seen=seen):
                seen.append(str(request.url))
                if mode == "redirect":
                    return self.response(
                        {}, 302, {"location": "https://attacker.example"}
                    )
                if mode == "oversize":
                    return self.response({"data": "x" * self.spec.response_limit})
                return self.response(
                    {"jsonrpc": "2.0", "id": "other", "result": self.remote}
                )

            transport = A2ATransport(
                self.spec, token="test-credential", transport=httpx.MockTransport(peer)
            )
            try:
                with self.assertRaises(Exception) as error:
                    if mode == "wrong-id":
                        await transport.observe("remote-task-1")
                    else:
                        await transport.discover()
                if mode != "redirect":
                    self.assertIn(
                        "A2A_RESPONSE_TOO_LARGE"
                        if mode == "oversize"
                        else "A2A_RESPONSE_BINDING_CHANGED",
                        str(error.exception),
                    )
                self.assertEqual(1, len(seen))
                self.assertFalse(any("attacker" in url for url in seen))
            finally:
                await transport.aclose()

    async def test_recovery_rejects_changed_profile_and_retains_unknown(self):
        self.fail_get = True
        run = await self.submit()
        await self.worker().once()
        changed = self.worker()
        changed.spec = replace(self.spec, endpoint="https://remote.example/changed")
        await self.due(run["id"])
        result = await changed.recover()
        self.assertEqual("PROFILE_CHANGED", result[0]["error"])
        self.assertEqual(
            "reconciliation_required",
            (await self.console.store.get(run["id"]))["state"],
        )
