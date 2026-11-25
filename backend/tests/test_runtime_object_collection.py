"""Offline deletion, writer fencing, durable restart and backup qualification."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.object_access import ObjectPolicies
from app.runtime.object_collection import collect
from app.runtime.object_lifetime import ObjectLifetime
from app.runtime.objects import Objects
from app.runtime.recovery import backup_sqlite, restore_sqlite
from app.runtime.remote_schema import chunks, uploads
from app.runtime.storage_lease import StorageLease, artifacts_lock, database_lock
import test_runtime_objects as fixtures
import test_runtime_task_console as console_fixtures


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.ObjectScenarios.asyncSetUp
    asyncTearDown = fixtures.ObjectScenarios.asyncTearDown

    async def test_cli_dry_run_and_apply(self):
        value = await self.publish("cli")
        await self.delete(value, "delete")
        await self.offline()
        command = [
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--database-url",
            self.url,
            "object-collect",
            "--artifacts",
            str(self.root / "objects"),
            "--actor",
            "operator",
        ]

        def run(extra):
            result = subprocess.run(command + extra, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        self.assertEqual((await asyncio.to_thread(run, []))["blobs"], 1)
        self.assertEqual(
            (await asyncio.to_thread(run, ["--apply", "--key", "cli"]))[
                "collected_blobs"
            ],
            1,
        )

    async def publish(self, key, data=b"content"):
        return await self.catalog.publish(self.provider, [data], key, "operator")

    async def delete(self, value, key):
        return await ObjectLifetime(self.db, self.scope).change(
            value["ref"], expected_revision=0, key=key, actor="operator", delete=True
        )

    async def offline(self):
        self.provider.close()
        await self.db.close()

    def reopen(self):
        self.db = Database(self.url)
        self.catalog = Objects(self.db, self.scope)
        self.provider = LocalArtifacts(self.root / "objects")

    async def test_collect_known_deleted_only_preserve_shared_identity_and_restore(
        self,
    ):
        deleted = await self.publish("deleted", b"unique")
        shared = await self.publish("shared")
        live = await self.publish("live")
        orphan = self.provider.put([b"unregistered"])
        await self.delete(deleted, "delete-unique")
        await self.delete(shared, "delete-shared")
        await self.offline()
        plan = collect(self.url, self.root / "objects")
        self.assertEqual(plan["blobs"], 1)
        self.assertEqual(plan["shared_blobs_retained"], 1)
        done = collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertEqual(done["collected_blobs"], 1)
        self.assertFalse((self.root / "objects" / deleted["storage"]["key"]).exists())
        self.assertTrue((self.root / "objects" / shared["storage"]["key"]).exists())
        self.assertTrue((self.root / "objects" / orphan.key).exists())
        self.assertEqual(
            collect(self.url, self.root / "objects", apply=True, key="gc"), done
        )
        self.assertEqual(collect(self.url, self.root / "objects")["blobs"], 0)
        self.reopen()
        self.assertEqual(
            (await ObjectLifetime(self.db, self.scope).show(deleted["ref"]))[
                "content_collection"
            ],
            "collected",
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await ObjectPolicies(self.db, self.scope).check(
                deleted["ref"], None, "object.download"
            )
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, self.provider.root, bundle)
        destination = self.root / "restored"
        await restore_sqlite(bundle, destination)
        paths = list(destination.glob("*.sqlite"))
        self.assertEqual(len(paths), 1)
        restored = Database("sqlite+aiosqlite:///" + str(paths[0]))
        try:
            self.assertEqual(
                (await ObjectLifetime(restored, self.scope).show(deleted["ref"]))[
                    "content_collection"
                ],
                "collected",
            )
            self.assertEqual(
                (await Objects(restored, self.scope).get(live["ref"]))["ref"],
                live["ref"],
            )
            self.assertFalse(
                (destination / "objects" / deleted["storage"]["key"]).exists()
            )
        finally:
            await restored.close()
        republished = await self.publish("republished", b"unique")
        self.assertNotEqual(republished["ref"], deleted["ref"])
        self.assertEqual(
            b"".join(self.provider.read(republished["storage"]["key"])), b"unique"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await ObjectPolicies(self.db, self.scope).check(
                deleted["ref"], None, "object.read"
            )

    async def test_live_database_or_provider_refuses_collection_and_new_writers_block(
        self,
    ):
        value = await self.publish("one")
        await self.delete(value, "delete")
        with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
            collect(self.url, self.provider.root)
        await self.db.close()
        with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
            collect(self.url, self.provider.root)
        self.provider.close()
        with (
            StorageLease(database_lock(self.root / "state.sqlite"), exclusive=True),
            StorageLease(artifacts_lock(self.root / "objects"), exclusive=True),
        ):
            with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
                Database(self.url)
            with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
                LocalArtifacts(self.root / "objects")
            with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
                backup_sqlite(
                    self.url, self.root / "objects", self.root / "blocked-backup"
                )
        self.reopen()

    async def test_process_lease_holds_until_worker_process_exits(self):
        await self.publish("one")
        await self.offline()
        code = 'from app.runtime.artifacts import LocalArtifacts;from pathlib import Path;import sys;p=LocalArtifacts(Path(sys.argv[1]));print("ready",flush=True);sys.stdin.read()'
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.root / "objects")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(await asyncio.to_thread(child.stdout.readline), "ready\n")
            with self.assertRaisesRegex(RuntimeErrorCode, "STORAGE_MAINTENANCE_BUSY"):
                collect(self.url, self.root / "objects")
        finally:
            child.communicate("", timeout=5)
        self.assertEqual(collect(self.url, self.root / "objects")["blobs"], 0)

    async def test_crash_after_unlink_resumes_without_losing_tombstone_or_backup(self):
        value = await self.publish("one")
        await self.delete(value, "delete")
        await self.offline()
        original = Path.unlink

        def crash(path, *args, **kwargs):
            original(path, *args, **kwargs)
            if path.name == value["storage"]["key"]:
                raise OSError("injected power loss after unlink")

        with patch.object(Path, "unlink", crash), self.assertRaises(OSError):
            collect(self.url, self.root / "objects", apply=True, key="gc")
        bundle = self.root / "pending-backup"
        backup_sqlite(self.url, self.root / "objects", bundle)
        done = collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertEqual(done["collected_blobs"], 1)
        self.reopen()
        self.assertEqual(
            (await ObjectLifetime(self.db, self.scope).show(value["ref"]))[
                "content_collection"
            ],
            "collected",
        )

    async def test_resume_rechecks_new_same_digest_reference(self):
        value = await self.publish("one")
        await self.delete(value, "delete")
        await self.offline()
        with (
            patch.object(Path, "unlink", side_effect=OSError("injected before unlink")),
            self.assertRaises(OSError),
        ):
            collect(self.url, self.root / "objects", apply=True, key="gc")
        self.reopen()
        fresh = await self.publish("fresh")
        await self.offline()
        report = collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertEqual(report["retained_on_resume"], 1)
        self.assertEqual(report["collected_blobs"], 0)
        self.assertTrue((self.root / "objects" / fresh["storage"]["key"]).exists())

    async def test_pending_collection_restores_and_new_target_finishes_release(self):
        from app.runtime.storage_lease import bind_storage

        value = await self.publish("one")
        await self.delete(value, "delete")
        await self.offline()
        with (
            patch.object(Path, "unlink", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            collect(self.url, self.root / "objects", apply=True, key="source-gc")
        backup_sqlite(self.url, self.root / "objects", self.root / "backup")
        target = self.root / "restored"
        await restore_sqlite(self.root / "backup", target)
        url = "sqlite+aiosqlite:///" + str(next(target.glob("*.sqlite")))
        db = Database(url)
        provider = LocalArtifacts(target / "objects")
        bind_storage(provider, db)
        self.assertEqual(
            (await ObjectLifetime(db, self.scope).show(value["ref"]))[
                "content_collection"
            ],
            "pending",
        )
        provider.close()
        await db.close()
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            collect(url, target / "objects", apply=True, key="source-gc")
        report = collect(url, target / "objects", apply=True, key="restored-target-gc")
        self.assertEqual(report["released_objects"], 1)
        db = Database(url)
        try:
            state = await ObjectLifetime(db, self.scope).show(value["ref"])
            self.assertEqual(state["ref"], value["ref"])
            self.assertEqual(state["content_collection"], "collected")
        finally:
            await db.close()

    async def test_remote_chunks_uploads_and_recovery_receipts_pin_content(self):
        values = [
            await self.publish(k, k.encode())
            for k in ["chunk", "upload", "exit", "tool"]
        ]
        for i, value in enumerate(values):
            await self.delete(value, "delete-" + str(i))
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                chunks.insert().values(
                    upload_id="u", offset=0, key=values[0]["storage"]["key"], size=5
                )
            )
            await c.execute(
                uploads.insert().values(
                    id="u",
                    scope=self.scope,
                    attempt_id="a",
                    key=values[1]["storage"]["key"],
                    size=6,
                    received=0,
                    complete=False,
                    created_at=1,
                )
            )
        for folder, value in [
            (".exits-retained", values[2]),
            (".tool-receipts-v1", values[3]),
        ]:
            path = self.provider.root / folder
            path.mkdir()
            (path / "receipt.json").write_text(
                json.dumps({"key": value["storage"]["key"]})
            )
        await self.offline()
        report = collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertEqual(report["collected_blobs"], 0)
        self.assertEqual(report["shared_blobs_retained"], 4)

    async def test_other_database_binding_refuses_deletion(self):
        value = await self.publish("one")
        await self.delete(value, "delete")
        other = Database("sqlite+aiosqlite:///" + str(self.root / "other.sqlite"))
        await other.migrate()
        try:
            await Objects(other, "other").publish(
                self.provider, [b"other"], "two", "operator"
            )
        finally:
            await other.close()
        await self.offline()
        with self.assertRaisesRegex(
            RuntimeErrorCode, "COLLECTION_ROOT_BINDING_REQUIRED"
        ):
            collect(self.url, self.root / "objects", apply=True, key="gc")

    async def test_corruption_fails_before_any_collection_intent(self):
        value = await self.publish("one")
        await self.delete(value, "delete")
        await self.offline()
        (self.root / "objects" / value["storage"]["key"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(RuntimeErrorCode, "ARTIFACT_DIGEST_MISMATCH"):
            collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertTrue((self.root / "objects" / value["storage"]["key"]).exists())

    async def test_symlink_blob_is_never_followed(self):
        value = await self.publish("one")
        await self.delete(value, "delete")
        await self.offline()
        target = self.root / "objects" / value["storage"]["key"]
        target.unlink()
        external = self.root / "external"
        external.write_bytes(b"keep")
        target.symlink_to(external)
        with self.assertRaisesRegex(RuntimeErrorCode, "COLLECTION_PATH_INVALID"):
            collect(self.url, self.root / "objects", apply=True, key="gc")
        self.assertEqual(external.read_bytes(), b"keep")


class ArtifactCollectionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = console_fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = console_fixtures.TaskConsoleTests.asyncTearDown
    submit = console_fixtures.TaskConsoleTests.submit
    finish = console_fixtures.TaskConsoleTests.finish

    async def test_native_result_collected_but_retained_derived_version_and_history_survive(
        self,
    ):
        import time

        result = await self.finish(await self.submit())
        artifact = next(a for a in result["artifacts"] if a["kind"] == "result")
        catalog = Objects(self.db, "one")
        source = artifact["object_ref"]
        derived = await catalog.publish(
            self.console.provider, [b"derived"], "derived", "operator"
        )
        await catalog.link(derived["ref"], "derived_from", source, "operator")
        lifetime = ObjectLifetime(self.db, "one")
        await lifetime.change(
            derived["ref"],
            expected_revision=0,
            key="retain",
            actor="operator",
            retain_until=time.time() + 300,
        )
        await lifetime.change(
            source, expected_revision=0, key="delete", actor="operator", delete=True
        )
        url = "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite")
        root = self.console.provider.root
        self.console.provider.close()
        await self.db.close()
        report = collect(url, root, apply=True, key="gc")
        self.assertEqual(report["collected_blobs"], 1)
        self.assertFalse((root / artifact["digest"]).exists())
        self.assertTrue((root / derived["storage"]["key"]).exists())
        backup_sqlite(url, root, self.root / "backup")
        restored = self.root / "restored"
        await restore_sqlite(self.root / "backup", restored)
        db = Database("sqlite+aiosqlite:///" + str(next(restored.glob("*.sqlite"))))
        try:
            value = await ObjectLifetime(db, "one").show(source)
            self.assertEqual(value["binding"]["run_id"], result["id"])
            self.assertEqual(value["content_collection"], "collected")
            self.assertEqual(
                (await ObjectLifetime(db, "one").show(derived["ref"]))["state"],
                "source_unavailable",
            )
        finally:
            await db.close()
