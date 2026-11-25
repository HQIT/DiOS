"""Deadline enforcement at E2AG, local/remote Workers and uncertain A2A cancellation."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime_a2a_outbound as a2a
import test_runtime_remote_control as remote
import test_runtime_work_authorizations as work
from app.runtime.capacity import Capacity
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.work_authorizations import require_current
from test_runtime_worker import FakeAdapter


class LiveAdapter(FakeAdapter):
    async def observe(self, handle):
        return self.observation(handle)


class EnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_controller_observation_cannot_extend_local_budget(self):
        f = work.WorkAuthorizationTests()
        await f.asyncSetUp()
        try:
            await Capacity(f.store).put(
                "deadline", {"kind": "scope"}, 3, 0, budget={"max_runtime_seconds": 2}
            )
            await f.submit()
            adapter = LiveAdapter()
            original = f.store.get

            async def stalled(run_id):
                if adapter.running:
                    await asyncio.sleep(10)
                return await original(run_id)

            began = asyncio.get_running_loop().time()
            with patch.object(f.store, "get", side_effect=stalled):
                final = await f.worker(adapter).once()
            self.assertLess(asyncio.get_running_loop().time() - began, 6)
            self.assertTrue(adapter.stopped)
            self.assertEqual(
                final["attempts"][0]["error"]["code"], "RUN_BUDGET_EXHAUSTED"
            )
        finally:
            await f.asyncTearDown()

    async def test_governed_worker_stops_and_retains_output_at_budget_deadline(self):
        f = work.WorkAuthorizationTests()
        await f.asyncSetUp()
        try:
            await Capacity(f.store).put(
                "deadline", {"kind": "scope"}, 3, 0, budget={"max_runtime_seconds": 2}
            )
            await f.submit()
            adapter = LiveAdapter()
            final = await f.worker(adapter).once()
            self.assertEqual(final["state"], "failed")
            self.assertEqual(
                final["attempts"][0]["error"]["code"], "RUN_BUDGET_EXHAUSTED"
            )
            self.assertTrue(adapter.stopped)
            self.assertEqual(
                (await Capacity(f.store).status())["items"][0]["reserved_attempts"], 0
            )
            self.assertTrue(final["execution_budget"]["exhausted"])
        finally:
            await f.asyncTearDown()

    async def test_execution_deadline_denies_effects_but_retains_result_validation(
        self,
    ):
        f = work.WorkAuthorizationTests()
        await f.asyncSetUp()
        try:
            await Capacity(f.store).put(
                "deadline", {"kind": "scope"}, 3, 0, budget={"max_runtime_seconds": 10}
            )
            run, attempt, _ = await f.claimed()
            deadline = (await f.store.get(run["id"]))["execution_budget"]["deadline_at"]
            with patch.object(Database, "now", AsyncMock(return_value=deadline + 1)):
                async with f.db.read(f.store.scope) as c:
                    with self.assertRaisesRegex(
                        RuntimeErrorCode, "RUN_BUDGET_EXHAUSTED"
                    ):
                        await require_current(c, run, attempt, deadline + 1, "execute")
                    await require_current(
                        c, run, attempt, deadline + 1, "accept_result"
                    )
        finally:
            await f.asyncTearDown()

    async def test_remote_worker_uses_server_budget_without_shared_database(self):
        f = remote.RemoteControlTests()
        await f.asyncSetUp()
        try:
            await Capacity(f.f.store).put(
                "deadline", {"kind": "scope"}, 3, 0, budget={"max_runtime_seconds": 3}
            )
            adapter = LiveAdapter()
            adapter.name = "local-docker-v1"
            f.worker.adapter = adapter
            final = await f.worker.once()
            self.assertEqual(final["state"], "failed")
            self.assertEqual(
                final["attempts"][0]["error"]["code"], "RUN_BUDGET_EXHAUSTED"
            )
            self.assertTrue(adapter.stopped)
            self.assertFalse(hasattr(f.remote, "db"))
        finally:
            await f.f.asyncTearDown()

    async def test_a2a_budget_cancels_original_task_and_keeps_unknown_capacity(self):
        f = a2a.OutboundTests()
        await f.asyncSetUp()
        try:
            f.auto_complete = False
            f.cancel_pending = True
            capacity = Capacity(f.console.store)
            await capacity.put(
                "deadline", {"kind": "scope"}, 3, 0, budget={"max_runtime_seconds": 2}
            )
            run = await f.submit()
            worker = f.worker()
            worker.timeout = 3
            final = await worker.once()
            self.assertEqual(final["state"], "reconciliation_required")
            self.assertEqual(
                final["attempts"][0]["remote"]["a2a"]["cancel_reason"],
                "RUN_BUDGET_EXHAUSTED",
            )
            self.assertEqual(
                (await capacity.status())["items"][0]["reserved_attempts"], 1
            )
            self.assertEqual(f.submit_count, 1)
            f.cancel_pending = False
            # Explicit observation of the same Task, never a new submit.
            for _ in range(80):
                final = await worker.tick(run["id"], reconcile=True)
                if final["state"] in {"failed", "cancelled"}:
                    break
                await asyncio.sleep(0.1)
            self.assertEqual(final["state"], "failed")
            self.assertEqual(
                final["attempts"][0]["error"]["code"], "RUN_BUDGET_EXHAUSTED"
            )
            self.assertEqual(
                final["attempts"][0]["remote"]["a2a"]["observed_state"], "canceled"
            )
            self.assertEqual(f.submit_count, 1)
            self.assertEqual(
                (await capacity.status())["items"][0]["reserved_attempts"], 0
            )
        finally:
            await f.asyncTearDown()


if __name__ == "__main__":
    unittest.main()
