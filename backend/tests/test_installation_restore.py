"""Clean installation restoration and rejection at actual dispatch entry points."""

import asyncio
import json
import sqlite3
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import test_installation_backup as fixtures
from app.config import settings
from app.models.tables import (
    Agent,
    E2AGToolGrant,
    ManagedRuntimeControl,
    ManagedServiceCall,
)
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.lifecycle import Lifecycle
from app.runtime.recovery import _file_info
from app.runtime.spool import ReportSpool
from app.runtime.store import RuntimeStore
from app.services import (
    a2a_service,
    agent_runtime,
    e2ag_tool_gateway,
    managed_admission,
    managed_recovery,
    managed_service_recovery,
)
from app.services.installation_restore import restore_installation
from app.services.managed_upgrade import Upgrade
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class InstallationRestoreTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InstallationTests.asyncSetUp
    asyncTearDown = fixtures.InstallationTests.asyncTearDown
    backup = fixtures.InstallationTests.backup

    async def restore(self):
        self.target = self.root / "restored"
        return await restore_installation(self.bundle, self.target)

    async def test_preserve_identity_relocate_files_and_quarantine_pending_report(self):
        before = await self.backup()
        source = _file_info(self.bundle / "installation.json")
        result = await self.restore()
        self.assertEqual(_file_info(self.bundle / "installation.json"), source)
        self.assertEqual(
            _file_info(self.bundle / "service.sqlite"), before["service"]["database"]
        )
        self.assertEqual(result["mode"], "restore_hold")
        self.assertEqual(result["service"]["relocated_paths"], 3)
        self.assertEqual(
            (self.target / "workspaces/agent/资料.txt").read_text(), "只读业务资料"
        )
        self.assertEqual(
            (self.target / "runtime/configuration/profile").read_bytes(),
            self.config.read_bytes(),
        )
        self.assertEqual(
            ReportSpool.inspect(self.target / "spools/worker.sqlite")["retained"], 1
        )
        self.assertFalse(list((self.target / "runtime/objects").glob(".reports-*")))
        with sqlite3.connect(self.target / "service.sqlite") as db:
            self.assertEqual(
                db.execute("select run_id from managed_task_executions").fetchone()[0],
                "compatibility-local-id",
            )
            self.assertEqual(
                db.execute("select workspace_path from agents").fetchone()[0],
                str(self.target / "workspaces/agent"),
            )
            self.assertEqual(
                db.execute("select status from a2a_tasks").fetchone()[0], "working"
            )
        db = Database(
            "sqlite+aiosqlite:///" + str(self.target / "runtime/state.sqlite")
        )
        try:
            restored = RuntimeStore(db, "test")
            original = await self.store.get(self.run["id"])
            recovered = await restored.get(self.run["id"])
            original.pop("observed_at")
            recovered.pop("observed_at")
            self.assertEqual(recovered, original)
            binding = (
                self.run["id"],
                self.attempt["id"],
                "worker",
                "instance",
                self.attempt["epoch"],
            )
            for scope in ("test", "new-scope"):
                self.assertEqual(
                    (await Lifecycle(db, scope).status())["mode"], "restore_hold"
                )
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                ):
                    await RuntimeStore(db, scope).submit("new", {})
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
            ):
                await restored.heartbeat(*binding)
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
            ):
                await restored.report(*binding, 1, "preparing")
            with self.assertRaisesRegex(
                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
            ):
                await Lifecycle(db, "test").resume(0)
        finally:
            await db.close()
        self.assertEqual(
            (await self.store.submit("source-continues", {}))["state"], "queued"
        )

    async def test_all_compatibility_scopes_old_tasks_tools_and_stop_are_held(self):
        async with async_sessionmaker(self.service)() as db:
            db.add(
                E2AGToolGrant(
                    id="grant",
                    token_hash=e2ag_tool_gateway.hash_grant_token("old-token"),
                    trace_id="trace",
                    task_id="legacy-task",
                    agent_id="agent",
                    mcp_server_id="server",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            db.add(
                ManagedServiceCall(
                    id="call", scope="test", agent_id="agent", task_id="legacy-task"
                )
            )
            # A pre-existing normal drain must not provide an alternate release path.
            db.add(ManagedRuntimeControl(scope="test", mode="draining", revision=7))
            await db.commit()
        await self.backup()
        receipt = await self.restore()
        self.assertEqual(receipt["service"]["revoked_tool_grants"], 1)
        engine = create_async_engine(
            "sqlite+aiosqlite:///" + str(self.target / "service.sqlite")
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            with (
                patch("app.db.database.async_session", sessions),
                patch.object(settings, "runtime_database_url", ""),
            ):
                for scope in ("test", "new-scope"):
                    with patch.object(settings, "runtime_scope", scope):
                        self.assertEqual(
                            (await Upgrade().status())["mode"], "restore_hold"
                        )
                        for operation in (
                            Upgrade().drain(),
                            Upgrade().resume(7),
                            managed_admission.dispatch("call"),
                            managed_recovery.reconcile("legacy-task"),
                            managed_service_recovery.reconcile("call"),
                        ):
                            with self.assertRaisesRegex(
                                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                            ):
                                await operation
                        async with sessions() as db:
                            with self.assertRaisesRegex(
                                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                            ):
                                async with managed_admission.gate(
                                    db, admitted_task_id="legacy-task"
                                ):
                                    self.fail("restored task admitted")
                            for operation in (
                                managed_admission.validate_call(db, "call", "agent"),
                                a2a_service.cancel_task(db, "legacy-task"),
                                agent_runtime.stop_agent("agent", db),
                                agent_runtime.ensure_running(
                                    "agent", db, call_id="call"
                                ),
                            ):
                                with self.assertRaisesRegex(
                                    RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                                ):
                                    await operation
                            grant, reason = await e2ag_tool_gateway.validate_grant(
                                db, "grant", "old-token"
                            )
                            self.assertIsNone(grant)
                            self.assertEqual(reason, "RESTORE_RECONCILIATION_REQUIRED")
                            agent = await db.get(Agent, "agent")
                            with self.assertRaisesRegex(
                                RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                            ):
                                await e2ag_tool_gateway.issue_task_mcp_config(
                                    db, agent=agent, task=None, servers=[]
                                )
                async with sessions() as db:
                    self.assertEqual(
                        (await db.get(E2AGToolGrant, "grant")).status, "revoked"
                    )
        finally:
            await engine.dispose()
        async with async_sessionmaker(self.service)() as db:
            self.assertEqual((await db.get(E2AGToolGrant, "grant")).status, "active")

    async def test_corruption_and_existing_destination_never_publish(self):
        await self.backup()
        target = self.root / "existing"
        target.mkdir()
        (target / "sentinel").write_text("keep")
        with self.assertRaises(FileExistsError):
            await restore_installation(self.bundle, target)
        self.assertEqual((target / "sentinel").read_text(), "keep")
        (self.bundle / "workspaces/agent/资料.txt").write_text("corrupt")
        with self.assertRaisesRegex(RuntimeErrorCode, "BACKUP_TREE_MISMATCH"):
            await restore_installation(self.bundle, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    async def test_failed_runtime_migration_has_no_service_or_installation_publication(
        self,
    ):
        await self.backup()
        with (
            patch(
                "app.runtime.recovery.Database.migrate",
                side_effect=RuntimeError("migration failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "migration failed"),
        ):
            await self.restore()
        self.assertFalse((self.target / "service.sqlite").exists())
        self.assertFalse((self.target / "installation-restore.json").exists())
        self.assertFalse((self.target / "runtime/state.sqlite").exists())

    async def test_cli_restores_without_source_paths_and_does_not_overwrite(self):
        await self.backup()
        # The fresh process has only bundle paths, not an active application configuration.
        target = self.root / "cli-restored"
        cli = Path(__file__).resolve().parents[2] / "cli/dios"
        command = [
            sys.executable,
            str(cli),
            "runtime",
            "installation-restore",
            str(self.bundle),
            str(target),
        ]
        p = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(p.communicate(), 30)
        self.assertEqual(p.returncode, 0, err.decode())
        self.assertEqual(json.loads(out)["mode"], "restore_hold")
        self.assertNotIn("private-fixture-key", out.decode())
        p = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(p.communicate(), 30)
        self.assertNotEqual(p.returncode, 0)


class RestoredCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_issued_credential_is_revoked_without_deleting_history(self):
        from app.runtime.credential_schema import credentials
        from app.runtime.recovery import backup_sqlite, restore_sqlite
        from app.runtime.worker_credentials import WorkerCredentials
        from test_runtime_worker_credentials import WorkerCredentialTests

        fixture = WorkerCredentialTests()
        await fixture.asyncSetUp()
        try:
            envelope, issued = await fixture.issue()
            f = fixture.f
            bundle, target = f.root / "backup", f.root / "restored"
            await asyncio.to_thread(backup_sqlite, f.url, f.provider.root, bundle)
            await restore_sqlite(bundle, target)
            db = Database("sqlite+aiosqlite:///" + str(target / "state.sqlite"))
            try:
                store = RuntimeStore(db, f.scope)
                service = WorkerCredentials(
                    store, fixture.fixture.identities, f.policy, fixture.key
                )
                async with db.read(f.scope) as c:
                    row = (
                        (
                            await c.execute(
                                select(credentials).where(
                                    credentials.c.id == issued["credential_id"]
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                self.assertEqual(row["state"], "revoked")
                self.assertEqual(row["version"], issued["version"] + 1)
                self.assertEqual(row["run_id"], f.run["id"])
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                ):
                    await service.authorize(
                        fixture.fixture.cert,
                        time.time() + 3600,
                        fixture.envelope("describe", {}, issued["access_token"]),
                    )
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
                ):
                    await service.exchange(
                        fixture.fixture.cert, time.time() + 3600, envelope
                    )
                # Still usable only on the separate original instance.
                await fixture.service.authorize(
                    fixture.fixture.cert,
                    time.time() + 3600,
                    fixture.envelope("describe", {}, issued["access_token"]),
                )
            finally:
                await db.close()
        finally:
            await fixture.asyncTearDown()
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
