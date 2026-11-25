"""Liveness is independent of task activity, scheduler ownership and UI polling."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime_capacity as fixtures
import test_runtime_principals as principals
from app.runtime.database import RuntimeErrorCode
from app.runtime.monitor import LoopObservation, prometheus, snapshot
from app.runtime.monitor_schema import loops
from app.runtime.recovery import backup_sqlite, verify_backup
from app.services.run_schedule_worker import ScheduleLoop


class MonitorScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def monitor(self):
        return (await snapshot(self.db, self.scope))["services"][0]

    async def test_missing_failed_recovered_and_restart_history(self):
        self.assertEqual((await self.monitor())["state"], "unobserved")
        old = LoopObservation(self.db, self.scope, "run_scheduler")
        with self.assertRaises(ValueError):
            await old.cycle(AsyncMock(side_effect=ValueError("private credential")))
        row = await self.monitor()
        self.assertEqual(row["state"], "degraded")
        self.assertEqual(row["latest"]["error"], "SCHEDULER_ERROR")
        self.assertIsNone(row["latest"]["succeeded_at"])
        await old.cycle(AsyncMock(return_value={"schedules_observed": 0}))
        self.assertEqual((await self.monitor())["state"], "healthy")
        await old.record("stopped")
        self.assertEqual((await self.monitor())["state"], "stopped")
        newer = LoopObservation(self.db, self.scope, "run_scheduler")
        await newer.cycle(AsyncMock())
        row = await self.monitor()
        self.assertEqual(row["state"], "healthy")
        self.assertEqual(len(row["instances"]), 2)
        self.assertEqual(row["fresh_instances"], 1)
        self.assertFalse((await self.db.migrate())["changed"])

    async def test_long_tick_keeps_heartbeat_and_never_restarts(self):
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def operation():
            nonlocal count
            count += 1
            started.set()
            await release.wait()

        observer = LoopObservation(self.db, self.scope, "run_scheduler", interval=0.02)
        task = asyncio.create_task(observer.cycle(operation))
        try:
            await asyncio.wait_for(started.wait(), 5)
            async with self.db.transaction(self.scope) as c:
                now = await self.db.now(c)
            observer.cycle_started_at = now - 125
            await observer.record("running")
            first = (await self.monitor())["latest"]["heartbeat_at"]
            for _ in range(100):
                row = await self.monitor()
                if row["latest"]["heartbeat_at"] > first:
                    break
                await asyncio.sleep(0.01)
            self.assertGreater(row["latest"]["heartbeat_at"], first)
            self.assertEqual(row["state"], "slow")
            self.assertEqual(count, 1)
            self.assertFalse(task.done())
            release.set()
            await task
            self.assertEqual((await self.monitor())["state"], "healthy")
        finally:
            release.set()
            await task

    async def test_expired_clock_future_and_disabled_not_healthy(self):
        observer = LoopObservation(self.db, self.scope, "run_scheduler")
        await observer.cycle(AsyncMock())
        async with self.db.transaction(self.scope) as c:
            now = await self.db.now(c)
            await c.execute(loops.update().values(heartbeat_at=now - 25))
        self.assertEqual((await self.monitor())["state"], "stale")
        async with self.db.transaction(self.scope) as c:
            await c.execute(loops.update().values(heartbeat_at=now + 60))
        self.assertEqual((await self.monitor())["state"], "stale")
        await observer.record("disabled")
        self.assertEqual((await self.monitor())["state"], "disabled")

    async def test_observation_failure_does_not_repeat_operation_or_fake_success(self):
        observer = LoopObservation(self.db, self.scope, "run_scheduler")
        operation = AsyncMock(return_value="accepted")
        with patch(
            "app.runtime.monitor.require",
            side_effect=RuntimeErrorCode("MONITOR_MIGRATION_REQUIRED"),
        ):
            self.assertEqual(await observer.cycle(operation), "accepted")
        self.assertEqual(operation.await_count, 1)
        self.assertEqual((await self.monitor())["state"], "unobserved")
        await observer.record("idle")
        self.assertEqual((await self.monitor())["state"], "unobserved")

    async def test_cancellation_not_successful_cycle_and_scope_isolation(self):
        observer = LoopObservation(self.db, self.scope, "run_scheduler")
        with self.assertRaises(asyncio.CancelledError):
            await observer.cycle(AsyncMock(side_effect=asyncio.CancelledError()))
        row = await self.monitor()
        self.assertIsNone(row["latest"]["succeeded_at"])
        self.assertEqual(row["state"], "degraded")
        self.assertTrue(
            all(
                s["state"] == "unobserved"
                for s in (await snapshot(self.db, "other"))["services"]
            )
        )
        metrics = prometheus(await snapshot(self.db, self.scope))
        self.assertIn('service="run_scheduler",state="degraded"} 1', metrics)
        self.assertNotIn(observer.instance, metrics)
        self.assertNotIn("SCHEDULER_CANCELLED", metrics)

    async def test_healthy_instance_cannot_hide_fresh_failed_peer(self):
        a = LoopObservation(self.db, self.scope, "run_scheduler")
        b = LoopObservation(self.db, self.scope, "run_scheduler")
        await a.cycle(AsyncMock())
        with self.assertRaises(RuntimeErrorCode):
            await b.cycle(
                AsyncMock(side_effect=RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE"))
            )
        self.assertEqual((await self.monitor())["state"], "degraded")
        await a.record("idle")
        self.assertEqual(
            (await self.monitor())["evidence"]["error"], "AUTH_PROVIDER_UNAVAILABLE"
        )
        await b.record("stopped")
        self.assertEqual((await self.monitor())["state"], "healthy")
        if not self.postgres:
            (self.root / "objects").mkdir(exist_ok=True)
            backup_sqlite(
                self.url, str(self.root / "objects"), str(self.root / "monitor-backup")
            )
            self.assertIn(
                "runtime_monitor_schema",
                verify_backup(str(self.root / "monitor-backup"))["schemas"],
            )


class SQLiteMonitorTests(MonitorScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL test URL not set"
)
class PostgresMonitorTests(MonitorScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class MonitorApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = principals.PrincipalTests.asyncSetUp
    asyncTearDown = principals.PrincipalTests.asyncTearDown
    save = principals.PrincipalTests.save
    headers_for = principals.PrincipalTests.headers_for

    async def test_operator_only_health_and_metrics(self):
        for path in ("scheduler-health", "scheduler-metrics"):
            response = await self.client.get(
                "/api/os/runtime/" + path, headers=self.headers_for("a")
            )
            self.assertEqual(response.status_code, 403)
            response = await self.client.get(
                "/api/os/runtime/" + path, headers=self.headers
            )
            self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])

    async def test_disabled_loop_does_not_schedule_and_stops_cleanly(self):
        loop = ScheduleLoop(self.console, enabled=False)
        loop.service.tick = AsyncMock()
        loop.start()
        for _ in range(100):
            value = await snapshot(self.db, self.console.scope)
            if value["services"][0]["state"] == "disabled":
                break
            await asyncio.sleep(0.01)
        await loop.stop()
        self.assertEqual(loop.service.tick.await_count, 0)
        value = await snapshot(self.db, self.console.scope)
        self.assertEqual(value["services"][0]["state"], "stopped")
