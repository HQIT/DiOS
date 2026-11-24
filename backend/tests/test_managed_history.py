import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.models.tables import A2ATask, ManagedServiceCall
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.history import HistoryQueries
from app.runtime.history_archive_schema import objects, versions
from app.runtime.lifecycle import Lifecycle
from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup
from app.runtime.schema import runs
from app.runtime.store import RuntimeStore
from app.services import managed_history, managed_recovery, managed_runtime
from sqlalchemy import select
from tests import test_managed_runtime as fixtures


class ManagedHistoryTests(unittest.IsolatedAsyncioTestCase):
    postgres = False

    async def asyncSetUp(self):
        await fixtures.ManagedRuntimeTests.asyncSetUp(self)
        url = (
            os.environ["DIOS_TEST_POSTGRES_URL"]
            if self.postgres
            else f"sqlite+aiosqlite:///{self.root}/runtime.sqlite"
        )
        self.runtime = Database(url)
        await self.runtime.migrate()
        self.scope = "archive-" + uuid4().hex
        self.query = HistoryQueries(self.runtime, self.scope)
        self.artifacts = self.root / "imported-objects"
        self.source = self.root / "api.sqlite"

    async def asyncTearDown(self):
        await self.runtime.close()
        await fixtures.ManagedRuntimeTests.asyncTearDown(self)

    async def seed(self, *, large=False, managed=True):
        await fixtures.ManagedRuntimeTests.task(self, "completed")
        self.content = "中文证据与修复版本🌏" * (10000 if large else 3)
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            task.artifacts = [
                {"artifactId": "original-result", "parts": [{"text": self.content}]}
            ]
            await db.commit()
            if managed:
                row = await managed_recovery.register(
                    db,
                    "task",
                    managed_runtime.handle("task", "task"),
                    str(self.root / "agent"),
                    "task",
                )
                row.finished, row.process_instance = True, "source-process"
                await db.commit()

    async def export(self, name="bundle"):
        bundle = self.root / name
        manifest = await asyncio.to_thread(
            managed_history.export_history,
            self.source,
            "source-installation",
            settings.runtime_scope,
            bundle,
        )
        return bundle, manifest

    async def apply(self, bundle, manifest):
        return await managed_history.apply_bundle(
            bundle,
            manifest["manifest_digest"],
            self.runtime,
            self.scope,
            self.artifacts,
        )

    async def test_large_chinese_source_and_relations_preserved_without_execution(self):
        await self.seed(large=True)
        with sqlite3.connect(self.source) as source:
            source.row_factory = sqlite3.Row
            original = dict(
                source.execute("SELECT * FROM a2a_tasks WHERE id='task'").fetchone()
            )
        bundle, manifest = await self.export()
        self.assertEqual(manifest["counts"]["tasks"], 1)
        self.assertEqual(managed_history.inspect_bundle(bundle), manifest)
        result = await self.apply(bundle, manifest)
        self.assertFalse(result["execution_started"])
        mapping = (
            await self.query.imported(
                "source-installation", "task", "task", include_record=True
            )
        )["record"]
        self.assertEqual(mapping["acceptance"], "legacy_unverified")
        metadata = mapping["record"]["metadata"]
        self.assertEqual(metadata["origin_status"], "completed")
        self.assertFalse(metadata["model_session_evidence"])
        blob = LocalArtifacts(self.artifacts)
        stored = json.loads(b"".join(blob.read(metadata["source_record"]["digest"])))
        self.assertEqual(stored, original)
        self.assertEqual(
            json.loads(stored["artifacts"])[0]["parts"][0]["text"], self.content
        )
        related = await self.query.related("source-installation", "task", "task")
        self.assertEqual(
            {r["relation"] for r in related["items"]}, {"has_attempt", "has_artifact"}
        )
        self.assertTrue(all(r["resolution"] == "resolved" for r in related["items"]))
        await self.apply(bundle, manifest)
        self.assertEqual(
            (await self.query.imported("source-installation", "task", "task"))[
                "record"
            ]["target_id"],
            mapping["target_id"],
        )
        self.assertEqual(len((await self.query.list_imported())["items"]), 3)
        async with self.runtime.read(self.scope) as c:
            self.assertEqual(
                (
                    await c.execute(select(runs.c.id).where(runs.c.scope == self.scope))
                ).all(),
                [],
            )
        with sqlite3.connect(self.source) as source:
            source.row_factory = sqlite3.Row
            self.assertEqual(
                dict(
                    source.execute("SELECT * FROM a2a_tasks WHERE id='task'").fetchone()
                ),
                original,
            )
        self.adapter.start.assert_not_awaited()

    async def test_missing_attempt_stays_missing_and_shared_content_keeps_both_origins(
        self,
    ):
        await self.seed(managed=False)
        async with self.sessions() as db:
            old = await db.get(A2ATask, "task")
            db.add(
                A2ATask(
                    id="task2",
                    agent_id="agent",
                    status="completed",
                    message={},
                    artifacts=old.artifacts,
                )
            )
            await db.commit()
        bundle, manifest = await self.export()
        self.assertEqual(manifest["counts"]["attempts"], 0)
        await self.apply(bundle, manifest)
        refs = []
        for task_id in ("task", "task2"):
            related = (
                await self.query.related("source-installation", "task", task_id)
            )["items"]
            self.assertEqual(len(related), 1)
            self.assertEqual(related[0]["peer"]["kind"], "artifact")
            item = (
                await self.query.imported(
                    "source-installation",
                    "artifact",
                    related[0]["peer"]["source_id"],
                    include_record=True,
                )
            )["record"]
            refs.append(item["record"]["archive_blobs"][0]["digest"])
        self.assertEqual(refs[0], refs[1])
        self.assertEqual(
            len((await self.query.list_imported(kind="artifact"))["items"]), 2
        )

    async def test_active_unknown_and_foreign_scope_are_counted_and_left_untouched(
        self,
    ):
        await self.seed()
        async with self.sessions() as db:
            for name, state in [
                ("active", "working"),
                ("unknown", "failed"),
                ("foreign", "completed"),
            ]:
                db.add(
                    A2ATask(
                        id=name,
                        agent_id="agent",
                        status=state,
                        message={},
                        artifacts=[],
                    )
                )
            db.add(
                ManagedServiceCall(
                    id="unknown-call",
                    scope=settings.runtime_scope,
                    agent_id="agent",
                    task_id="unknown",
                    state="unknown",
                    dispatched=True,
                )
            )
            row = await managed_recovery.register(
                db, "foreign", "old-handle", str(self.root), "foreign"
            )
            row.scope, row.finished = "different", True
            await db.commit()
        bundle, manifest = await self.export()
        self.assertEqual(
            [
                manifest["counts"][k]
                for k in (
                    "tasks",
                    "active_tasks",
                    "unsettled_tasks",
                    "foreign_scope_tasks",
                )
            ],
            [1, 1, 1, 1],
        )
        self.assertEqual(manifest["counts"]["total_source_tasks"], 4)
        await self.apply(bundle, manifest)
        self.assertEqual(len((await self.query.list_imported(kind="task"))["items"]), 1)
        async with self.sessions() as db:
            self.assertEqual((await db.get(A2ATask, "active")).status, "working")
            self.assertEqual(
                (await db.get(ManagedServiceCall, "unknown-call")).state, "unknown"
            )

    async def test_conflicting_attempt_rolls_back_task_and_relations_as_one_group(self):
        await self.seed()
        bundle, manifest = await self.export()
        await RuntimeStore(self.runtime, self.scope).import_history(
            "source-installation", "attempt", "task:task", {"older": "conflict"}
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "IMPORT_CONFLICT"):
            await self.apply(bundle, manifest)
        rows = (await self.query.list_imported())["items"]
        self.assertEqual([r["kind"] for r in rows], ["attempt"])
        async with self.runtime.read(self.scope) as c:
            self.assertEqual(
                (
                    await c.execute(
                        select(objects).where(objects.c.scope == self.scope)
                    )
                ).all(),
                [],
            )

    async def test_changed_source_cannot_overwrite_imported_identity(self):
        await self.seed()
        bundle, manifest = await self.export()
        await self.apply(bundle, manifest)
        original = await self.query.imported(
            "source-installation", "task", "task", include_record=True
        )
        async with self.sessions() as db:
            task = await db.get(A2ATask, "task")
            task.error = "late source update"
            await db.commit()
        second, changed = await self.export("second")
        with self.assertRaisesRegex(RuntimeErrorCode, "IMPORT_CONFLICT"):
            await self.apply(second, changed)
        current = await self.query.imported(
            "source-installation", "task", "task", include_record=True
        )
        self.assertEqual(current["record"], original["record"])

    async def test_manifest_index_or_blob_corruption_is_rejected_before_any_import(
        self,
    ):
        await self.seed()
        bundle, manifest = await self.export()
        with self.assertRaisesRegex(RuntimeErrorCode, "HISTORY_MANIFEST_CONFLICT"):
            await managed_history.apply_bundle(
                bundle, "0" * 64, self.runtime, self.scope, self.artifacts
            )
        index = bundle / "groups.ndjson"
        original = index.read_bytes()
        index.write_bytes(original[:-2])
        with self.assertRaisesRegex(RuntimeErrorCode, "HISTORY_INDEX_MISMATCH"):
            await self.apply(bundle, manifest)
        index.write_bytes(original)
        obj = next((bundle / "objects").iterdir())
        obj.write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeErrorCode, "ARTIFACT_DIGEST_MISMATCH"):
            await self.apply(bundle, manifest)
        self.assertEqual((await self.query.list_imported())["items"], [])

    async def test_bundle_destination_never_overwrites_and_snapshot_does_not_ship_secrets(
        self,
    ):
        await self.seed()
        with sqlite3.connect(self.source) as db:
            db.execute("CREATE TABLE private_credentials (value TEXT)")
            db.execute(
                "INSERT INTO private_credentials VALUES ('private-fixture-secret')"
            )
        bundle, _ = await self.export()
        self.assertEqual(
            {p.name for p in bundle.iterdir()},
            {"objects", "groups.ndjson", "manifest.json"},
        )
        for path in bundle.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"private-fixture-secret", path.read_bytes())
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaises(FileExistsError):
            await self.export()

    async def test_scope_and_coverage_claims_must_match_verified_records(self):
        await self.seed()
        bundle, _ = await self.export()
        original = (bundle / "manifest.json").read_bytes()
        for changed_field, error in [
            ("counts", "HISTORY_COUNT_MISMATCH"),
            ("source_scope", "HISTORY_SOURCE_INVALID"),
        ]:
            changed = json.loads(original)
            if changed_field == "counts":
                changed["counts"]["total_source_tasks"] += 1
            else:
                changed["source_scope"] = "another-installation-scope"
            raw = json.dumps(changed).encode()
            (bundle / "manifest.json").write_bytes(raw)
            with self.assertRaisesRegex(RuntimeErrorCode, error):
                await managed_history.apply_bundle(
                    bundle,
                    hashlib.sha256(raw).hexdigest(),
                    self.runtime,
                    self.scope,
                    self.artifacts,
                )
        self.assertEqual((await self.query.list_imported())["items"], [])

    async def test_wal_commits_are_included_in_online_snapshot(self):
        await self.seed()
        with sqlite3.connect(self.source) as writer:
            self.assertEqual(
                writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
            )
            writer.execute(
                "UPDATE a2a_tasks SET error='committed WAL data' WHERE id='task'"
            )
            writer.commit()
            bundle, manifest = await self.export()
        await self.apply(bundle, manifest)
        item = (
            await self.query.imported(
                "source-installation", "task", "task", include_record=True
            )
        )["record"]
        blob = item["record"]["archive_blobs"][0]
        raw = json.loads(b"".join(LocalArtifacts(self.artifacts).read(blob["digest"])))
        self.assertEqual(raw["error"], "committed WAL data")

    async def test_backup_restore_includes_all_history_blobs_and_preserves_mapping(
        self,
    ):
        await self.seed(large=True)
        bundle, manifest = await self.export()
        await self.apply(bundle, manifest)
        original = (await self.query.list_imported())["items"]
        backup = self.root / "backup"
        result = await asyncio.to_thread(
            backup_sqlite, str(self.runtime.engine.url), self.artifacts, backup
        )
        self.assertEqual(len(result["objects"]), 3)
        self.assertIn("runtime_history_archive_schema", result["schemas"])
        self.assertEqual(verify_backup(backup)["objects"], result["objects"])
        restored = self.root / "restored"
        await restore_sqlite(backup, restored)
        target = Database(f"sqlite+aiosqlite:///{restored}/state.sqlite")
        try:
            current = (await HistoryQueries(target, self.scope).list_imported())[
                "items"
            ]
            self.assertEqual(current, original)
            self.assertEqual(
                (await Lifecycle(target, self.scope).status())["mode"], "restore_hold"
            )
            for key in result["objects"]:
                self.assertEqual(
                    LocalArtifacts(restored / "objects").verify(key).size,
                    result["objects"][key]["size"],
                )
        finally:
            await target.close()

    async def test_missing_history_blob_stops_backup_publication(self):
        await self.seed()
        bundle, manifest = await self.export()
        await self.apply(bundle, manifest)
        next(self.artifacts.iterdir()).unlink()
        with self.assertRaises(FileNotFoundError):
            await asyncio.to_thread(
                backup_sqlite,
                str(self.runtime.engine.url),
                self.artifacts,
                self.root / "bad-backup",
            )
        self.assertFalse((self.root / "bad-backup/manifest.json").exists())

    async def test_additive_migration_preserves_existing_history_and_rejects_unversioned_objects(
        self,
    ):
        store = RuntimeStore(self.runtime, self.scope)
        before = await store.import_history(
            "older", "task", "original", {"original": "history"}
        )
        async with self.runtime.engine.begin() as c:
            await c.run_sync(objects.drop)
            await c.run_sync(versions.drop)
        self.assertTrue((await self.runtime.migrate())["changed"])
        self.assertFalse((await self.runtime.migrate())["changed"])
        self.assertEqual(
            (await self.query.imported("older", "task", "original"))["record"][
                "target_id"
            ],
            before["target_id"],
        )
        async with self.runtime.engine.begin() as c:
            await c.run_sync(versions.drop)
        with self.assertRaisesRegex(
            RuntimeErrorCode, "HISTORY_ARCHIVE_SCHEMA_UNVERSIONED"
        ):
            await self.runtime.migrate()

    async def test_cli_export_inspect_import_and_existing_history_query(self):
        await self.seed()
        bundle = self.root / "cli-bundle"

        async def cli(module, *args):
            command = (
                [sys.executable, "-m", module]
                if module
                else [sys.executable, str(Path(__file__).parents[2] / "cli/dios")]
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                *map(str, args),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), 20)
            self.assertEqual(process.returncode, 0, err.decode())
            return json.loads(out)

        module = "app.services.managed_history"
        result = await cli(
            None,
            "managed-history",
            "export",
            "--source-db",
            self.source,
            "--source-instance",
            "source-installation",
            "--source-scope",
            settings.runtime_scope,
            "--destination",
            bundle,
        )
        self.assertEqual(result, await cli(module, "inspect", bundle))
        applied = await cli(
            module,
            "import",
            bundle,
            "--expected-digest",
            result["manifest_digest"],
            "--database-url",
            self.runtime.engine.url,
            "--scope",
            self.scope,
            "--artifacts",
            self.artifacts,
        )
        self.assertFalse(applied["execution_started"])
        queried = await cli(
            "app.runtime.cli",
            "--database-url",
            self.runtime.engine.url,
            "--scope",
            self.scope,
            "history",
            "links",
            "source-installation",
            "task",
            "task",
        )
        self.assertEqual(len(queried["items"]), 2)


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"),
    "explicit PostgreSQL qualification required",
)
class PostgresHistoryArchiveTests(unittest.IsolatedAsyncioTestCase):
    postgres = True
    asyncSetUp = ManagedHistoryTests.asyncSetUp
    asyncTearDown = ManagedHistoryTests.asyncTearDown
    seed = ManagedHistoryTests.seed
    export = ManagedHistoryTests.export
    apply = ManagedHistoryTests.apply
    test_large_chinese_source_and_relations_preserved_without_execution = ManagedHistoryTests.test_large_chinese_source_and_relations_preserved_without_execution
    test_conflicting_attempt_rolls_back_task_and_relations_as_one_group = ManagedHistoryTests.test_conflicting_attempt_rolls_back_task_and_relations_as_one_group
