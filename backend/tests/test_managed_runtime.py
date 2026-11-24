from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.db.database import Base
from app.models.tables import A2ATask, Agent, AgentRuntime
from app.runtime.adapter import LaunchSpec, LogSnapshot, Observation
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import _native_call
from app.services import a2a_service, agent_runtime, managed_runtime
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def observation(state="running", alive=True):
    return Observation(
        "handle",
        state,
        alive,
        17 if alive else None,
        "instance-1",
        None if alive else 0,
        "not_configured",
        "fixture",
    )


class ManagedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root}/api.sqlite")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(settings, "workspace_root", self.root))
        self.stack.enter_context(patch.object(settings, "host_workspace_root", ""))
        self.stack.enter_context(
            patch.object(settings, "managed_runtime_lock_dir", str(self.root / "locks"))
        )
        self.stack.enter_context(patch("app.db.database.async_session", self.sessions))
        self.adapter = AsyncMock()
        self.adapter.observe.return_value = observation("absent", False)
        self.adapter.start.return_value = observation()
        self.adapter.stop.return_value = observation("exited", False)
        self.adapter.logs.return_value = LogSnapshot("中文日志".encode(), b"diagnostic")
        self.adapter.endpoint.return_value = "http://fixture:8000"
        self.stack.enter_context(
            patch(
                "app.services.managed_runtime.managed_adapter",
                return_value=self.adapter,
            )
        )
        async with self.sessions() as db:
            db.add(
                Agent(
                    id="agent",
                    name="Test",
                    mode="task",
                    workspace_path=str(self.root / "agent"),
                )
            )
            await db.commit()
        (self.root / "agent").mkdir()

    async def asyncTearDown(self):
        self.stack.close()
        await self.engine.dispose()
        self.temp.cleanup()

    async def task(self, state="working", mode="task"):
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.mode = mode
            task = A2ATask(
                id="task", agent_id="agent", status=state, message={}, artifacts=[]
            )
            db.add(task)
            await db.commit()
        return task

    async def test_existing_launch_digests_are_unchanged(self):
        spec = LaunchSpec("fixture", ("python", "main.py"))
        legacy = asdict(spec)
        legacy.pop("host_aliases")
        self.assertEqual(
            hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest(),
            spec.fingerprint(),
        )
        LaunchSpec("fixture", None).validate()

    async def test_cancellation_drains_native_operation_before_releasing_owner(self):
        entered, release = threading.Event(), threading.Event()

        def native():
            entered.set()
            release.wait(5)

        async def operation():
            async with managed_runtime.exclusive("native-operation"):
                await _native_call(native)

        task = asyncio.create_task(operation())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RUNTIME_OPERATION_IN_PROGRESS"
            ):
                async with managed_runtime.exclusive("native-operation"):
                    pass
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_http_cancellation_reports_standard_error_and_capability(self):
        import httpx
        from app.api.os.a2a import router
        from app.db.database import get_db
        from fastapi import FastAPI

        await self.task(mode="service")

        async def session():
            async with self.sessions() as db:
                yield db

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/a2a/agents/agent",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tasks/cancel",
                    "params": {"id": "task"},
                },
            )
            self.assertEqual(-32002, response.json()["error"]["code"])
            self.assertEqual(
                "TASK_CANCELLATION_UNSUPPORTED",
                response.json()["error"]["data"]["reason"],
            )
            card = (
                await client.get("/a2a/agents/agent/.well-known/agent-card.json")
            ).json()
            self.assertFalse(card["_dios"]["task_cancellation"])

    async def test_task_system_input_overrides_agent_env_and_limits_are_explicit(self):
        await managed_runtime.start_task(
            "task", "run", self.root, {"TASK_CONFIG": "/escape", "X": "value"}
        )
        args = self.adapter.start.call_args.args
        spec = args[1]
        self.assertEqual(
            "/workspace/agent-task-run.json", spec.environment["TASK_CONFIG"]
        )
        self.assertEqual("value", spec.environment["X"])
        self.assertIsNone(spec.command)
        self.assertTrue(spec.read_only_root)
        self.assertEqual(settings.managed_runtime_user, spec.user)
        self.assertEqual(
            [False, True, True], [mount.read_only for mount in spec.mounts]
        )

    async def test_service_reuses_running_object_without_rewriting_workspace(self):
        self.adapter.observe.return_value = observation()
        async with self.sessions() as db:
            with (
                patch(
                    "app.services.agent_runtime._health_check",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "app.services.agent_runtime._sync_agent_workspace", new=AsyncMock()
                ) as sync,
            ):
                self.assertEqual(
                    "http://fixture:8000",
                    await agent_runtime.ensure_running("agent", db),
                )
                sync.assert_not_awaited()
        self.adapter.start.assert_not_awaited()
        self.adapter.remove.assert_not_awaited()

    async def test_new_service_records_intent_and_uses_lifecycle_contract(self):
        async with self.sessions() as db:
            with patch(
                "app.services.agent_runtime._health_check",
                new=AsyncMock(return_value=True),
            ):
                await agent_runtime.ensure_running("agent", db)
            runtime = await db.get(AgentRuntime, "agent")
            self.assertEqual("running", runtime.status)
            self.assertEqual(
                managed_runtime.handle("service", "agent"), runtime.container_id
            )
        self.assertEqual("service", self.adapter.start.call_args.args[1].kind)

    async def test_legacy_record_is_not_implicitly_deleted_or_adopted(self):
        async with self.sessions() as db:
            db.add(
                AgentRuntime(
                    agent_id="agent",
                    container_id="old-container",
                    url="old",
                    status="running",
                )
            )
            await db.commit()
            with self.assertRaisesRegex(
                RuntimeErrorCode, "LEGACY_RUNTIME_REQUIRES_DRAIN"
            ):
                await agent_runtime.ensure_running("agent", db)
        self.adapter.start.assert_not_awaited()
        self.adapter.remove.assert_not_awaited()

    async def test_unhealthy_service_is_stopped_and_logs_retained(self):
        async with self.sessions() as db:
            with (
                patch(
                    "app.services.agent_runtime._health_check",
                    new=AsyncMock(return_value=False),
                ),
                self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_HEALTH_FAILED"),
            ):
                await agent_runtime.ensure_running("agent", db)
            self.assertEqual("error", (await db.get(AgentRuntime, "agent")).status)
        self.adapter.stop.assert_awaited_once()
        self.adapter.remove.assert_not_awaited()
        self.assertEqual(
            1, len(list((self.root / "managed-runtime-artifacts").glob("*/*")))
        )

    async def test_service_stop_failure_preserves_record_and_object(self):
        self.adapter.stop.side_effect = TimeoutError()
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
            with self.assertRaises(TimeoutError):
                await agent_runtime.stop_agent("agent", db)
            self.assertIsNotNone(await db.get(AgentRuntime, "agent"))
        self.adapter.remove.assert_not_awaited()

    async def test_task_cancel_revokes_before_stop_and_retains_before_remove(self):
        await self.task()
        order = []

        async def revoke(*_):
            order.append("revoke")

        async def stop(*_, **__):
            order.append("stop")
            return observation("exited", False)

        async def logs(*_, **__):
            order.append("logs")
            return LogSnapshot(b"output", b"")

        async def remove(*_, **__):
            order.append("remove")

        self.adapter.stop.side_effect = stop
        self.adapter.logs.side_effect = logs
        self.adapter.remove.side_effect = remove
        async with self.sessions() as db:
            with patch("app.services.e2ag_tool_gateway.revoke_task_grants", new=revoke):
                task = await a2a_service.cancel_task(db, "task")
                self.assertEqual("canceled", task.status)
        self.assertEqual(["revoke", "stop", "logs", "remove"], order)

    async def test_uncertain_stop_cannot_claim_cancellation_or_delete_config(self):
        await self.task()
        config = self.root / "agent/agent-task-task.json"
        config.write_text("retained")
        self.adapter.stop.side_effect = TimeoutError()
        async with self.sessions() as db:
            with self.assertRaises(TimeoutError):
                await a2a_service.cancel_task(db, "task")
            self.assertEqual("working", (await db.get(A2ATask, "task")).status)
        self.assertTrue(config.exists())
        self.adapter.remove.assert_not_awaited()

    async def test_submitted_task_cancels_without_contacting_runtime(self):
        await self.task("submitted")
        async with self.sessions() as db:
            self.assertEqual(
                "canceled", (await a2a_service.cancel_task(db, "task")).status
            )
        self.adapter.stop.assert_not_awaited()

    async def test_shared_service_request_cannot_claim_unsupported_cancellation(self):
        await self.task(mode="service")
        async with self.sessions() as db:
            with self.assertRaisesRegex(
                RuntimeErrorCode, "TASK_CANCELLATION_UNSUPPORTED"
            ):
                await a2a_service.cancel_task(db, "task")
            self.assertEqual("working", (await db.get(A2ATask, "task")).status)
        self.adapter.stop.assert_not_awaited()

    async def test_lifecycle_guard_prevents_cancel_during_launch(self):
        await self.task()
        async with (
            managed_runtime.exclusive(managed_runtime.handle("task", "task")),
            self.sessions() as db,
        ):
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RUNTIME_OPERATION_IN_PROGRESS"
            ):
                await a2a_service.cancel_task(db, "task")
        self.adapter.stop.assert_not_awaited()

    async def test_exit_zero_without_report_is_not_success(self):
        await self.task()
        await a2a_service._finish_task_runtime(
            "task", "task", str(self.root / "agent"), 0, "output"
        )
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual("failed", task.status)
            self.assertEqual("RUNTIME_RESULT_MISSING", task.error)

    async def test_confirmed_exit_with_report_keeps_result_and_completes(self):
        await self.task()
        report = self.root / "agent/output/events/task/task_result.md"
        report.parent.mkdir(parents=True)
        report.write_text("# 结果\n\n已完成，出处：fixture。", encoding="utf-8")
        await a2a_service._finish_task_runtime(
            "task", "task", str(self.root / "agent"), 0, "diagnostic"
        )
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual("completed", task.status)
            self.assertIn("出处", task.artifacts[0]["parts"][0]["text"])

    async def test_service_observation_failure_never_recreates_object(self):
        self.adapter.observe.side_effect = TimeoutError()
        async with self.sessions() as db:
            with self.assertRaises(TimeoutError):
                await agent_runtime.ensure_running("agent", db)
        self.adapter.start.assert_not_awaited()
        self.adapter.remove.assert_not_awaited()

    async def test_cleanup_failure_retries_without_reexecuting_task(self):
        await self.task()
        self.adapter.observe.return_value = observation("exited", False)
        self.adapter.remove.side_effect = [TimeoutError(), None]
        await asyncio.wait_for(
            a2a_service._poll_task_container(
                "task", "task", "handle", str(self.root / "agent"), poll_interval=0.01
            ),
            3,
        )
        self.assertEqual(2, self.adapter.remove.await_count)
        self.adapter.start.assert_not_awaited()
        async with self.sessions() as db:
            self.assertEqual("failed", (await db.get(A2ATask, "task")).status)

    async def test_observation_timeout_retries_without_restarting_or_stopping(self):
        await self.task()
        self.adapter.observe.side_effect = [
            TimeoutError(),
            observation("exited", False),
            observation("exited", False),
        ]
        await asyncio.wait_for(
            a2a_service._poll_task_container(
                "task", "task", "handle", str(self.root / "agent"), poll_interval=0.01
            ),
            3,
        )
        self.adapter.start.assert_not_awaited()
        self.adapter.stop.assert_not_awaited()

    async def test_missing_object_does_not_establish_execution_exit(self):
        await self.task()
        await a2a_service._poll_task_container(
            "task", "task", "handle", str(self.root / "agent"), poll_interval=0.01
        )
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual("working", task.status)
            self.assertEqual("RUNTIME_OBSERVATION_ABSENT", task.error)
        self.adapter.remove.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
