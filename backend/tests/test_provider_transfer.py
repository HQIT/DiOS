"""Real PostgreSQL transfer, lossless references, crash rollback and held authority."""

import asyncio
import os
import signal
import sys
import time
import unittest
from uuid import uuid4

from app.runtime.artifacts import LocalArtifacts
from app.runtime.credential_schema import credentials
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.history import HistoryQueries
from app.runtime.lifecycle import Lifecycle
from app.runtime.object_access import ObjectPolicies
from app.runtime.provider_transfer import transfer
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.schema import runs
from app.runtime.store import RuntimeStore
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from tests.test_runtime_recovery import RecoveryTests


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_TRANSFER_ADMIN_URL"), "isolated PostgreSQL required"
)
class ProviderTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = RecoveryTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.run, self.artifact, self.blob = await self.fixture.seed()
        self.queued = await self.fixture.store.submit("queued", {"中文": "待执行🙂"})
        current = await self.fixture.store.get(self.run["id"])
        attempt = current["attempts"][0]
        async with self.fixture.db.transaction("test") as conn:
            await conn.execute(
                credentials.insert().values(
                    id="old-credential",
                    scope="test",
                    run_id=self.run["id"],
                    attempt_id=attempt["id"],
                    binding={"source": "synthetic credential fixture"},
                    authority={},
                    key_id="fixture",
                    state="active",
                    version=3,
                    nonce="nonce",
                    token_hash="a" * 64,
                    previous_hash="b" * 64,
                    previous_until=time.time() + 5,
                    expires_at=time.time() + 600,
                    created_at=time.time(),
                    updated_at=time.time(),
                )
            )
        self.principal = {
            "issuer": "fixture",
            "audience": "fixture",
            "scope": "test",
            "id": "alice",
            "subject": "user:alice",
            "actor": "app:test",
        }
        self.ref = {"kind": "artifact", "id": self.artifact, "version": 1}
        await ObjectPolicies(self.fixture.db, "test").change(
            self.ref,
            [
                {
                    "principal": self.principal,
                    "actions": ["object.read", "object.download"],
                    "expires_at": time.time() + 600,
                }
            ],
            0,
            "fixture-access",
            "operator:test",
        )
        self.manifest = await asyncio.to_thread(
            backup_sqlite,
            self.fixture.url,
            self.fixture.provider.root,
            self.fixture.bundle,
        )
        self.name = "dios_transfer_test_" + uuid4().hex
        self.admin = Database(os.environ["DIOS_TEST_TRANSFER_ADMIN_URL"])
        self.addAsyncCleanup(self.admin.close)
        async with self.admin.engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text('CREATE DATABASE "' + self.name + '"'))
        self.target = Database(
            make_url(os.environ["DIOS_TEST_TRANSFER_ADMIN_URL"]).set(database=self.name)
        )
        self.addAsyncCleanup(self.drop_database)
        await self.target.migrate()

    async def drop_database(self):
        await self.target.close()
        async with self.admin.engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text('DROP DATABASE "' + self.name + '"'))

    async def count_runs(self):
        async with self.target.engine.connect() as conn:
            return (
                await conn.execute(text("SELECT count(*) FROM runtime_runs"))
            ).scalar()

    async def test_lossless_idempotent_and_hold(self):
        planned = await transfer(
            self.fixture.bundle, self.fixture.restored, self.target
        )
        self.assertEqual(planned["state"], "ready")
        self.assertFalse(self.fixture.restored.exists())
        result = await transfer(
            self.fixture.bundle, self.fixture.restored, self.target, apply=True
        )
        self.assertEqual(result["state"], "transferred")
        self.assertTrue(all(row["matched"] for row in result["tables"].values()))
        self.assertIn("runtime_studio_releases", result["tables"])
        self.assertIn("runtime_technical_releases", result["tables"])
        replay = await transfer(
            self.fixture.bundle, self.fixture.restored, self.target, apply=True
        )
        self.assertEqual(replay["state"], "already_transferred")
        self.assertEqual(replay["tables"], result["tables"])
        target = RuntimeStore(self.target, "test")
        actual, expected = (
            await target.get(self.run["id"]),
            await self.fixture.store.get(self.run["id"]),
        )
        actual.pop("observed_at")
        expected.pop("observed_at")
        self.assertEqual(actual, expected)
        self.assertEqual(
            (await HistoryQueries(self.target, "test").run(self.run["id"]))[
                "artifacts"
            ]["items"][0]["id"],
            self.artifact,
        )
        self.assertEqual(
            (
                await HistoryQueries(self.target, "test").related(
                    "old", "task", "old-task"
                )
            )["items"][0]["resolution"],
            "unresolved",
        )
        self.assertEqual(
            b"".join(
                LocalArtifacts(self.fixture.restored / "objects").read(self.blob.key)
            ),
            "中文产物".encode(),
        )
        async with self.target.read("test") as conn:
            credential = (await conn.execute(select(credentials))).mappings().one()
        self.assertEqual(
            (credential["state"], credential["version"], credential["previous_until"]),
            ("revoked", 4, None),
        )
        for operation in (
            target.submit("new", {}),
            target.claim("w", "instance"),
            Lifecycle(self.target, "test").resume(0),
        ):
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
            ):
                await operation
        self.assertEqual(
            (await target.submit("queued", {"中文": "待执行🙂"}))["id"],
            self.queued["id"],
        )
        self.assertEqual(await self.count_runs(), 2)
        self.assertEqual(verify_backup(self.fixture.bundle), self.manifest)
        policy = ObjectPolicies(self.target, "test")
        self.assertEqual(
            await policy.show(self.ref),
            await ObjectPolicies(self.fixture.db, "test").show(self.ref),
        )
        await policy.check(self.ref, self.principal, "object.download", audit=False)
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await policy.check(
                self.ref,
                dict(self.principal, id="bob", subject="user:bob"),
                "object.download",
                audit=False,
            )

    async def test_sigkill_rolls_back_rows_then_retry_reuses_objects(self):
        marker = self.fixture.root / "before-commit"
        program = """import asyncio,os,signal
from pathlib import Path
from app.runtime.database import Database
from app.runtime.lifecycle import Lifecycle
from app.runtime.provider_transfer import transfer
original=Lifecycle.hold_restored_in_transaction
async def stop(self,c):
 await original(self,c)
 Path(os.environ['TRANSFER_MARKER']).write_text('uncommitted')
 os.kill(os.getpid(),signal.SIGKILL)
Lifecycle.hold_restored_in_transaction=stop
async def main():
 db=Database(os.environ['TRANSFER_TARGET'])
 await transfer(Path(os.environ['TRANSFER_BUNDLE']),Path(os.environ['TRANSFER_DEST']),db,apply=True)
asyncio.run(main())
"""
        env = dict(
            os.environ,
            TRANSFER_TARGET=self.target.engine.url.render_as_string(
                hide_password=False
            ),
            TRANSFER_BUNDLE=str(self.fixture.bundle),
            TRANSFER_DEST=str(self.fixture.restored),
            TRANSFER_MARKER=str(marker),
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await asyncio.wait_for(process.communicate(), 45)
        self.assertEqual(process.returncode, -signal.SIGKILL, error.decode()[-2000:])
        self.assertTrue(marker.exists())
        self.assertEqual(await self.count_runs(), 0)
        self.assertTrue((self.fixture.restored / "objects" / self.blob.key).is_file())
        result = await transfer(
            self.fixture.bundle, self.fixture.restored, self.target, apply=True
        )
        self.assertEqual(result["state"], "transferred")
        self.assertEqual(await self.count_runs(), 2)

    async def test_nonempty_target_is_not_overwritten(self):
        store = RuntimeStore(self.target, "existing")
        run = await store.submit("existing", {"keep": True})
        before = await store.get(run["id"])
        with self.assertRaisesRegex(RuntimeErrorCode, "TRANSFER_"):
            await transfer(
                self.fixture.bundle, self.fixture.restored, self.target, apply=True
            )
        after = await store.get(run["id"])
        after.pop("observed_at")
        before.pop("observed_at")
        self.assertEqual(after, before)
        self.assertEqual(await self.count_runs(), 1)

    async def test_corrupt_blob_and_unknown_columns_fail_closed(self):
        blob = self.fixture.bundle / "objects" / self.blob.key
        original = blob.read_bytes()
        blob.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_ARTIFACT_MISMATCH"):
            await transfer(
                self.fixture.bundle, self.fixture.restored, self.target, apply=True
            )
        self.assertFalse(self.fixture.restored.exists())
        blob.write_bytes(original)
        async with self.target.engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE runtime_runs ADD COLUMN unexpected text")
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "TRANSFER_SCHEMA_MISMATCH"):
            await transfer(
                self.fixture.bundle, self.fixture.restored, self.target, apply=True
            )
        self.assertEqual(await self.count_runs(), 0)

    async def test_guard_and_source_changes_are_rejected(self):
        await transfer(
            self.fixture.bundle, self.fixture.restored, self.target, apply=True
        )
        async with self.fixture.db.transaction("test") as conn:
            await conn.execute(
                runs.update().where(runs.c.id == self.queued["id"]).values(version=2)
            )
        changed = self.fixture.root / "changed-bundle"
        await asyncio.to_thread(
            backup_sqlite, self.fixture.url, self.fixture.provider.root, changed
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "TRANSFER_SOURCE_CHANGED"):
            await transfer(changed, self.fixture.restored, self.target, apply=True)
        self.assertEqual(await self.count_runs(), 2)
        from app.runtime.studio_release_schema import GUARDS

        async with self.fixture.db.engine.begin() as conn:
            await conn.execute(text('DROP TRIGGER "' + next(iter(GUARDS)) + '"'))
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_SCHEMA_INCOMPATIBLE"):
            await asyncio.to_thread(
                backup_sqlite,
                self.fixture.url,
                self.fixture.provider.root,
                self.fixture.root / "bad-guards",
            )
