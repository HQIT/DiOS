import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from app.config import settings
from app.models.tables import (
    A2ATask,
    ChatMessage,
    ManagedServiceCall,
    ManagedServiceOwner,
    ManagedServiceResult,
)
from app.runtime.database import RuntimeErrorCode
from app.services import (
    a2a_service,
    managed_admission,
    managed_recovery,
    managed_service_results,
)
from app.services.managed_service_recovery import CallOwner, reconcile, reply_content
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests import test_managed_runtime as fixtures
from tests import test_managed_upgrade as upgrade_fixtures


class ServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    task = fixtures.ManagedRuntimeTests.task
    chat_response = upgrade_fixtures.ManagedUpgradeTests.chat_response

    async def owned(self, dispatch=True):
        await self.task(mode="service")
        owner = CallOwner()
        async with self.sessions() as db:
            await owner.begin(db, "agent", task_id="task")
        if dispatch:
            await managed_admission.dispatch(owner.id)
        return owner

    async def snapshot(self, call_id):
        async with self.sessions() as db:
            call = await db.get(ManagedServiceCall, call_id)
            task = await db.get(A2ATask, "task")
            receipt = await db.get(ManagedServiceResult, call_id)
            return (
                call.state,
                task.status if task else None,
                receipt.applied if receipt else None,
            )

    async def test_live_owner_blocks_reconciliation_and_controller_never_replays(self):
        owner = await self.owned()
        try:
            self.assertEqual(await reconcile(owner.id), "owned")
            self.assertEqual(
                (await managed_recovery.ManagedRecovery().tick())[
                    "service:" + owner.id
                ],
                "owned",
            )
            self.assertEqual((await self.snapshot(owner.id))[0], "active")
        finally:
            await owner.close()
        self.assertEqual(await self.snapshot(owner.id), ("unknown", "working", None))
        self.adapter.start.assert_not_awaited()

    async def test_receipt_recovers_complete_result_without_model_and_is_idempotent(
        self,
    ):
        owner = await self.owned()
        await managed_service_results.retain(
            owner.id, "task", "task", "中文完整报告", complete=True
        )
        await owner.close()
        from app.services.managed_upgrade import Upgrade

        with patch.object(settings, "runtime_database_url", ""):
            item = (await Upgrade().status())["service_calls"][0]
            self.assertTrue(item["result_retained"])
            self.assertTrue(item["result_complete"])
            self.assertFalse(item["result_applied"])
            self.assertNotIn("content", item)
        self.assertEqual(await reconcile(owner.id), "completed")
        self.assertEqual(await reconcile(owner.id), "completed")
        self.assertEqual(
            await self.snapshot(owner.id), ("completed", "completed", True)
        )
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual(task.artifacts[0]["parts"][0]["text"], "中文完整报告")
        self.adapter.start.assert_not_awaited()

    async def test_projection_failure_rolls_back_result_and_completion_together(self):
        owner = await self.owned()
        await managed_service_results.retain(
            owner.id, "task", "task", "report", complete=True
        )

        class FailingSession(AsyncSession):
            async def commit(self):
                await self.flush()
                raise OSError("storage unavailable before commit")

        broken = async_sessionmaker(
            self.engine, class_=FailingSession, expire_on_commit=False
        )
        with patch("app.db.database.async_session", broken):
            with self.assertRaises(OSError):
                await managed_service_results.apply(owner.id)
        self.assertEqual(await self.snapshot(owner.id), ("active", "working", False))
        await owner.close()
        self.assertEqual(await reconcile(owner.id), "completed")

    async def test_corrupt_or_conflicting_receipt_never_becomes_success(self):
        owner = await self.owned()
        await managed_service_results.retain(
            owner.id, "task", "task", "report", complete=True
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "SERVICE_RESULT_CONFLICT"):
            await managed_service_results.retain(
                owner.id, "task", "task", "changed", complete=True
            )
        async with self.sessions() as db:
            receipt = await db.get(ManagedServiceResult, owner.id)
            receipt.record = {**receipt.record, "content": "corrupt"}
            await db.commit()
        await owner.close()
        with self.assertRaisesRegex(RuntimeErrorCode, "SERVICE_RESULT_CORRUPT"):
            await reconcile(owner.id)
        self.assertEqual((await self.snapshot(owner.id))[:2], ("unknown", "working"))

    async def test_partial_receipt_is_retained_but_never_completed(self):
        owner = await self.owned()
        await managed_service_results.retain(
            owner.id, "task", "task", "partial", complete=False
        )
        await owner.close()
        self.assertEqual(await reconcile(owner.id), "unknown")
        self.assertEqual(await self.snapshot(owner.id), ("unknown", "working", True))
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertFalse(task.artifacts[0]["metadata"]["complete"])

    async def test_canceled_target_is_not_overwritten_by_late_receipt(self):
        owner = await self.owned()
        await managed_service_results.retain(
            owner.id, "task", "task", "late", complete=True
        )
        async with self.sessions() as db:
            (await db.get(A2ATask, "task")).status = "canceled"
            await db.commit()
        await owner.close()
        with self.assertRaisesRegex(RuntimeErrorCode, "SERVICE_RESULT_TARGET_SETTLED"):
            await reconcile(owner.id)
        self.assertEqual((await self.snapshot(owner.id))[1], "canceled")

    async def test_legacy_unowned_and_other_scope_calls_are_not_adopted(self):
        async with self.sessions() as db:
            call_id = await managed_admission.begin_call(db, "agent")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "SERVICE_CALL_OWNERSHIP_UNVERIFIED"
        ):
            await reconcile(call_id)
        self.assertEqual(await managed_recovery.ManagedRecovery().tick(), {})
        async with self.sessions() as db:
            db.add(ManagedServiceOwner(call_id=call_id, scope="other"))
            await db.commit()
        self.assertEqual(await managed_recovery.ManagedRecovery().tick(), {})

    async def test_native_process_death_before_dispatch_and_after_result_receipt(self):
        await self.task(mode="service")
        for phase in ("before", "dispatched", "receipt"):
            task_id = "task-" + phase
            async with self.sessions() as db:
                db.add(
                    A2ATask(
                        id=task_id,
                        agent_id="agent",
                        status="working",
                        message={},
                        artifacts=[],
                    )
                )
                await db.commit()
            code = """
import asyncio,sys
from app.db.database import async_session
from app.services.managed_service_recovery import CallOwner
from app.services import managed_admission,managed_service_results
async def main():
 owner=CallOwner()
 async with async_session() as db: await owner.begin(db,'agent',task_id=sys.argv[2])
 if sys.argv[1]!='before': await managed_admission.dispatch(owner.id)
 if sys.argv[1]=='receipt': await managed_service_results.retain(owner.id,'task',sys.argv[2],'native 完整结果',complete=True)
 print(owner.id,flush=True)
 await asyncio.Event().wait()
asyncio.run(main())
"""
            env = dict(
                os.environ,
                DIOS_DATABASE_URL=str(self.engine.url),
                DIOS_RUNTIME_SCOPE=settings.runtime_scope,
                DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
            )
            p = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                phase,
                task_id,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                call_id = (
                    (await asyncio.wait_for(p.stdout.readline(), 15)).decode().strip()
                )
                self.assertEqual(len(call_id), 32)
                self.assertEqual(await reconcile(call_id), "owned")
                p.kill()
                await asyncio.wait_for(p.wait(), 10)
                expected = {
                    "before": "failed",
                    "dispatched": "unknown",
                    "receipt": "completed",
                }[phase]
                self.assertEqual(await reconcile(call_id), expected)
            finally:
                if p.returncode is None:
                    p.kill()
                    await p.wait()

    async def test_native_http_receipt_survives_kill_and_new_cli_recovers_once(self):
        await self.task(mode="service")
        requests = []

        async def upstream(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(
                int(line.split(b":", 1)[1])
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(size)))
            body = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": "native HTTP 中文报告"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        code = """
