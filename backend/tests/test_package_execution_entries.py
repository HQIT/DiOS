"""Locked packages cannot use legacy unversioned execution entry points."""

import asyncio
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

import test_runtime_capacity as fixtures
import test_runtime_task_console as console_fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.package_lock import resolve
from app.runtime.package_payloads import tree_descriptor
from app.runtime.revisions import Revisions
from app.runtime.schema import runs
from app.runtime.worker import read_profile
from test_execution_revisions import profile
from test_package_lock import package, requirement
from work_auth_fixtures import policy_fixture


def locked(raw):
    lock = resolve([package()], [requirement("agent")])
    return dict(raw, package_lock=lock, packaged_mounts=[tree_descriptor(lock)])


class PackageEntryTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def test_cli_profile_cannot_enqueue_locked_package_without_revision(self):
        raw = locked(profile(mode="e2ag"))
        path = self.root / "locked.json"
        path.write_text(json.dumps(raw))
        saved = await Revisions(self.db, self.scope).create(
            "candidate", compile_profile(raw), actor="operator"
        )
        _, name, spec, _, _ = await load_profile(
            self.db, self.scope, saved["revision"]["id"]
        )
        policy, _ = policy_fixture(self.root, name, spec)
        result = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--database-url",
                self.url,
                "--scope",
                self.scope,
                "enqueue",
                "--profile",
                str(path),
                "--policy-file",
                str(policy.path),
                "--key",
                "no-revision",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(
            json.loads(result.stdout), {"error": "PACKAGE_REVISION_REQUIRED"}
        )
        async with self.db.read(self.scope) as connection:
            self.assertEqual(
                (
                    await connection.execute(
                        select(runs.c.id).where(runs.c.scope == self.scope)
                    )
                ).all(),
                [],
            )

    async def test_worker_profile_rejected_before_adapter_or_remote_channel(self):
        from app.runtime.remote_runtime import serve_remote

        raw = locked(profile(mode="e2ag"))
        path = self.root / "locked.json"
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(RuntimeErrorCode, "PACKAGE_REVISION_REQUIRED"):
            read_profile(path)
        with patch("app.runtime.remote_tool_channel.RemoteToolProvider") as channel:
            with self.assertRaisesRegex(RuntimeErrorCode, "PACKAGE_REVISION_REQUIRED"):
                await serve_remote(
                    SimpleNamespace(revision_file=None, profile=str(path))
                )
            channel.assert_not_called()


class ConsolePackageEntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = console_fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = console_fixtures.TaskConsoleTests.asyncTearDown

    async def test_inline_package_rejected_and_revision_pointer_remains_supported(self):
        raw = locked(self.profile)
        raw["launch"]["image"] = "sha256:" + "1" * 64
        path = self.profiles / "review.json"
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(RuntimeErrorCode, "PACKAGE_REVISION_REQUIRED"):
            await self.console.catalog()
        from app.runtime.execution_revisions import freeze_console

        freeze_console(raw)
        saved = await Revisions(self.db, "one").create(
            "published", compile_profile(raw), actor="operator"
        )
        path.write_text(
            json.dumps(
                {
                    "id": "review",
                    "revision_ref": saved["revision"]["id"],
                    "enabled": True,
                }
            )
        )
        rows = await self.console.catalog()
        self.assertEqual(rows[0]["revision_ref"], saved["revision"]["id"])
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_AUTHORIZATION_REQUIRED"):
            await self.console.submit(
                "review", "first-item", "reject ungoverned package", "no-authorization"
            )
