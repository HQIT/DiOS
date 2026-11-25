"""Logical outcome accounting survives replay, migration, restart and access changes."""

import os
import unittest
from unittest.mock import patch

import test_runtime_capacity as fixtures
import test_runtime_principals as principal_fixtures
from app.runtime.adapter import LaunchSpec
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.outcome_schema import metadata, outcomes
from app.runtime.overview import observation, view
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.worker import Worker
from app.services.task_console import console_filter
from app.services.task_inputs import CONSOLE_ORIGIN
from sqlalchemy import select
from test_runtime_worker import FakeAdapter


class OutcomeScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def finished(self, key="one", profile="one"):
        spec = LaunchSpec("test", ("run",))
        run = await self.store.submit(
            key,
            {
                "origin": CONSOLE_ORIGIN,
                "runtime_profile": profile,
                "profile_digest": spec.fingerprint(),
            },
        )
        worker = Worker(
            self.store,
            FakeAdapter(),
            profile,
            spec,
            LocalArtifacts(self.root / "objects"),
            poll_interval=0.001,
        )
        result = await worker.once()
        self.assertEqual(
            result["state"], "succeeded", [a["error"] for a in result["attempts"]]
        )
        return run

    async def overview(self, **kw):
        return await view(self.db, self.scope, console_filter(self.scope), **kw)

    async def test_first_terminal_only_survives_later_events_and_restart(self):
        run = await self.finished()
        before = await self.overview()
        async with self.db.transaction(self.scope) as c:
            current = await self.store._run(c, run["id"])
            await self.store._event(
                c,
                current,
                "attempt.output_retained",
                {"synthetic": True},
                await self.db.now(c) + 7200,
            )
        self.assertEqual((await self.overview())["window_totals"]["succeeded"], 1)
        await self.db.close()
        self.db = Database(self.url)
        after = await self.overview()
        self.assertEqual(before["history_totals"], after["history_totals"])
        self.assertTrue(after["coverage"]["complete"])

    async def test_migration_backfills_first_terminal_source_without_retimestamping(
        self,
    ):
        run = await self.finished()
        async with self.db.transaction(self.scope) as c:
            original = dict(
                (
                    await c.execute(
                        select(outcomes).where(outcomes.c.scope == self.scope)
                    )
                )
                .mappings()
                .one()
            )
            current = await self.store._run(c, run["id"])
            await self.store._event(
                c, current, "attempt.output_retained", {}, await self.db.now(c) + 7200
            )
            await c.run_sync(metadata.drop_all)
        result = await self.db.migrate()
        self.assertEqual(result["outcome_version"], 1)
        async with self.db.read(self.scope) as c:
            restored = dict(
                (
                    await c.execute(
                        select(outcomes).where(outcomes.c.scope == self.scope)
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(restored, original)
        self.assertFalse((await self.db.migrate())["changed"])

    async def test_utc_boundaries_and_profile_filter(self):
        await self.finished("one", "one")
        await self.finished("two", "two")
        now = 20000 * 86400 + 12 * 3600 + 3000
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                outcomes.update()
                .where(outcomes.c.scope == self.scope)
                .values(finished_at=now - 1)
            )
        with patch.object(self.db, "now", return_value=float(now)):
            value = await self.overview(period="hour", buckets=2)
            self.assertEqual([x["succeeded"] for x in value["series"]], [0, 2])
            filtered = await view(
                self.db,
                self.scope,
                console_filter(self.scope, "two"),
                period="day",
                buckets=2,
            )
            self.assertEqual(filtered["window_totals"]["succeeded"], 1)
            self.assertEqual(filtered["history_totals"]["succeeded"], 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_OVERVIEW_WINDOW"):
            await self.overview(period="day", buckets=91)

    async def test_outcome_failure_rolls_back_run_and_missing_history_is_explicit(self):
        run = await self.store.submit(
            "queued", {"origin": CONSOLE_ORIGIN, "runtime_profile": "one"}
        )
        with (
            patch(
                "app.runtime.outcome_schema.require",
                side_effect=RuntimeErrorCode("OUTCOME_MIGRATION_REQUIRED"),
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "OUTCOME_MIGRATION_REQUIRED"),
        ):
            await self.store.cancel(run["id"], run["version"])
        current = await self.store.get(run["id"])
        self.assertEqual(current["state"], "queued")
        await self.store.cancel(run["id"], current["version"])
        self.assertEqual((await self.overview())["history_totals"]["cancelled"], 1)
        async with self.db.transaction(self.scope) as c:
            await c.execute(outcomes.delete().where(outcomes.c.scope == self.scope))
        current = await self.overview()
        self.assertFalse(current["coverage"]["complete"])
        self.assertEqual(current["coverage"]["missing_terminal_outcomes"], 1)

    async def test_backup_keeps_outcome_evidence(self):
        if self.postgres:
            self.skipTest("SQLite backup provider")
        await self.finished()
        backup_sqlite(self.url, str(self.root / "objects"), str(self.root / "backup"))
        verified = verify_backup(str(self.root / "backup"))
        self.assertIn("runtime_outcome_schema", verified["schemas"])


class SQLiteOutcomeTests(OutcomeScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL fixture disabled"
)
class PostgresOutcomeTests(OutcomeScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class OverviewAccessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = principal_fixtures.PrincipalTests.asyncSetUp
    asyncTearDown = principal_fixtures.PrincipalTests.asyncTearDown
    save = principal_fixtures.PrincipalTests.save
    headers_for = principal_fixtures.PrincipalTests.headers_for
    submit_as = principal_fixtures.PrincipalTests.submit_as
    get = principal_fixtures.PrincipalTests.get
    finish = principal_fixtures.PrincipalTests.finish

    async def test_stats_and_list_share_live_object_and_profile_filters(self):
        await self.submit_as("a")
        await self.finish()
        b = await self.submit_as("b")
        await self.finish()
        own = (await self.get("a", "overview")).json()
        self.assertEqual(own["history_totals"]["succeeded"], 1)
        operator = await self.client.get(
            "/api/os/runtime/overview", headers=self.headers
        )
        self.assertEqual(operator.json()["history_totals"]["succeeded"], 0)
        self.directory["principals"]["a"]["objects"][b["id"]] = ["run.read"]
        self.save()
        shared = (await self.get("a", "overview?profile_id=review")).json()
        listing = (await self.get("a", "runs?profile_id=review")).json()
        self.assertEqual(shared["history_totals"]["succeeded"], len(listing["items"]))
        self.directory["principals"]["a"]["objects"].clear()
        self.save()
        self.assertEqual(
            (await self.get("a", "overview")).json()["history_totals"]["succeeded"], 1
        )
        self.assertEqual(
            (await self.get("a", "overview?profile_id=absent")).json()[
                "history_totals"
            ]["succeeded"],
            0,
        )
        self.assertEqual(
            (await self.get("a", "overview?period=day&buckets=91")).status_code, 422
        )


class ObservationTests(unittest.TestCase):
    def test_fresh_silent_stale_paused_and_process_evidence(self):
        row = {
            "id": "r",
            "profile_id": "p",
            "state": "executing",
            "attempt_id": "a",
            "phase": "waiting_model",
            "started_at": 1,
            "last_output_at": None,
            "retry_at": None,
            "lease_until": 1100,
            "session_id": "s",
            "heartbeat_at": 1000,
            "remote": {
                "runtime": {
                    "observed_at": 1000,
                    "alive": True,
                    "pid": 12,
                    "process_instance": "boot",
                    "state": "running",
                }
            },
        }
        self.assertEqual(observation(row, 1000)["diagnosis"], "no_recent_output")
        self.assertTrue(observation(row, 1000)["confirmed_session"])
        self.assertEqual(observation(row, 1020)["diagnosis"], "observation_stale")
        row["remote"]["runtime"]["state"] = "paused"
        self.assertFalse(observation(row, 1000)["confirmed_session"])
        self.assertEqual(observation(row, 1000)["diagnosis"], "process_paused")
        row["remote"]["runtime"].pop("pid")
        self.assertFalse(observation(row, 1000)["confirmed_session"])
        row["remote"]["runtime"]["observed_at"] = "malformed"
        self.assertEqual(observation(row, 1000)["diagnosis"], "observation_stale")
        row["remote"]["runtime"] = ["malformed"]
        self.assertFalse(observation(row, 1000)["confirmed_session"])
