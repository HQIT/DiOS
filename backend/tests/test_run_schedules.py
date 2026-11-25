import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_runtime_task_console as fixtures
from app.api.os import run_schedules as api
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.lifecycle import Lifecycle
from app.runtime.schedule_schema import occurrences
from app.runtime.schema import runs
from app.runtime.work_auth_schema import work
from app.runtime.worker import read_profile
from app.services.run_schedules import RunSchedules
from app.services.task_console import TaskConsole
from sqlalchemy import func, select
from work_auth_fixtures import policy_fixture


class RunScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TaskConsoleTests.asyncSetUp(self)
        self.profile["governance"] = {"mode": "e2ag"}
        path = self.profiles / "review.json"
        path.write_text(json.dumps(self.profile))
        _, self.spec = read_profile(path)
        self.policy, self.descriptor = policy_fixture(self.root, "review", self.spec)
        self.console.policy = self.policy
        self.manager = RunSchedules(self.console)
        self.app.include_router(api.router, prefix="/api/os")
        self.template = {
            "profile_id": "review",
            "sample_id": "first-item",
            "input_payload": None,
            "goal": "review boundary",
        }
        self.now = time.time()
        self.clock = patch.object(
            self.db, "now", AsyncMock(side_effect=lambda c: self.now)
        )
        self.clock.start()

    async def asyncTearDown(self):
        self.clock.stop()
        await fixtures.TaskConsoleTests.asyncTearDown(self)

    async def create(self, key="timer"):
        return await self.manager.create(self.template, "* * * * *", key)

    async def due(self, row):
        self.now = row["next_due"] + 0.1
        await self.manager.tick()
        return (await self.manager.list())["items"][0]

    async def count(self, table):
        async with self.db.read("one") as c:
            return (
                await c.execute(select(func.count()).select_from(table))
            ).scalar_one()

    async def test_create_idempotent_and_no_work_until_due(self):
        row = await self.create()
        self.assertEqual(row["id"], (await self.create())["id"])
        self.assertEqual(0, await self.count(runs))
        self.assertEqual(0, await self.count(work))
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.manager.create(
                {**self.template, "goal": "changed"}, "* * * * *", "timer"
            )
        await self.manager.tick()
        self.assertEqual(0, await self.count(runs))
        row = await self.due(row)
        self.assertEqual("running", row["active"]["state"])
        self.assertEqual(1, await self.count(runs))
        self.assertEqual(1, await self.count(work))
        run = await self.console.detail(row["active"]["run_id"])
        self.assertEqual("queued", run["state"])
        self.assertEqual("service:test", run["authorization"]["actor"])

    async def test_multiple_instances_and_restart_preserve_one_run(self):
        row = await self.create()
        self.now = row["next_due"] + 0.1
        await asyncio.gather(self.manager.tick(), RunSchedules(self.console).tick())
        await RunSchedules(self.console).tick()
        self.assertEqual(1, await self.count(runs))
        self.assertEqual(1, await self.count(work))
        self.assertEqual(1, await self.count(occurrences))

    async def test_commit_before_observation_recovers_receipt_without_new_work(self):
        row = await self.create()
        self.now = row["next_due"] + 0.1
        claim = await self.manager.claim(row["id"])
        item = await self.manager.plan(claim)
        original = self.console.submit

        async def crash(**kwargs):
            await original(**kwargs)
            raise asyncio.CancelledError()

        with (
            patch.object(self.console, "submit", side_effect=crash),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.manager.deliver(claim, item)
        self.assertEqual(1, await self.count(runs))
        # Simulate owner death and lease expiry, then a different scheduler.
        self.now += 31
        self.policy.path.unlink()
        await RunSchedules(self.console).tick()
        row = (await self.manager.list())["items"][0]
        self.assertIsNotNone(row["active"]["run_id"])
        self.assertEqual(1, await self.count(work))

    async def test_expired_owner_is_fenced_at_actual_submission(self):
        row = await self.create()
        self.now = row["next_due"] + 0.1
        claim = await self.manager.claim(row["id"])
        item = await self.manager.plan(claim)
        self.now += 31
        other = RunSchedules(self.console)
        replacement = await other.claim(row["id"])
        self.assertGreater(replacement["lease_epoch"], claim["lease_epoch"])
        await self.manager.deliver(claim, item)
        self.assertEqual(0, await self.count(runs))
        await other.deliver(replacement, await other.plan(replacement))
        self.assertEqual(1, await self.count(runs))

    async def test_pause_between_authorization_and_commit_blocks_enqueue(self):
        row = await self.create()
        self.now = row["next_due"] + 0.1
        original = self.console.store.submit

        async def racing(*args, **kwargs):
            await self.manager.control(
                row["id"], row["version"], False, "operator", "pause during admission"
            )
            return await original(*args, **kwargs)

        with patch.object(self.console.store, "submit", side_effect=racing):
            await self.manager.tick()
        self.assertEqual(0, await self.count(runs))
        self.assertEqual(0, await self.count(work))
        current = (await self.manager.list())["items"][0]
        self.assertEqual(0, current["active"]["failures"])
        await self.manager.control(
            row["id"], current["version"], True, "operator", "resume"
        )
        await self.manager.tick()
        self.assertEqual(1, await self.count(runs))

    async def test_policy_change_requires_explicit_reauthorization(self):
        row = await self.create()
        data = json.loads(self.policy.path.read_text())
        data["actor"] = "service:new"
        self.policy.path.write_text(json.dumps(data))
        row = await self.due(row)
        self.assertEqual("dead_letter", row["active"]["state"])
        self.assertEqual("SCHEDULE_BINDING_CHANGED", row["active"]["error"])
        self.assertEqual(0, await self.count(work))
        refreshed = await self.manager.reauthorize(
            row["id"], row["version"], "operator", "approved new service identity"
        )
        self.assertIsNone(refreshed["active"])
        self.assertEqual("service:new", refreshed["binding"]["actor"])
        row = await self.due(refreshed)
        self.assertEqual(
            "service:new",
            (await self.console.detail(row["active"]["run_id"]))["authorization"][
                "actor"
            ],
        )

    async def test_authorization_revocation_prevents_new_work(self):
        row = await self.create()
        data = json.loads(self.policy.path.read_text())
        data["profiles"]["review"]["permissions"]["service"] = []
        self.policy.path.write_text(json.dumps(data))
        row = await self.due(row)
        self.assertEqual("dead_letter", row["active"]["state"])
        self.assertEqual(0, await self.count(work))

    async def test_unknown_and_queued_runs_coalesce_without_replacement(self):
        row = await self.due(await self.create())
        run_id = row["active"]["run_id"]
        store = self.console.store
        await store.register_worker("worker", "instance", "host", 123)
        await store.claim("worker", "instance", lease_seconds=1)
        self.now += 180
        await store.expire()
        await self.manager.tick()
        current = (await self.manager.list())["items"][0]
        self.assertEqual(run_id, current["active"]["run_id"])
        self.assertEqual(3, current["buffered"]["trigger_count"])
        self.assertEqual(1, await self.count(runs))

    async def test_failed_original_is_not_retried_and_discard_does_not_revive(self):
        row = await self.due(await self.create())
        run = await self.console.store.get(row["active"]["run_id"])
        await self.console.store.cancel(run["id"], run["version"])
        self.now += 60
        await self.manager.tick()
        row = (await self.manager.list())["items"][0]
        item = row["active"]
        self.assertEqual("dead_letter", item["state"])
        with self.assertRaisesRegex(RuntimeErrorCode, "RECONCILIATION"):
            await self.manager.resolve(
                row["id"], item["id"], "retry", "operator", "retry", "r1"
            )
        await self.manager.resolve(
            row["id"], item["id"], "discard", "operator", "reviewed cancellation", "d1"
        )
        await self.manager.resolve(
            row["id"], item["id"], "discard", "operator", "reviewed cancellation", "d1"
        )
        await self.manager.tick()
        self.assertEqual(2, await self.count(runs))
        old = next(
            x
            for x in (await self.manager.history(row["id"]))["items"]
            if x["id"] == item["id"]
        )
        self.assertEqual("discarded", old["state"])

    async def test_drain_defers_work_and_resumes_same_occurrence(self):
        row = await self.create()
        lifecycle = Lifecycle(self.db, "one")
        state = await lifecycle.begin_drain()
        row = await self.due(row)
        item_id = row["active"]["id"]
        self.assertEqual("pending", row["active"]["state"])
        self.assertEqual(0, await self.count(work))
        await lifecycle.resume(state["revision"])
        await self.manager.tick()
        row = (await self.manager.list())["items"][0]
        self.assertEqual(item_id, row["active"]["id"])
        self.assertEqual(1, await self.count(work))

    async def test_catchup_bounded_and_scope_isolation(self):
        row = await self.create()
        self.now += 86400 * 100
        await self.manager.tick()
        history = (await self.manager.history(row["id"]))["items"]
        self.assertEqual({"expired", "running"}, {x["state"] for x in history})
        self.assertEqual(
            1440, next(x for x in history if x["state"] == "running")["trigger_count"]
        )
        other = RunSchedules(
            TaskConsole(
                self.db, "other", self.profiles, self.root / "other", policy=self.policy
            )
        )
        self.assertEqual([], (await other.list())["items"])
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await other.control(row["id"], 1, False, "operator", "cross scope")

    async def test_api_operator_gate_strict_payload_and_cli_contract(self):
        path = "/api/os/runtime/run-schedules"
        self.assertEqual(401, (await self.client.get(path)).status_code)
        body = self.template | {
            "idempotency_key": "api",
            "cron_expression": "* * * * *",
        }
        response = await self.client.post(path, headers=self.headers, json=body)
        self.assertEqual(201, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["cache-control"])
        # Per-request identity delegation is separate from operator schedule templates.
        for reference in (None, "dl-user-supplied"):
            rejected = await self.client.post(path, headers=self.headers,
                json=body | {"delegation_ref": reference})
            self.assertEqual(422, rejected.status_code)
        self.assertEqual(
            422,
            (
                await self.client.post(
                    path, headers=self.headers, json=body | {"permissions": []}
                )
            ).status_code,
        )
        row = response.json()
        response = await self.client.post(
            path + "/" + row["id"] + "/control",
            headers=self.headers,
            json={
                "expected_version": 1,
                "enabled": False,
                "actor": "operator",
                "reason": "test",
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        response = await self.client.post(
            path + "/" + row["id"] + "/control",
            headers=self.headers,
            json={
                "expected_version": 1,
                "enabled": True,
                "actor": "operator",
                "reason": "stale",
            },
        )
        self.assertEqual(409, response.status_code)

    async def test_background_loop_submits_without_manual_tick(self):
        from app.runtime.schedule_schema import schedules
        from app.services.run_schedule_worker import ScheduleLoop

        self.clock.stop()
        row = await self.create()
        async with self.db.transaction("one") as c:
            await c.execute(
                schedules.update()
                .where(schedules.c.id == row["id"])
                .values(next_due=time.time() + 0.1)
            )
        loop = ScheduleLoop(self.console)
        loop.start()
        try:
            async with asyncio.timeout(8):
                while not await self.count(runs):
                    await asyncio.sleep(0.1)
            self.assertEqual(await self.count(runs), 1)
        finally:
            await loop.stop()

    async def test_reauthorization_never_revives_a_discarded_failed_run(self):
        row = await self.due(await self.create())
        original = await self.console.detail(row["active"]["run_id"])
        await self.console.store.cancel(original["id"], original["version"])
        await self.manager.tick()
        row = (await self.manager.list())["items"][0]
        await self.manager.resolve(
            row["id"],
            row["active"]["id"],
            "discard",
            "operator",
            "skip failed run",
            "discard-once",
        )
        updated = await self.manager.reauthorize(
            row["id"], row["version"], "operator", "future policy confirmation"
        )
        self.assertEqual(updated["active"]["state"], "discarded")
        await self.manager.tick()
        self.assertIsNone((await self.manager.list())["items"][0]["active"])
        self.assertEqual(await self.count(work), 1)

    async def test_backup_retains_schedule_occurrence_run_and_authorization(self):
        from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup

        row = await self.due(await self.create())
        self.console.provider.root.mkdir(exist_ok=True)
        bundle = self.root / "schedule-backup"
        manifest = await asyncio.to_thread(
            backup_sqlite, str(self.db.engine.url), self.console.provider.root, bundle
        )
        self.assertIn("runtime_schedule_schema", manifest["schemas"])
        await asyncio.to_thread(verify_backup, bundle)
        restored = self.root / "restored-schedule"
        await restore_sqlite(bundle, restored)
        db = Database("sqlite+aiosqlite:///" + str(restored / "state.sqlite"))
        try:
            manager = RunSchedules(
                TaskConsole(
                    db, "one", self.profiles, restored / "objects", policy=self.policy
                )
            )
            snapshot = (await manager.list())["items"][0]
            self.assertEqual(snapshot["id"], row["id"])
            self.assertEqual(snapshot["active"]["run_id"], row["active"]["run_id"])
            self.assertEqual(snapshot["binding"], row["binding"])
            self.assertEqual(
                (await manager.history(row["id"]))["items"][0]["id"],
                row["active"]["id"],
            )
        finally:
            await db.close()

    async def test_late_delivery_failure_cannot_revive_superseded_occurrence(self):
        row = await self.create()
        self.now = row["next_due"] + 0.1

        async def late_failure(**kwargs):
            await self.manager.reauthorize(
                row["id"], row["version"], "operator", "replace binding"
            )
            raise OSError("late failed observation")

        with patch.object(self.console, "submit", side_effect=late_failure):
            await self.manager.tick()
        self.assertIsNone((await self.manager.list())["items"][0]["active"])
        item = (await self.manager.history(row["id"]))["items"][0]
        self.assertEqual(item["state"], "discarded")
        self.assertEqual(item["failures"], 0)
        self.assertEqual(await self.count(work), 0)

    async def test_missing_migration_is_explicit_and_idempotent(self):
        result = await self.db.migrate()
        self.assertFalse(result["changed"])
        other = Database(
            "sqlite+aiosqlite:///" + str(self.root / "uninitialized.sqlite")
        )
        try:
            manager = RunSchedules(
                TaskConsole(
                    other, "one", self.profiles, self.root / "other", policy=self.policy
                )
            )
            with self.assertRaisesRegex(RuntimeErrorCode, "MIGRATION_REQUIRED"):
                await manager.list()
        finally:
            await other.close()

    async def test_transient_failure_retries_with_same_occurrence_and_bounded_backoff(
        self,
    ):
        row = await self.create()
        self.now = row["next_due"] + 0.1
        with patch.object(
            self.console, "submit", side_effect=OSError("temporary source unavailable")
        ):
            await self.manager.tick()
        item = (await self.manager.list())["items"][0]["active"]
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["failures"], 1)
        await self.manager.tick()
        self.assertEqual(await self.count(work), 0)
        self.now = item["retry_at"] + 0.1
        await self.manager.tick()
        current = (await self.manager.list())["items"][0]["active"]
        self.assertEqual(current["id"], item["id"])
        self.assertEqual(current["state"], "running")
        self.assertEqual(await self.count(work), 1)

    async def test_external_principal_and_unmanaged_profile_cannot_create_schedules(
        self,
    ):
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_ACTION_DENIED"):
            RunSchedules(self.console.for_principal({"subject": "user"}))
        self.profile["governance"] = {"mode": "trusted-local"}
        (self.profiles / "review.json").write_text(json.dumps(self.profile))
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_POLICY_REQUIRED"):
            await self.create()
        self.assertEqual((await self.manager.list())["items"], [])

    async def test_renewed_policy_does_not_replace_active_work(self):
        row = await self.due(await self.create())
        original = await self.console.detail(row["active"]["run_id"])
        config = json.loads((self.root / "work-policy.json").read_text())
        config["actor"] = "service:renewed"
        (self.root / "work-policy.json").write_text(json.dumps(config))
        updated = await self.manager.reauthorize(
            row["id"], row["version"], "operator", "new policy for future runs"
        )
        self.assertEqual(updated["active"]["run_id"], original["id"])
        self.assertEqual(updated["binding"]["actor"], "service:renewed")
        recovered = await self.console.detail(original["id"])
        self.assertEqual(recovered["authorization"], original["authorization"])
        self.assertEqual(await self.count(work), 1)
