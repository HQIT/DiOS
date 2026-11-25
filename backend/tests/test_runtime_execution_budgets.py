"""Rolling admission and immutable execution deadlines on both runtime providers."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

from app.runtime.capacity import Capacity, queue_status
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.execution_budgets import validate
from app.runtime.store import RuntimeStore
from test_runtime_capacity import CapacityScenarios


class BudgetScenarios:
    postgres = False
    asyncSetUp = CapacityScenarios.asyncSetUp
    asyncTearDown = CapacityScenarios.asyncTearDown
    submit = CapacityScenarios.submit
    worker = CapacityScenarios.worker

    async def budget(self, **value):
        return await self.capacity.put("shared", {"kind": "scope"}, 20, 0, budget=value)

    async def reclaim(self, run, attempt, worker):
        await self.store.mark_unknown(
            (run["id"], attempt["id"], *worker, attempt["epoch"]), "TEST_INTERRUPTION"
        )
        current = await self.store.get(run["id"])
        await self.store.reconcile_absent(
            run["id"], current["version"], "trusted-absence-proof"
        )
        current = await self.store.get(run["id"])
        await self.store.retry(run["id"], current["version"])

    async def test_window_is_atomic_across_connections_and_survives_restart(self):
        await self.budget(max_dispatches=3, dispatch_window_seconds=60)
        for _ in range(10):
            await self.submit()
        dbs = [Database(self.url) for _ in range(10)]
        try:
            stores = [RuntimeStore(db, self.scope) for db in dbs]
            workers = [await self.worker(store) for store in stores]
            claimed = await asyncio.gather(
                *(store.claim(*worker) for store, worker in zip(stores, workers))
            )
            self.assertEqual(sum(x is not None for x in claimed), 3)
            i = next(i for i, row in enumerate(claimed) if row)
            attempt = claimed[i]
            await self.store.reject_unlaunched(
                (attempt["run_id"], attempt["id"], *workers[i], attempt["epoch"]),
                "AUTH_DENIED",
            )
            # Failed preflight consumed admission too; no refund by failure/restart.
            self.assertIsNone(await self.store.claim(*await self.worker()))
            await self.db.close()
            self.db = Database(self.url)
            self.store = RuntimeStore(self.db, self.scope)
            self.capacity = Capacity(self.store)
            self.assertIsNone(await self.store.claim(*await self.worker()))
            rate = (await self.capacity.status())["items"][0]["dispatch_rate"]
            self.assertEqual(rate["used"], 3)
            with patch.object(
                Database, "now", AsyncMock(return_value=rate["available_at"] + 0.01)
            ):
                self.assertIsNotNone(await self.store.claim(*await self.worker()))
        finally:
            for db in dbs:
                await db.close()

    async def test_rate_block_does_not_block_unrelated_profile_or_leak_usage(self):
        await self.capacity.put(
            "account",
            {"kind": "profiles", "profiles": ["one"]},
            20,
            0,
            budget={"max_dispatches": 1, "dispatch_window_seconds": 60},
        )
        first = await self.submit()
        await self.store.claim(*await self.worker())
        blocked = await self.submit()
        other = await self.submit(profile="two")
        self.assertEqual(
            (await self.store.claim(*await self.worker()))["run_id"], other["id"]
        )
        async with self.db.read(self.scope) as c:
            value = await queue_status(
                c, self.scope, await self.store._run(c, blocked["id"])
            )
        self.assertEqual(value["reason"], "DISPATCH_RATE_LIMITED")
        self.assertEqual(value["limits"], [])
        self.assertNotIn(first["id"], str(value))
        self.assertGreater(value["available_at"], 0)

    async def test_retry_keeps_original_deadline_even_after_policy_relaxes(self):
        await self.budget(max_runtime_seconds=30)
        run = await self.submit()
        worker = await self.worker()
        attempt = await self.store.claim(*worker)
        before = (await self.store.get(run["id"]))["execution_budget"]
        await self.reclaim(run, attempt, worker)
        await self.capacity.put(
            "shared", {"kind": "scope"}, 20, 1, budget={"max_runtime_seconds": 300}
        )
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db, self.scope)
        second = await self.store.claim(*await self.worker())
        self.assertEqual(second["run_id"], run["id"])
        self.assertEqual(
            (await self.store.get(run["id"]))["execution_budget"]["deadline_at"],
            before["deadline_at"],
        )

    async def test_expired_retry_fails_without_creating_an_attempt(self):
        await self.budget(max_runtime_seconds=10)
        run = await self.submit()
        worker = await self.worker()
        attempt = await self.store.claim(*worker)
        deadline = (await self.store.get(run["id"]))["execution_budget"]["deadline_at"]
        await self.reclaim(run, attempt, worker)
        with patch.object(Database, "now", AsyncMock(return_value=deadline + 1)):
            self.assertIsNone(await self.store.claim(*await self.worker()))
        final = await self.store.get(run["id"])
        self.assertEqual(final["state"], "failed")
        self.assertEqual(len(final["attempts"]), 1)

    async def test_expired_execution_cannot_heartbeat_but_keeps_capacity(self):
        await self.budget(max_runtime_seconds=10)
        run = await self.submit()
        worker = await self.worker()
        attempt = await self.store.claim(*worker)
        deadline = (await self.store.get(run["id"]))["execution_budget"]["deadline_at"]
        with patch.object(Database, "now", AsyncMock(return_value=deadline + 1)):
            with self.assertRaisesRegex(RuntimeErrorCode, "RUN_BUDGET_EXHAUSTED"):
                await self.store.heartbeat(
                    run["id"], attempt["id"], *worker, attempt["epoch"]
                )
            self.assertEqual(
                (await self.capacity.status())["items"][0]["reserved_attempts"], 1
            )

    async def test_omitted_budget_preserves_and_empty_budget_clears_only_future_bindings(
        self,
    ):
        await self.budget(
            max_runtime_seconds=60, max_dispatches=5, dispatch_window_seconds=60
        )
        run = await self.submit()
        await self.store.claim(*await self.worker())
        before = (await self.store.get(run["id"]))["execution_budget"]
        await self.capacity.put("shared", {"kind": "scope"}, 19, 1)
        self.assertEqual(
            (await self.capacity.status())["items"][0]["budget"]["max_runtime_seconds"],
            60,
        )
        await self.capacity.put("shared", {"kind": "scope"}, 19, 2, budget={})
        self.assertIsNone(
            (await self.capacity.status())["items"][0]["budget"]["max_runtime_seconds"]
        )
        self.assertEqual(
            (await self.store.get(run["id"]))["execution_budget"]["deadline_at"],
            before["deadline_at"],
        )
        other = await self.submit()
        await self.store.claim(*await self.worker())
        self.assertIsNone((await self.store.get(other["id"]))["execution_budget"])

    async def test_invalid_budget_cannot_partially_update_limit(self):
        for value in (
            {"max_runtime_seconds": True},
            {"max_runtime_seconds": 0},
            {"max_dispatches": 3},
            {"max_dispatches": 0, "dispatch_window_seconds": 60},
            {"unknown": 2},
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_EXECUTION_BUDGET"):
                await self.capacity.put(
                    "shared", {"kind": "scope"}, 20, 0, budget=value
                )
        self.assertEqual((await self.capacity.status())["items"], [])
        self.assertEqual(
            validate({}),
            {
                "max_dispatches": None,
                "max_runtime_seconds": None,
                "dispatch_window_seconds": None,
            },
        )


class SQLiteBudgetTests(BudgetScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_backup_preserves_policy_and_pinned_deadline(self):
        from app.runtime.recovery import backup_sqlite, verify_backup

        await self.budget(
            max_runtime_seconds=60, max_dispatches=2, dispatch_window_seconds=30
        )
        run = await self.submit()
        await self.store.claim(*await self.worker())
        before = (await self.store.get(run["id"]))["execution_budget"]
        objects = self.root / "objects"
        objects.mkdir()
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, objects, bundle)
        manifest = await asyncio.to_thread(verify_backup, bundle)
        self.assertIn("runtime_execution_budget_schema", manifest["schemas"])
        db = Database("sqlite+aiosqlite:///" + str(bundle / "state.sqlite"))
        try:
            store = RuntimeStore(db, self.scope)
            self.assertEqual(
                (await store.get(run["id"]))["execution_budget"]["deadline_at"],
                before["deadline_at"],
            )
            self.assertEqual(
                (await Capacity(store).status())["items"][0]["budget"][
                    "max_dispatches"
                ],
                2,
            )
        finally:
            await db.close()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgreSQLBudgetTests(BudgetScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


if __name__ == "__main__":
    unittest.main()
