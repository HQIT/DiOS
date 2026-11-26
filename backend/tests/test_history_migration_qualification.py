"""Crash qualification on populated legacy copies and immutable history groups."""

import asyncio
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import SCHEMA_CHECKSUM, Database
from app.runtime.history import HistoryQueries
from app.runtime.history_archive import import_group
from app.runtime.schema import (
    artifacts,
    attempts,
    events,
    metadata,
    runs,
    versions,
    workers,
)
from app.runtime.store import RuntimeStore
from sqlalchemy import select
from tests import test_managed_history as history_fixtures

OBSERVATIONS = []


def snapshot(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return list(db.iterdump())


def backup(source, target):
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


class CrashHistoryQualification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def crash(self, code, *arguments):
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code, *map(str, arguments)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertIn("transaction-write-observed", result.stdout)

    async def test_populated_v1_copy_recovers_after_migration_process_is_killed(self):
        source, target = self.root / "v1.sqlite", self.root / "relocated.sqlite"
        db = Database("sqlite+aiosqlite:///" + str(source))
        remote = {
            "endpoint_ref": "remote-service",
            "task_id": "remote-task",
            "protocol_version": None,
            "unknown_extension": {"中文": ["保留", None]},
        }
        blob = LocalArtifacts(self.root / "objects").put(["原生历史结果🙂".encode()])
        try:
            async with db.transaction("migration", check=False) as c:
                await c.run_sync(metadata.create_all)
                await c.execute(
                    versions.insert().values(version=1, checksum=SCHEMA_CHECKSUM)
                )
            store = RuntimeStore(db, "qualification")
            run = await store.submit(
                "legacy-run", {"unknown_version": None, "remote": remote}
            )
            await store.import_history(
                "legacy", "task", "original", {"未知字段": {"x": None}}
            )
            async with db.transaction("qualification") as c:
                await c.execute(
                    workers.insert().values(
                        id="worker",
                        scope="qualification",
                        instance="instance",
                        host="old-host",
                        pid=100,
                        slots=1,
                        draining=True,
                        heartbeat_at=1,
                    )
                )
                await c.execute(
                    attempts.insert().values(
                        id="attempt",
                        run_id=run["id"],
                        worker_id="worker",
                        worker_instance="instance",
                        epoch=1,
                        authorization_epoch=0,
                        lease_until=0,
                        state="succeeded",
                        session_id="original-session",
                        process_id=101,
                        process_instance="old-process",
                        last_sequence=4,
                        remote=remote,
                        started_at=1,
                        finished_at=2,
                    )
                )
                await c.execute(
                    runs.update()
                    .where(runs.c.id == run["id"])
                    .values(state="succeeded", epoch=1, attempt_id="attempt")
                )
                await c.execute(
                    artifacts.insert().values(
                        id="artifact",
                        run_id=run["id"],
                        attempt_id="attempt",
                        digest=blob.key,
                        size=blob.size,
                        provider="local",
                        storage_key=blob.key,
                        created_at=2,
                    )
                )
        finally:
            await db.close()
        baseline = snapshot(source)
        backup(source, target)
        await self.crash(
            """
import asyncio,os,signal,sys
from sqlalchemy import event
from app.runtime.database import Database
async def main():
 db=Database('sqlite+aiosqlite:///'+sys.argv[1])
 @event.listens_for(db.engine.sync_engine,'after_cursor_execute')
 def terminate(conn,cursor,statement,parameters,context,many):
  if statement.startswith('INSERT INTO runtime_history_schema'):
   print('transaction-write-observed',flush=True)
   os.kill(os.getpid(),signal.SIGKILL)
 await db.migrate()
asyncio.run(main())
""",
            target,
        )
        self.assertEqual(snapshot(source), baseline)
        self.assertEqual(snapshot(target), baseline)
        migrated = Database("sqlite+aiosqlite:///" + str(target))
        try:
            self.assertTrue((await migrated.migrate())["changed"])
            self.assertFalse((await migrated.migrate())["changed"])
            query = HistoryQueries(migrated, "qualification")
            view = await query.run(run["id"])
            self.assertEqual(view["run"]["state"], "succeeded")
            self.assertEqual(view["sessions"][0]["session_id"], "original-session")
            self.assertEqual(view["artifacts"]["items"][0]["id"], "artifact")
            native = await RuntimeStore(migrated, "qualification").get(run["id"])
            self.assertEqual(native["attempts"][0]["remote"], remote)
            self.assertNotIn("capabilities", native["attempts"][0]["remote"])
            self.assertIsNone(native["input"]["unknown_version"])
            imported = await query.imported(
                "legacy", "task", "original", include_record=True
            )
            self.assertEqual(imported["record"]["record"], {"未知字段": {"x": None}})
            self.assertEqual(imported["record"]["acceptance"], "legacy_unverified")
            self.assertEqual(
                LocalArtifacts(self.root / "objects").verify(blob.key).size, blob.size
            )
            OBSERVATIONS.append(
                {
                    "case": "populated-v1-migration-crash",
                    "signal": "SIGKILL",
                    "source_unchanged": True,
                    "rollback_matches_source": True,
                    "migration_retry_idempotent": True,
                    "run_id": run["id"],
                    "attempt_id": "attempt",
                    "session_id": "original-session",
                    "artifact_digest": blob.key,
                    "unknown_fields_preserved": True,
                }
            )
        finally:
            await migrated.close()

    async def test_killed_import_group_retries_with_all_relations_and_no_execution(
        self,
    ):
        path = self.root / "history.sqlite"
        url = "sqlite+aiosqlite:///" + str(path)
        db = Database(url)
        await db.migrate()
        await db.close()
        archive = LocalArtifacts(self.root / "archive")
        destination = LocalArtifacts(self.root / "destination")
        blob = archive.put(["共享中文产物🌏".encode()])
        rows = [
            ("task", "task"),
            ("attempt", "attempt1"),
            ("attempt", "attempt2"),
            ("session", "shared-session"),
            ("artifact", "shared-artifact"),
        ]
        records = [
            {
                "kind": kind,
                "source_id": identity,
                "record": {
                    "protocol_version": None,
                    "extension": {"未知": [None, "保留"]},
                },
                "blobs": [{"digest": blob.key, "size": blob.size}]
                if kind == "artifact"
                else [],
            }
            for kind, identity in rows
        ]
        links = []
        for attempt in ("attempt1", "attempt2"):
            for kind, origin, relation, target_kind, target in [
                ("task", "task", "has_attempt", "attempt", attempt),
                ("attempt", attempt, "uses_session", "session", "shared-session"),
                (
                    "attempt",
                    attempt,
                    "produced_artifact",
                    "artifact",
                    "shared-artifact",
                ),
            ]:
                links.append(
                    {
                        "kind": kind,
                        "source_id": origin,
                        "relation": relation,
                        "target_kind": target_kind,
                        "target_source_id": target,
                    }
                )
        group = {"records": records, "links": links}
        spec = self.root / "group.json"
        spec.write_text(json.dumps(group))
        baseline = snapshot(path)
        await self.crash(
            """
import asyncio,json,os,signal,sys
from pathlib import Path
from sqlalchemy import event
from app.runtime.database import Database
from app.runtime.artifacts import LocalArtifacts
from app.runtime.history_archive import import_group
async def main():
 root=Path(sys.argv[1]);db=Database('sqlite+aiosqlite:///'+str(root/'history.sqlite'))
 @event.listens_for(db.engine.sync_engine,'after_cursor_execute')
 def terminate(conn,cursor,statement,parameters,context,many):
  if statement.startswith('INSERT INTO runtime_history_links'):
   print('transaction-write-observed',flush=True)
   os.kill(os.getpid(),signal.SIGKILL)
 await import_group(db,'qualification','legacy',json.loads((root/'group.json').read_text()),LocalArtifacts(root/'archive'),LocalArtifacts(root/'destination'))
asyncio.run(main())
""",
            self.root,
        )
        self.assertEqual(snapshot(path), baseline)
        db = Database(url)
        try:
            first = await import_group(
                db, "qualification", "legacy", group, archive, destination
            )
            second = await import_group(
                db, "qualification", "legacy", group, archive, destination
            )
            self.assertEqual(first, second)
            query = HistoryQueries(db, "qualification")
            for kind, identity in rows:
                raw = (
                    await query.imported("legacy", kind, identity, include_record=True)
                )["record"]
                self.assertEqual(raw["record"]["metadata"], records[0]["record"])
                self.assertEqual(raw["acceptance"], "legacy_unverified")
            for kind, identity in [
                ("session", "shared-session"),
                ("artifact", "shared-artifact"),
            ]:
                related = await query.related(
                    "legacy", kind, identity, direction="incoming"
                )
                self.assertEqual(
                    {r["peer"]["source_id"] for r in related["items"]},
                    {"attempt1", "attempt2"},
                )
                self.assertTrue(
                    all(r["resolution"] == "resolved" for r in related["items"])
                )
            async with db.read("qualification") as c:
                self.assertEqual((await c.execute(select(runs.c.id))).all(), [])
                self.assertEqual((await c.execute(select(events.c.id))).all(), [])
            self.assertEqual(destination.verify(blob.key).size, blob.size)
            OBSERVATIONS.append(
                {
                    "case": "history-group-crash",
                    "signal": "SIGKILL",
                    "database_rolled_back": True,
                    "idempotent_mappings": first,
                    "relation_count": len(links),
                    "shared_session_and_artifact_origins": 2,
                    "native_runs": 0,
                    "execution_events": 0,
                    "artifact_digest": blob.key,
                }
            )
        finally:
            await db.close()


class SourceExtensionQualification(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = history_fixtures.ManagedHistoryTests.asyncSetUp
    asyncTearDown = history_fixtures.ManagedHistoryTests.asyncTearDown
    seed = history_fixtures.ManagedHistoryTests.seed
    export = history_fixtures.ManagedHistoryTests.export
    apply = history_fixtures.ManagedHistoryTests.apply

    async def test_unknown_source_columns_and_managed_remote_references_survive(self):
        await self.seed()
        extension = json.dumps({"protocol_version": None, "future": {"中文": "原值🙂"}})
        with sqlite3.connect(self.source) as source:
            source.execute(
                "ALTER TABLE managed_task_executions ADD COLUMN future_extension TEXT"
            )
            source.execute(
                "UPDATE managed_task_executions SET future_extension=?, runtime_handle=?",
                (extension, "remote://opaque/original"),
            )
        before = snapshot(self.source)
        bundle, manifest = await self.export()
        await self.apply(bundle, manifest)
        item = (
            await self.query.imported(
                "source-installation", "attempt", "task:task", include_record=True
            )
        )["record"]
        blobs = LocalArtifacts(self.artifacts)
        stored = json.loads(
            b"".join(blobs.read(item["record"]["metadata"]["source_record"]["digest"]))
        )
        self.assertEqual(stored["future_extension"], extension)
        self.assertEqual(stored["runtime_handle"], "remote://opaque/original")
        self.assertEqual(snapshot(self.source), before)
        self.adapter.start.assert_not_awaited()
        OBSERVATIONS.append(
            {
                "case": "managed-source-extension",
                "unknown_column_preserved": True,
                "opaque_runtime_handle_preserved": True,
                "source_unchanged": True,
                "adapter_start_calls": 0,
            }
        )