import asyncio,sys
from app.services import a2a_service,agent_runtime,managed_service_results
async def endpoint(*args,**kwargs): return sys.argv[1]
async def held(call_id):
 print(call_id,flush=True)
 await asyncio.Event().wait()
agent_runtime.ensure_running=endpoint
managed_service_results.apply=held
asyncio.run(a2a_service._run_service_call('task','agent'))
"""
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
            DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
        )
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            f"http://127.0.0.1:{port}",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            call_id = (await asyncio.wait_for(p.stdout.readline(), 15)).decode().strip()
            self.assertEqual(len(call_id), 32)
            self.assertEqual(await self.snapshot(call_id), ("active", "working", False))
            p.kill()
            await p.wait()
            for _ in range(2):
                recovery = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.services.managed_recovery",
                    "reconcile",
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(recovery.communicate(), 15)
                self.assertEqual(recovery.returncode, 0, err.decode())
                self.assertTrue(json.loads(out)["status"]["executions_settled"])
            self.assertEqual(
                await self.snapshot(call_id), ("completed", "completed", True)
            )
            self.assertEqual(len(requests), 1)
        finally:
            if p.returncode is None:
                p.kill()
                await p.wait()
            server.close()
            await server.wait_closed()

    async def test_a2a_actual_http_path_persists_receipt_and_recovers_projection(self):
        await self.task(mode="service")
        calls = []

        async def handler(request):
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "真实协议报告"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        with (
            patch(
                "app.services.agent_runtime.ensure_running",
                AsyncMock(return_value="http://fixture"),
            ),
            patch(
                "app.services.http_internal.internal_client",
                lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
            patch.object(
                managed_service_results, "apply", AsyncMock(side_effect=OSError("disk"))
            ),
        ):
            await a2a_service._run_service_call("task", "agent")
        async with self.sessions() as db:
            call = (await db.scalars(select(ManagedServiceCall))).one()
            call_id = call.id
        self.assertEqual(await self.snapshot(call_id), ("unknown", "working", False))
        self.assertEqual(await reconcile(call_id), "completed")
        self.assertEqual(len(calls), 1)

    async def test_stream_partial_last_delta_is_saved_and_receipt_marks_incomplete(
        self,
    ):
        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"last"}}]}\n\n'

        response = await self.chat_response(chunks=chunks())
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        async with self.sessions() as db:
            receipt = (await db.scalars(select(ManagedServiceResult))).one()
            message = (
                await db.scalars(
                    select(ChatMessage).where(ChatMessage.role == "assistant")
                )
            ).one()
            self.assertEqual(message.content, "last")
            self.assertFalse(receipt.record["complete"])
            self.assertTrue(receipt.applied)
            self.assertEqual(
                (await db.get(ManagedServiceCall, receipt.call_id)).state, "unknown"
            )

    async def test_stream_done_without_answer_or_with_truncation_is_not_success(self):
        for data in (
            b"data: [DONE]\n\n",
            b'data: {"choices":[{"delta":{"content":"cut"},"finish_reason":"length"}]}\n\ndata: [DONE]\n\n',
        ):

            async def chunks(data=data):
                yield data

            response = await self.chat_response(chunks=chunks())
            async for _ in response.body_iterator:
                pass
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(
                    select(func.count())
                    .select_from(ManagedServiceCall)
                    .where(ManagedServiceCall.state == "completed")
                ),
                0,
            )

    async def test_stream_usage_and_optional_data_space_preserve_complete_answer(self):
        async def chunks():
            yield b'data:{"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
            yield b'data: {"choices":[],"usage":{"total_tokens":10}}\n\n'
            yield b"data:[DONE]\n\n"

        response = await self.chat_response(chunks=chunks())
        async for _ in response.body_iterator:
            pass
        async with self.sessions() as db:
            call = (await db.scalars(select(ManagedServiceCall))).one()
            self.assertEqual(call.state, "completed")

    async def test_chat_persisted_receipt_projects_once_after_restart(self):
        with patch.object(
            managed_service_results, "apply", AsyncMock(side_effect=OSError("disk"))
        ):
            with self.assertRaises(OSError):
                await self.chat_response(stream=False)
        async with self.sessions() as db:
            call_id = (await db.scalars(select(ManagedServiceCall.id))).one()
        self.assertEqual(await reconcile(call_id), "completed")
        self.assertEqual(await reconcile(call_id), "completed")
        async with self.sessions() as db:
            self.assertEqual(
                await db.scalar(
                    select(func.count())
                    .select_from(ChatMessage)
                    .where(ChatMessage.role == "assistant")
                ),
                1,
            )

    async def test_nonstream_validation_requires_complete_nonempty_text(self):
        for value in (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": " "}}]},
            {"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]},
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "SERVICE_RESULT_INCOMPLETE"):
                reply_content(value)

    async def test_duplicate_task_dispatch_cannot_replace_original_call(self):
        owner = await self.owned()
        try:
            await a2a_service._run_service_call("task", "agent")
            self.assertEqual(await self.snapshot(owner.id), ("active", "working", None))
            async with self.sessions() as db:
                self.assertEqual(
                    await db.scalar(
                        select(func.count()).select_from(ManagedServiceCall)
                    ),
                    1,
                )
        finally:
            await owner.close()

    async def test_drain_query_timeout_keeps_one_original_observation_alive(self):
        from app.services.managed_upgrade import Upgrade

        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def observe():
            entered.set()
            await release.wait()
            finished.set()
            return {"ready_for_cutover": False, "mode": "draining"}

        upgrade = Upgrade()
        with patch.object(upgrade, "status", side_effect=observe) as status:
            try:
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "DRAIN_OBSERVATION_TIMEOUT"
                ):
                    await upgrade.wait(0.01)
                self.assertTrue(entered.is_set())
                self.assertFalse(finished.is_set())
                status.assert_called_once()
            finally:
                release.set()
                await asyncio.wait_for(finished.wait(), 2)

    async def test_stream_header_send_failure_releases_owner_without_dispatch(self):
        from starlette.requests import ClientDisconnect

        async def chunks():
            yield b"data: [DONE]\n\n"

        response = await self.chat_response(chunks=chunks())

        async def send(_):
            raise OSError("client disconnected before headers")

        async def receive():
            await asyncio.Event().wait()

        with self.assertRaises(ClientDisconnect):
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send
            )
        async with self.sessions() as db:
            call = (await db.scalars(select(ManagedServiceCall))).one()
            self.assertEqual(call.state, "failed")
            self.assertFalse(call.dispatched)
        self.assertEqual(await reconcile(call.id), "failed")

    async def test_interruption_revokes_existing_e2ag_grants_without_minting_new(self):
        from datetime import UTC, datetime, timedelta

        from app.models.tables import E2AGToolGrant

        owner = await self.owned()
        async with self.sessions() as db:
            db.add(
                E2AGToolGrant(
                    id="grant",
                    task_id="task",
                    agent_id="agent",
                    mcp_server_id="mcp",
                    token_hash="fixture",
                    trace_id="trace",
                    allowed_tools=["read"],
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
        await owner.close()
        async with self.sessions() as db:
            self.assertEqual((await db.get(E2AGToolGrant, "grant")).status, "revoked")
            self.assertEqual(
                await db.scalar(select(func.count()).select_from(E2AGToolGrant)), 1
            )
