"""Shared quota races, execution uncertainty, authority and upgrade contracts."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.runtime.adapter import LaunchSpec
from app.runtime.capacity import Capacity
from app.runtime.capacity_schema import changes, metadata
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.schema import attempts
from app.runtime.store import RuntimeStore
from sqlalchemy import select
from work_auth_fixtures import policy_fixture


class CapacityScenarios:
    postgres = False

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.url = (
            os.environ["DIOS_TEST_POSTGRES_URL"]
            if self.postgres
            else "sqlite+aiosqlite:///" + str(self.root / "state.sqlite")
        )
        self.db = Database(self.url)
        await self.db.migrate()
        self.scope = str(uuid4())
        self.store = RuntimeStore(self.db, self.scope)
        self.capacity = Capacity(self.store)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def worker(self, store=None):
        identity = (str(uuid4()), str(uuid4()))
        await (store or self.store).register_worker(
            *identity, "test-host", os.getpid(), 16
        )
        return identity

    async def submit(self, key=None, profile="one", **extra):
        return await self.store.submit(
            key or str(uuid4()), {"runtime_profile": profile, **extra}
        )

    async def limit(self, maximum=1, selector=None, name="shared", revision=0):
        return await self.capacity.put(
            name, selector or {"kind": "scope"}, maximum, revision
        )

    async def test_independent_database_connections_cannot_oversubscribe(self):
        await self.limit(3)
        for _ in range(12):
            await self.submit()
        databases = [Database(self.url) for _ in range(12)]
        try:
            stores = [RuntimeStore(db, self.scope) for db in databases]
            workers = [await self.worker(store) for store in stores]
            claimed = await asyncio.gather(
                *(store.claim(*worker) for store, worker in zip(stores, workers))
            )
            self.assertEqual(sum(x is not None for x in claimed), 3)
            self.assertEqual(
                (await self.capacity.status())["items"][0]["reserved_attempts"], 3
            )
        finally:
            for db in databases:
                await db.close()

    async def test_profile_group_intersection_and_long_blocked_prefix(self):
        await self.limit(0, {"kind": "profiles", "profiles": ["one", "two"]})
        for _ in range(140):
            await self.submit()
        blocked = await self.submit(profile="two")
        free = await self.submit(profile="three")
        worker = await self.worker()
        self.assertIsNone(await self.store.claim(*worker, profile_id="two"))
        self.assertEqual((await self.store.claim(*worker))["run_id"], free["id"])
        self.assertEqual((await self.store.get(blocked["id"]))["state"], "queued")
        # A missing profile must not become SQL NULL under NOT IN.
        no_profile = await self.store.submit("no-profile", {})
        self.assertEqual((await self.store.claim(*worker))["run_id"], no_profile["id"])

    async def test_overlapping_limits_all_apply_and_scopes_are_isolated(self):
        await self.limit(2)
        await self.limit(1, {"kind": "profiles", "profiles": ["one", "two"]}, "group")
        first = await self.submit()
        await self.submit(profile="two")
        third = await self.submit(profile="three")
        worker = await self.worker()
        self.assertEqual((await self.store.claim(*worker))["run_id"], first["id"])
        self.assertEqual((await self.store.claim(*worker))["run_id"], third["id"])
        self.assertIsNone(await self.store.claim(*worker))
        other = RuntimeStore(self.db, str(uuid4()))
        run = await other.submit("other", {"runtime_profile": "one"})
        self.assertEqual(
            (await other.claim(*await self.worker(other)))["run_id"], run["id"]
        )

    async def test_subject_uses_accepted_work_and_ignores_payload_impersonation(self):
        _, descriptor = policy_fixture(
            self.root, "one", LaunchSpec("image", ("command",))
        )
        await self.limit(0, {"kind": "subject", "subject": descriptor["subject"]})
        blocked = await self.store.submit(
            "authorized",
            {
                "runtime_profile": "one",
                "subject": "someone-else",
                "profile_digest": descriptor["profile_digest"],
            },
            authorization=descriptor,
        )
        free = await self.submit(subject=descriptor["subject"])
        self.assertEqual(
            (await self.store.claim(*await self.worker()))["run_id"], free["id"]
        )
        self.assertEqual((await self.store.get(blocked["id"]))["state"], "queued")

    async def test_cancel_and_revoke_do_not_release_until_exit_confirmed(self):
        await self.limit()
        run = await self.submit()
        await self.submit()
        worker = await self.worker()
        attempt = await self.store.claim(*worker)
        binding = (run["id"], attempt["id"], *worker, attempt["epoch"])
        current = await self.store.get(run["id"])
        await self.store.revoke(run["id"], current["version"])
        current = await self.store.get(run["id"])
        await self.store.cancel(run["id"], current["version"])
        self.assertIsNone(await self.store.claim(*await self.worker()))
        await self.store.confirm_cancel(*binding, "trusted-process-exit")
        self.assertIsNotNone(await self.store.claim(*await self.worker()))

    async def test_expired_owner_and_restart_keep_uncertain_reservation(self):
        await self.limit()
        await self.submit()
        await self.submit()
        attempt = await self.store.claim(*await self.worker())
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(lease_until=0)
            )
        await self.store.expire()
        await self.db.close()
        self.db = Database(self.url)
        self.store = RuntimeStore(self.db, self.scope)
        self.capacity = Capacity(self.store)
        self.assertIsNone(await self.store.claim(*await self.worker()))
        status = (await self.capacity.status())["items"][0]
        self.assertEqual(
            (
                status["reserved_attempts"],
                status["unresolved_attempts"],
                status["available"],
            ),
            (1, 1, 0),
        )

    async def test_claim_failure_rolls_back_reservation(self):
        await self.limit()
        await self.submit()
        worker = await self.worker()
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("audit unavailable")
        ), self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            await self.store.claim(*worker)
        self.assertEqual(
            (await self.capacity.status())["items"][0]["reserved_attempts"], 0
        )
        self.assertIsNotNone(await self.store.claim(*worker))

    async def test_prelaunch_failure_releases_capacity(self):
        await self.limit()
        run = await self.submit()
        await self.submit()
        worker = await self.worker()
        attempt = await self.store.claim(*worker)
        await self.store.reject_unlaunched(
            (run["id"], attempt["id"], *worker, attempt["epoch"]), "AUTH_DENIED"
        )
        self.assertIsNotNone(await self.store.claim(*await self.worker()))

    async def test_lowering_limit_preserves_active_work_and_revision_audit(self):
        await self.limit(2)
        for _ in range(3):
            await self.submit()
        worker = await self.worker()
        first = await self.store.claim(*worker)
        await self.store.claim(*worker)
        await self.limit(0, revision=1)
        self.assertIsNone(await self.store.claim(*worker))
        self.assertEqual((await self.store.get(first["run_id"]))["state"], "executing")
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await self.limit(3, revision=1)
        await self.capacity.put("shared", {"kind": "scope"}, 0, 2, enabled=False)
        self.assertIsNotNone(await self.store.claim(*worker))
        async with self.db.read(self.scope) as c:
            records = (
                (
                    await c.execute(
                        select(changes)
                        .where(changes.c.scope == self.scope)
                        .order_by(changes.c.revision)
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual([r["revision"] for r in records], [1, 2, 3])
        self.assertEqual(records[1]["record"]["before"]["max_concurrent"], 2)

    async def test_invalid_limits_are_rejected(self):
        for value in (-1, True, 1.5, 100001):
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_CAPACITY_LIMIT"):
                await self.limit(value)
        for selector in (
            {"kind": "payload"},
            {"kind": "scope", "subject": "x"},
            {"kind": "profiles", "profiles": []},
            {"kind": "profiles", "profiles": ["one", "one"]},
        ):
            with self.assertRaises(RuntimeErrorCode):
                await self.limit(selector=selector)


class SQLiteCapacityTests(CapacityScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_old_database_requires_additive_migration_and_backup_includes_limits(
        self,
    ):
        async with self.db.transaction(self.scope) as c:
            await c.run_sync(metadata.drop_all)
        await self.submit()
        worker = await self.worker()
        with self.assertRaisesRegex(RuntimeErrorCode, "CAPACITY_MIGRATION_REQUIRED"):
            await self.store.claim(*worker)
        self.assertTrue((await self.db.migrate())["changed"])
        self.assertFalse((await self.db.migrate())["changed"])
        await self.limit(0)
        artifact_root = self.root / "artifacts"
        artifact_root.mkdir()
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, artifact_root, bundle)
        verified = await asyncio.to_thread(verify_backup, bundle)
        self.assertIn("runtime_capacity_schema", verified["schemas"])
        backup_db = Database("sqlite+aiosqlite:///" + str(bundle / "state.sqlite"))
        try:
            self.assertEqual(
                (await Capacity(RuntimeStore(backup_db, self.scope)).status())["items"][
                    0
                ]["max_concurrent"],
                0,
            )
        finally:
            await backup_db.close()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgreSQLCapacityTests(CapacityScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


if __name__ == "__main__":
    unittest.main()
