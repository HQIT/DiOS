import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app.config import settings
from app.models.tables import A2ATask, AgentRuntime, ChatSession, ManagedServiceCall
from app.runtime.database import RuntimeErrorCode
from app.services import a2a_service, managed_admission, managed_runtime
from app.services.managed_upgrade import Upgrade
from fastapi import FastAPI
from sqlalchemy import func, select
from starlette.requests import Request
from tests import test_managed_runtime as fixtures


class ManagedUpgradeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    task = fixtures.ManagedRuntimeTests.task

    async def setup_upgrade(self):
        self.stack.enter_context(patch.object(settings, "runtime_database_url", ""))
        return Upgrade()

    async def begin(self):
        async with self.sessions() as db:
            return await managed_admission.begin_call(db, "agent")

    async def test_drain_survives_new_process_and_rejects_before_task_creation(self):
        upgrade = await self.setup_upgrade()
        first = await upgrade.drain()
        self.assertTrue(first["ready_for_cutover"])
        self.assertEqual(first["revision"], (await upgrade.drain())["revision"])
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_DATABASE_URL="",
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
        )
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.services.managed_upgrade",
            "status",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(p.communicate(), 15)
        self.assertEqual(p.returncode, 0, err.decode())
        self.assertEqual(json.loads(out)["revision"], first["revision"])
        async with self.sessions() as db:
            with self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_DRAINING"):
                await a2a_service.send_message(
                    db, "agent", {"parts": [{"text": "test"}]}
                )
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(A2ATask)), 0
            )
        self.adapter.start.assert_not_awaited()
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await upgrade.resume(first["revision"] + 1)
        self.assertEqual((await upgrade.resume(first["revision"]))["mode"], "running")
        await self.begin()

    async def test_admitted_task_can_dispatch_during_drain_and_blocks_cutover(self):
        upgrade = await self.setup_upgrade()
        await self.task(mode="service")
        self.assertFalse((await upgrade.drain())["ready_for_cutover"])
        async with self.sessions() as db:
            call_id = await managed_admission.begin_call(db, "agent", task_id="task")
            task = await db.get(A2ATask, "task")
            task.status = "completed"
            await db.commit()
        self.assertEqual((await upgrade.status())["active_service_calls"], 1)
        await managed_admission.dispatch(call_id)
        await managed_admission.finish(call_id, complete=True)
        self.assertTrue((await upgrade.status())["ready_for_cutover"])

    async def test_unknown_request_prevents_idle_service_stop_and_timeout_never_kills(
        self,
    ):
        upgrade = await self.setup_upgrade()
        call_id = await self.begin()
        async with self.sessions() as db:
            db.add(
                AgentRuntime(
                    agent_id="agent",
                    container_id=managed_runtime.handle("service", "agent"),
                    url="fixture",
                    status="running",
                )
            )
            await db.commit()
        await managed_admission.dispatch(call_id)
        await managed_admission.finish(call_id, error="ReadTimeout")
        snapshot = await upgrade.drain()
        self.assertEqual(snapshot["unknown_service_calls"], 1)
        self.assertEqual(snapshot["service_calls"][0]["id"], call_id)
        self.assertTrue(snapshot["service_calls"][0]["updated_at"].endswith("+00:00"))
        with self.assertRaisesRegex(RuntimeErrorCode, "ACTIVE_OR_UNKNOWN_EXECUTION"):
            await upgrade.settle_services()
        with self.assertRaisesRegex(RuntimeErrorCode, "DRAIN_(OBSERVATION_)?TIMEOUT"):
            await upgrade.wait(0.02)
        self.adapter.stop.assert_not_awaited()
        # Actual late completion proof is distinct from a timeout observation.
        await managed_admission.finish(call_id, complete=True)
        snapshot = await upgrade.settle_services()
        self.assertTrue(snapshot["ready_for_cutover"])
        self.adapter.stop.assert_awaited_once()
        self.adapter.logs.assert_awaited_once()
        self.adapter.remove.assert_awaited_once()

    async def test_predispatch_failure_is_settled(self):
        upgrade = await self.setup_upgrade()
        call_id = await self.begin()
        await managed_admission.finish(call_id, error="ConfigurationError")
        self.assertTrue((await upgrade.drain())["ready_for_cutover"])
        async with self.sessions() as db:
            self.assertEqual(
                (await db.get(ManagedServiceCall, call_id)).state, "failed"
            )

    async def test_kernel_failure_keeps_admission_closed_and_requires_both_revisions(
        self,
    ):
        await self.setup_upgrade()
        kernel = AsyncMock()
        kernel.status.return_value = {
            "mode": "draining",
            "revision": 7,
            "ready_for_cutover": True,
        }
        kernel.begin_drain.side_effect = RuntimeErrorCode("DATABASE_BUSY")
        upgrade = Upgrade(kernel)
        with self.assertRaisesRegex(RuntimeErrorCode, "DATABASE_BUSY"):
            await upgrade.drain()
        with self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_DRAINING"):
            await self.begin()
        kernel.begin_drain.side_effect = None
        snapshot = await upgrade.drain()
        with self.assertRaisesRegex(RuntimeErrorCode, "KERNEL_REVISION_REQUIRED"):
            await upgrade.resume(snapshot["revision"])
        kernel.resume.side_effect = RuntimeErrorCode("RESTORE_RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
        ):
            await upgrade.resume(snapshot["revision"], 7)
        self.assertEqual((await upgrade.status())["mode"], "draining")
        kernel.resume.side_effect = None
        await upgrade.resume(snapshot["revision"], 7)
        kernel.resume.assert_awaited_with(7)

    async def test_unavailable_kernel_observation_is_not_ready(self):
        await self.setup_upgrade()
        kernel = AsyncMock()
        kernel.status.side_effect = TimeoutError()
        snapshot = await Upgrade(kernel).drain()
        self.assertFalse(snapshot["ready_for_cutover"])
        self.assertEqual(snapshot["kernel"]["error"], "TimeoutError")
        self.assertFalse(snapshot["ready_to_stop_services"])

    async def test_serialized_waiter_cancellation_does_not_release_owner_or_leak_lock(
        self,
    ):
        await self.setup_upgrade()
        async with managed_runtime.serialized("admission:" + settings.runtime_scope):
            pending = asyncio.create_task(self.begin())
            await asyncio.sleep(0.05)
            self.assertFalse(pending.done())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RUNTIME_OPERATION_IN_PROGRESS"
            ):
                async with managed_runtime.exclusive(
                    "admission:" + settings.runtime_scope
                ):
                    pass
        ids = await asyncio.gather(*(self.begin() for _ in range(8)))
        self.assertEqual(len(set(ids)), 8)

    async def test_child_task_cannot_inherit_parent_admission_ownership(self):
        await self.setup_upgrade()
        async with self.sessions() as db, managed_admission.gate(db):
            child = asyncio.create_task(self.begin())
            await asyncio.sleep(0.1)
            self.assertFalse(child.done())
        await asyncio.wait_for(child, 3)

    async def test_api_revision_conflict_is_observable(self):
        await self.setup_upgrade()
        self.stack.enter_context(
            patch.object(settings, "access_token", "test-operator")
        )
        from app.api.os.upgrade import router

        app = FastAPI()
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            self.assertEqual((await client.post("/upgrade/drain")).status_code, 401)
            client.headers["Authorization"] = "Bearer test-operator"
            snapshot = (await client.post("/upgrade/drain")).json()
            self.assertTrue(snapshot["ready_for_cutover"])
            self.assertEqual(
                (
                    await client.post("/upgrade/resume", json={"revision": 999})
                ).status_code,
                409,
            )
            self.assertEqual(
                (
                    await client.post(
                        "/upgrade/resume", json={"revision": snapshot["revision"]}
                    )
                ).json()["mode"],
                "running",
            )

    async def test_event_fanout_finishes_admission_before_drain(self):
        upgrade = await self.setup_upgrade()
        from app.services.event_dispatcher import dispatch_event
        from tests.test_e2ag_dispatcher import event

        self.stack.enter_context(patch.object(settings, "e2ag_mode", "off"))
        entered, released = asyncio.Event(), asyncio.Event()

        async def launch(*_):
            entered.set()
            await released.wait()

        async def fanout():
            async with self.sessions() as db:
                return await dispatch_event(
                    event("manual/test", "manual.trigger"), ["agent", "agent"], db
                )

        with patch.object(a2a_service, "_send_to_task_agent", new=launch):
            sending = asyncio.create_task(fanout())
            await asyncio.wait_for(entered.wait(), 3)
            draining = asyncio.create_task(upgrade.drain())
            try:
                await asyncio.sleep(0.1)
                self.assertFalse(draining.done())
            finally:
                released.set()
            log, error = await asyncio.wait_for(sending, 3)
            self.assertEqual(log.status, "dispatched")
            self.assertIsNone(error)
            snapshot = await asyncio.wait_for(draining, 3)
            self.assertEqual(snapshot["active_protocol_tasks"], 2)

    async def test_drain_does_not_consume_approval_or_retry_budget(self):
        upgrade = await self.setup_upgrade()
        from app.models.tables import E2AGApproval, EventLog
        from app.services import event_retry_worker
        from app.services.e2ag_approval import decide_approval
        from app.services.event_dispatcher import dispatch_event
        from tests.test_e2ag_approval import sensitive_event

        self.stack.enter_context(patch.object(settings, "e2ag_mode", "enforce"))
        self.stack.enter_context(
            patch.object(event_retry_worker, "async_session", self.sessions)
        )
        async with self.sessions() as db:
            log, _ = await dispatch_event(sensitive_event(), [], db)
            approval = (await db.execute(select(E2AGApproval))).scalar_one()
            approval_id, log_id = approval.id, log.id
        snapshot = await upgrade.drain()
        with patch.object(
            event_retry_worker.EventRetryWorker,
            "_tick_admitted",
            new_callable=AsyncMock,
        ) as tick:
            await event_retry_worker.EventRetryWorker()._tick()
            tick.assert_not_awaited()
        async with self.sessions() as db:
            with self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_DRAINING"):
                await decide_approval(
                    db, approval_id, decision="approved", actor="test"
                )
            self.assertEqual(
                (await db.get(E2AGApproval, approval_id)).status, "pending"
            )
            self.assertEqual((await db.get(EventLog, log_id)).retry_count, 0)
        await upgrade.resume(snapshot["revision"])
        async with self.sessions() as db:
            approval, log = await decide_approval(
                db, approval_id, decision="approved", actor="test"
            )
            self.assertEqual(approval.status, "approved")
            self.assertEqual(log.status, "dispatched")

    async def chat_response(self, *, stream=True, chunks=None):
        from app.api.apps import chat

        self.stack.enter_context(patch.object(chat, "async_session", self.sessions))
        self.stack.enter_context(
            patch.object(
                chat, "ensure_running", AsyncMock(return_value="http://fixture")
            )
        )

        async def handler(_):
            if stream:

                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        async for chunk in chunks:
                            yield chunk

                return httpx.Response(200, stream=Stream())
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "中文结果"}}]}
            )

        self.stack.enter_context(
            patch.object(
                chat,
                "internal_client",
                lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            )
        )
        request = Request({"type": "http"})
        request._json = {
            "agent_id": "agent",
            "stream": stream,
            "messages": [{"role": "user", "content": "question"}],
        }
        async with self.sessions() as db:
            return await chat.chat_completions(request, db)

    async def test_stream_remains_active_until_upstream_done_and_saved(self):
        upgrade = await self.setup_upgrade()

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        response = await self.chat_response(chunks=chunks())
        it = response.body_iterator
        await anext(it)
        snapshot = await upgrade.drain()
        self.assertEqual(snapshot["active_service_calls"], 1)
        self.assertFalse(snapshot["ready_for_cutover"])
        async for _ in it:
            pass
        self.assertTrue((await upgrade.status())["ready_for_cutover"])

    async def test_truncated_or_disconnected_stream_remains_unknown(self):
        upgrade = await self.setup_upgrade()

        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'

        response = await self.chat_response(chunks=chunks())
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        self.assertEqual((await upgrade.drain())["unknown_service_calls"], 1)
        self.assertFalse((await upgrade.status())["ready_for_cutover"])

    async def test_nonstream_completion_is_settled_and_draining_chat_creates_no_session(
        self,
    ):
        upgrade = await self.setup_upgrade()
        await self.chat_response(stream=False)
        self.assertTrue((await upgrade.drain())["ready_for_cutover"])
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            await self.chat_response(stream=False)
        self.assertEqual(caught.exception.status_code, 503)
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(ChatSession)), 1
            )
