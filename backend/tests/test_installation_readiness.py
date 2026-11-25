"""Recovery observation must remain read-only and never imply authority."""

import asyncio
import json
import sqlite3
import sys
import unittest
from pathlib import Path

import test_installation_backup as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.recovery import _file_info
from app.services.installation_readiness import inspect_restore
from app.services.installation_restore import restore_installation


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InstallationTests.asyncSetUp
    asyncTearDown = fixtures.InstallationTests.asyncTearDown
    backup = fixtures.InstallationTests.backup

    async def restored(self):
        await self.backup()
        self.target = self.root / "restored"
        await restore_installation(self.bundle, self.target)

    async def test_active_history_and_spools_are_visible_without_writes_or_secrets(
        self,
    ):
        await self.restored()
        paths = [
            self.target / "service.sqlite",
            self.target / "runtime/state.sqlite",
            self.target / "spools/worker.sqlite",
        ]
        before = [_file_info(p) for p in paths]
        value = inspect_restore(self.target)
        self.assertEqual(before, [_file_info(p) for p in paths])
        self.assertEqual(value["holds"], {"runtime": True, "service": True})
        self.assertEqual(value["observations"]["unsettled_attempts"]["count"], 1)
        self.assertEqual(
            value["observations"]["unfinished_tasks"]["sample"][0]["id"], "legacy-task"
        )
        self.assertEqual(value["observations"]["spool:worker"]["count"], 1)
        self.assertFalse(value["local_execution_settled"])
        self.assertFalse(value["ready_to_activate"])
        self.assertNotIn("private-fixture-key", json.dumps(value))

    async def test_no_local_work_does_not_prove_source_isolation(self):
        await self.restored()
        # Build a settled recovery fixture; production uses the result reconciliation API.
        with sqlite3.connect(self.target / "runtime/state.sqlite") as db:
            db.execute("UPDATE runtime_attempts SET state='abandoned'")
            db.execute("UPDATE runtime_runs SET state='queued'")
        with sqlite3.connect(self.target / "service.sqlite") as db:
            db.execute("UPDATE a2a_tasks SET status='completed'")
            db.execute("UPDATE managed_task_executions SET finished=1")
        with sqlite3.connect(self.target / "spools/worker.sqlite") as db:
            db.execute("DELETE FROM reports")
        value = inspect_restore(self.target)
        self.assertTrue(value["local_execution_settled"])
        self.assertFalse(value["ready_to_activate"])
        self.assertIn(
            "source-controller-and-executor-isolation",
            value["requires_external_verification"],
        )
        with sqlite3.connect(self.target / "runtime/state.sqlite") as db:
            db.execute("UPDATE runtime_lifecycle SET mode='running' WHERE key='global'")
        self.assertFalse(inspect_restore(self.target)["local_execution_settled"])

    async def test_all_scopes_bounded_output_and_missing_spool(self):
        await self.restored()
        with sqlite3.connect(self.target / "service.sqlite") as db:
            db.execute("UPDATE managed_task_executions SET scope='another-scope'")
            db.execute(
                "INSERT INTO a2a_tasks SELECT 'second',agent_id,context_id,trace_id,status,message,artifacts,error,created_at,updated_at FROM a2a_tasks WHERE id='legacy-task'"
            )
        value = inspect_restore(self.target, sample_limit=1)
        self.assertEqual(value["observations"]["unfinished_tasks"]["count"], 2)
        self.assertTrue(value["observations"]["unfinished_tasks"]["truncated"])
        self.assertEqual(
            value["observations"]["unfinished_executions"]["sample"][0]["scope"],
            "another-scope",
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_RECOVERY_SAMPLE_LIMIT"):
            inspect_restore(self.target, sample_limit=0)
        (self.target / "spools/worker.sqlite").unlink()
        with self.assertRaisesRegex(RuntimeErrorCode, "RESTORE_SPOOL_MISSING"):
            inspect_restore(self.target)

    async def test_cli_observes_without_database_configuration(self):
        await self.restored()
        cli = Path(__file__).resolve().parents[2] / "cli/dios"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(cli),
            "runtime",
            "installation-readiness",
            str(self.target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, err.decode())
        self.assertFalse(json.loads(out)["ready_to_activate"])


if __name__ == "__main__":
    unittest.main()
