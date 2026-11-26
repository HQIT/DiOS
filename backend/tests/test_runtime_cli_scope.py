"""The installed CLI must address the same scope as the API and Worker service."""

import asyncio
import json
import os
import sys
import unittest

import test_runtime_worker as fixtures
from app.runtime.store import RuntimeStore


class InstalledScopeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkerTests.asyncSetUp
    asyncTearDown = fixtures.WorkerTests.asyncTearDown

    async def test_environment_scope_and_explicit_override_keep_queues_separate(self):
        installed = await self.store.submit("same-key", {"source": "installation"})
        local = await RuntimeStore(self.db, "local").submit(
            "same-key", {"source": "local"}
        )

        async def listed(*args, configured=True):
            env = dict(
                os.environ,
                PYTHONPATH=str(fixtures.ROOT / "backend"),
                DIOS_RUNTIME_DATABASE_URL=self.url,
            )
            if configured:
                env["DIOS_RUNTIME_SCOPE"] = self.scope
            else:
                env.pop("DIOS_RUNTIME_SCOPE", None)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.runtime.cli",
                *args,
                "list",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), 10)
            self.assertEqual(process.returncode, 0, err.decode())
            return {row["id"] for row in json.loads(out)}

        self.assertEqual(await listed(), {installed["id"]})
        self.assertEqual(await listed("--scope", "local"), {local["id"]})
        self.assertEqual(await listed(configured=False), {local["id"]})
