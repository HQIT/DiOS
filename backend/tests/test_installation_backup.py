"""Whole-installation capture with real SQLite spools and independent identity models."""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db.database import Base
from app.models.tables import A2ATask, Agent, LLMModel, ManagedTaskExecution
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.spool import ReportSpool
from app.runtime.store import RuntimeStore
from app.services.installation_backup import backup_installation, verify_installation
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class InstallationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "资料.txt").write_text("只读业务资料")
        (self.workspace / "empty").mkdir()
        (self.workspace / "run.sh").write_text("exit 0\n")
        (self.workspace / "run.sh").chmod(0o700)
        self.config = self.root / "profile.json"
        self.config.write_text('{"setting":"配置"}')
        self.url = "sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite")
        self.db = Database(self.url)
        await self.db.migrate()
        self.store = RuntimeStore(self.db, "test")
        self.provider = LocalArtifacts(self.root / "objects")
        self.run = await self.store.submit("native-runtime", {})
        await self.store.register_worker("worker", "instance", "host", os.getpid())
        self.attempt = await self.store.claim("worker", "instance")
        self.spool = ReportSpool(self.root / "worker.sqlite")
        self.spool.enqueue(
            "test",
            self.run["id"],
            self.attempt["id"],
            "worker",
            "instance",
            self.attempt["epoch"],
            1,
            "preparing",
        )
        self.service_url = "sqlite+aiosqlite:///" + str(self.root / "service.sqlite")
        self.service = create_async_engine(self.service_url)
        async with self.service.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(self.service)() as session:
            session.add(
                Agent(
                    id="agent",
                    name="Agent",
                    workspace_path=str(self.workspace),
                    mcp_config_path=str(self.config),
                )
            )
            session.add(
                A2ATask(
                    id="legacy-task", agent_id="agent", status="working", message={}
                )
            )
            session.add(
                ManagedTaskExecution(
                    task_id="legacy-task",
                    scope="test",
                    runtime_handle="native-handle",
                    workspace_path=str(self.workspace),
                    run_id="compatibility-local-id",
                )
            )
            session.add(
                LLMModel(
                    name="private",
                    provider="fixture",
                    model="fixture",
                    base_url="https://example.invalid",
                    api_key="private-fixture-key",
                )
            )
            await session.commit()
        self.plan = {
            "format": 1,
            "runtime_database_url": self.url,
            "service_database_url": self.service_url,
            "artifacts": str(self.provider.root),
            "configuration": [{"id": "profile", "path": str(self.config)}],
            "workspaces": [{"id": "agent", "path": str(self.workspace)}],
            "spools": [{"id": "worker", "path": str(self.spool.path)}],
            "external": [],
        }
        self.bundle = self.root / "bundle"

    async def asyncTearDown(self):
        await self.db.close()
        await self.service.dispose()
        self.temp.cleanup()

    async def backup(self, plan=None):
        return await asyncio.to_thread(
            backup_installation, plan or self.plan, self.bundle
        )

    async def test_full_capture_preserves_native_ids_sensitive_database_and_tree(self):
        value = await self.backup()
        self.assertEqual(verify_installation(self.bundle), value)
        self.assertEqual(
            value["service"]["inventory"]["relations"][
                "managed_task_executions.task_id"
            ],
            1,
        )
        self.assertNotIn("private-fixture-key", json.dumps(value))
        with sqlite3.connect(self.bundle / "service.sqlite") as db:
            self.assertEqual(
                db.execute("SELECT api_key FROM models").fetchone()[0],
                "private-fixture-key",
            )
            self.assertEqual(
                db.execute("SELECT run_id FROM managed_task_executions").fetchone()[0],
                "compatibility-local-id",
            )
        binding = value["spools"]["worker"]["inventory"]["bindings"][0]
        self.assertEqual(binding["run_id"], self.run["id"])
        self.assertEqual(binding["attempt_id"], self.attempt["id"])
        self.assertEqual(
            (self.bundle / "workspaces/agent/资料.txt").read_text(), "只读业务资料"
        )
        self.assertTrue(
            (self.bundle / "workspaces/agent/run.sh").stat().st_mode & 0o100
        )
        self.assertEqual((self.bundle / "service.sqlite").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.spool.status()["retained"], 1)
        self.assertEqual((await self.store.get(self.run["id"]))["state"], "executing")

    async def test_required_workspace_and_configuration_must_be_captured_or_declared(
        self,
    ):
        plan = dict(self.plan, workspaces=[])
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_RESOURCE_UNDECLARED"):
            await self.backup(plan)
        self.assertFalse((self.bundle / "installation.json").exists())
        self.bundle = self.root / "explicit"
        plan["external"] = [
            {
                "kind": "workspace",
                "reference": str(self.workspace),
                "recovery": "restore separately from the named workspace snapshot before resuming",
            }
        ]
        value = await self.backup(plan)
        self.assertTrue(any(r["external_recovery"] for r in value["resource_bindings"]))
        self.assertEqual(verify_installation(self.bundle), value)

    async def test_spool_from_missing_attempt_does_not_publish(self):
        self.spool.enqueue(
            "test",
            self.run["id"],
            "missing-attempt",
            "worker",
            "instance",
            1,
            1,
            "preparing",
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_SPOOL_REFERENCE_INVALID"):
            await self.backup()
        self.assertFalse((self.bundle / "installation.json").exists())

    async def test_local_worker_spool_is_discovered_without_manual_path(self):
        from app.runtime.reporting import report_spool

        local = report_spool(self.provider, "test")
        local.enqueue(
            "test",
            self.run["id"],
            self.attempt["id"],
            "worker",
            "instance",
            self.attempt["epoch"],
            1,
            "preparing",
        )
        value = await self.backup(dict(self.plan, spools=[]))
        self.assertEqual(len(value["spools"]), 1)
        self.assertEqual(
            next(iter(value["spools"].values()))["inventory"]["bindings"][0][
                "attempt_id"
            ],
            self.attempt["id"],
        )
        self.assertEqual(verify_installation(self.bundle), value)

    async def test_corrupt_spool_payload_does_not_publish(self):
        with sqlite3.connect(self.spool.path) as db:
            db.execute("UPDATE reports SET digest=?", ("0" * 64,))
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_SPOOL_DIGEST_INVALID"):
            await self.backup()

    async def test_missing_compatibility_task_is_detected(self):
        with sqlite3.connect(self.root / "service.sqlite") as db:
            db.execute("DELETE FROM a2a_tasks")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "BACKUP_SERVICE_REFERENCE_INVALID"
        ):
            await self.backup()

    async def test_cross_scope_service_receipt_is_rejected(self):
        from app.models.tables import ManagedServiceCall, ManagedServiceResult
        from app.runtime.store import digest

        async with async_sessionmaker(self.service)() as session:
            session.add(
                ManagedServiceCall(
                    id="call", scope="test", agent_id="agent", task_id="legacy-task"
                )
            )
            session.add(
                ManagedServiceResult(
                    call_id="call", scope="other", record={}, digest=digest({})
                )
            )
            await session.commit()
        with self.assertRaisesRegex(
            RuntimeErrorCode, "BACKUP_SERVICE_REFERENCE_INVALID:managed_service_results"
        ):
            await self.backup()

    async def test_symlink_and_destination_inside_source_are_rejected(self):
        outside = self.root / "outside"
        outside.write_text("not part of the selected workspace")
        (self.workspace / "link").symlink_to(outside)
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_TREE_UNSUPPORTED_ENTRY"):
            await self.backup()
        self.assertFalse((self.bundle / "installation.json").exists())
        self.bundle = self.workspace / "recursive-backup"
        with self.assertRaisesRegex(
            RuntimeErrorCode, "BACKUP_DESTINATION_INSIDE_SOURCE"
        ):
            await self.backup()
        self.assertFalse(self.bundle.exists())

    async def test_tree_edit_during_capture_is_detected(self):
        from app.runtime import backup_tree

        write = backup_tree._write

        def changed(path, chunks):
            result = write(path, chunks)
            if path.name == "资料.txt":
                (self.workspace / "资料.txt").write_text("修改中的资料")
            return result

        with (
            patch.object(backup_tree, "_write", side_effect=changed),
            self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_TREE_CHANGED"),
        ):
            await self.backup()

    async def test_tree_tampering_and_missing_spool_are_detected(self):
        await self.backup()
        path = self.bundle / "workspaces/agent/资料.txt"
        path.write_text("changed")
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_TREE_MISMATCH"):
            verify_installation(self.bundle)
        path.write_text("只读业务资料")
        (self.bundle / "spools/worker.sqlite").unlink()
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_FILE_MISSING"):
            verify_installation(self.bundle)

    async def test_budget_and_existing_destination_prevent_publication(self):
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_TREE_LIMIT"):
            await asyncio.to_thread(
                backup_installation, self.plan, self.bundle, max_tree_bytes=1
            )
        self.assertFalse((self.bundle / "installation.json").exists())
        with self.assertRaises(FileExistsError):
            await self.backup()

    async def test_cli_capture_and_verify(self):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(self.plan))

        async def cli(*args):
            p = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.runtime.cli",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await p.communicate()
            self.assertEqual(p.returncode, 0, err.decode())
            return json.loads(out)

        value = await cli(
            "installation-backup",
            "--plan",
            str(plan),
            "--destination",
            str(self.bundle),
        )
        self.assertEqual(await cli("installation-verify", str(self.bundle)), value)


if __name__ == "__main__":
    unittest.main()
