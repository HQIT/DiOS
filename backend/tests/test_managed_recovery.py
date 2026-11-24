import asyncio
import json
import os
import sys
import unittest

from app.config import settings
from app.models.tables import A2ATask, Agent, ManagedTaskExecution
from app.runtime.database import RuntimeErrorCode
from app.services import a2a_service, managed_recovery, managed_runtime
from tests import test_managed_runtime as fixtures


class ManagedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    task = fixtures.ManagedRuntimeTests.task

    async def intent(self):
        await self.task()
        async with self.sessions() as db:
            row = await managed_recovery.register(
                db,
                "task",
                managed_runtime.handle("task", "task"),
                str(self.root / "agent"),
                "task",
            )
            await db.commit()
        return row

    def report(self):
        path = self.root / "agent/output/events/task/task_result.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Result\n\nEvidence: fixture:source", encoding="utf-8")

    async def test_new_controller_recovers_original_result_without_launch(self):
        await self.intent()
        self.report()
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        self.assertEqual(
            await managed_recovery.ManagedRecovery().tick(), {"task": "finished"}
        )
        async with self.sessions() as db:
            self.assertEqual((await db.get(A2ATask, "task")).status, "completed")
            self.assertTrue((await db.get(ManagedTaskExecution, "task")).finished)
        self.assertEqual(await managed_recovery.ManagedRecovery().tick(), {})
        self.adapter.start.assert_not_awaited()
        self.adapter.remove.assert_awaited_once()

    async def test_cleanup_failure_survives_controller_restart(self):
        await self.intent()
        self.report()
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        self.adapter.remove.side_effect = [TimeoutError(), None]
        self.assertEqual(
            (await managed_recovery.ManagedRecovery().tick())["task"], "error"
        )
        async with self.sessions() as db:
            self.assertEqual((await db.get(A2ATask, "task")).status, "completed")
            self.assertFalse((await db.get(ManagedTaskExecution, "task")).finished)
        self.assertEqual(
            (await managed_recovery.ManagedRecovery().tick())["task"], "finished"
        )
        self.adapter.start.assert_not_awaited()
        self.assertEqual(self.adapter.logs.await_count, 1)

    async def test_persisted_cancel_wins_over_complete_report_after_restart(self):
        await self.intent()
        self.report()
        self.adapter.stop.side_effect = [
            TimeoutError(),
            fixtures.observation("exited", False),
        ]
        async with self.sessions() as db:
            with self.assertRaises(TimeoutError):
                await a2a_service.cancel_task(db, "task")
        async with self.sessions() as db:
            self.assertTrue(
                (await db.get(ManagedTaskExecution, "task")).cancel_requested
            )
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        self.assertEqual(
            (await managed_recovery.ManagedRecovery().tick())["task"], "finished"
        )
        async with self.sessions() as db:
            self.assertEqual((await db.get(A2ATask, "task")).status, "canceled")
        self.adapter.start.assert_not_awaited()

    async def test_configuration_edits_do_not_move_original_result_location(self):
        await self.intent()
        self.report()
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.workspace_path = str(self.root / "different")
            agent.mode = "service"
            await db.commit()
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        self.assertEqual(await managed_recovery.reconcile("task"), "finished")
        async with self.sessions() as db:
            self.assertEqual((await db.get(A2ATask, "task")).status, "completed")

    async def test_missing_object_remains_unresolved_and_never_relaunches(self):
        await self.intent()
        self.assertEqual(await managed_recovery.reconcile("task"), "absent")
        snapshot = await managed_recovery.status()
        self.assertEqual(snapshot["unresolved_executions"], 1)
        self.assertFalse(snapshot["executions_settled"])
        self.assertEqual(snapshot["items"][0]["error"], "RUNTIME_OBSERVATION_ABSENT")
        self.assertTrue(snapshot["items"][0]["observed_at"].endswith("+00:00"))
        self.assertNotIn("workspace_path", snapshot["items"][0])
        self.adapter.start.assert_not_awaited()
        self.adapter.remove.assert_not_awaited()

    async def test_late_exit_result_clears_earlier_observation_error(self):
        await self.intent()
        self.assertEqual(await managed_recovery.reconcile("task"), "absent")
        self.report()
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        self.assertEqual(await managed_recovery.reconcile("task"), "finished")
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.error, "")

    async def test_changed_process_cannot_be_stopped_or_removed(self):
        await self.intent()
        async with self.sessions() as db:
            row = await db.get(ManagedTaskExecution, "task")
            row.process_instance = "original"
            row.cancel_requested = True
            await db.commit()
        self.adapter.observe.return_value = fixtures.observation()
        self.assertEqual(await managed_recovery.reconcile("task"), "error")
        async with self.sessions() as db:
            self.assertEqual(
                (await db.get(ManagedTaskExecution, "task")).error, "PROCESS_CHANGED"
            )
        self.adapter.stop.assert_not_awaited()
        self.adapter.remove.assert_not_awaited()

    async def test_overlapping_controllers_do_not_finalize_twice(self):
        await self.intent()
        self.adapter.observe.return_value = fixtures.observation("exited", False)
        outcomes = await asyncio.gather(
            managed_recovery.reconcile("task"), managed_recovery.reconcile("task")
        )
        self.assertIn("finished", outcomes)
        self.adapter.logs.assert_awaited_once()
        self.adapter.remove.assert_awaited_once()

    async def test_controller_shutdown_drains_observation_without_stopping_agent(self):
        await self.intent()
        entered, released = asyncio.Event(), asyncio.Event()

        async def observe(*_):
            entered.set()
            await released.wait()
            return fixtures.observation()

        self.adapter.observe.side_effect = observe
        controller = managed_recovery.ManagedRecovery(interval=0.01)
        controller.start()
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping = asyncio.create_task(controller.stop())
            await asyncio.sleep(0.03)
            self.assertFalse(stopping.done())
            released.set()
            await asyncio.wait_for(stopping, 3)
        finally:
            released.set()
            await controller.stop()
        self.adapter.stop.assert_not_awaited()

    async def test_preledger_and_orphaned_tasks_block_settled_status(self):
        await self.task()
        async with self.sessions() as db:
            await db.delete(await db.get(Agent, "agent"))
            await db.commit()
        snapshot = await managed_recovery.status()
        self.assertEqual(snapshot["untracked_active_tasks"], 1)
        self.assertFalse(snapshot["executions_settled"])

    async def test_pagination_does_not_starve_later_tasks(self):
        await self.intent()
        async with self.sessions() as db:
            db.add(
                A2ATask(
                    id="task2",
                    agent_id="agent",
                    status="working",
                    message={},
                    artifacts=[],
                )
            )
            await managed_recovery.register(
                db,
                "task2",
                managed_runtime.handle("task", "task2"),
                str(self.root / "agent"),
                "task2",
            )
            await db.commit()
        controller = managed_recovery.ManagedRecovery(batch_size=1)
        self.assertEqual(set(await controller.tick()), {"task"})
        self.assertEqual(set(await controller.tick()), {"task2"})
        self.assertEqual(await controller.tick(), {})
        self.assertEqual(set(await controller.tick()), {"task"})

    async def test_registration_cannot_replace_original_identity(self):
        await self.intent()
        async with self.sessions() as db:
            with self.assertRaisesRegex(RuntimeErrorCode, "MANAGED_EXECUTION_CONFLICT"):
                await managed_recovery.register(
                    db, "task", "different", str(self.root), "task"
                )

    async def test_other_scope_is_not_observed(self):
        await self.intent()
        async with self.sessions() as db:
            row = await db.get(ManagedTaskExecution, "task")
            row.scope = settings.runtime_scope + "-other"
            await db.commit()
        self.assertEqual(await managed_recovery.ManagedRecovery().tick(), {})
        self.adapter.observe.assert_not_awaited()

    async def test_cli_reads_same_durable_pending_identity(self):
        await self.intent()
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.services.managed_recovery",
            "status",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 15)
        self.assertEqual(process.returncode, 0, err.decode())
        snapshot = json.loads(out)
        self.assertEqual(snapshot["pending_executions"], 1)
        self.assertEqual(snapshot["items"][0]["task_id"], "task")
