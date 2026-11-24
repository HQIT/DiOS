"""Opt-in real container qualification; isolated scope, database and workspace."""

import asyncio
import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from app.config import settings
from app.models.tables import A2ATask, E2AGToolGrant
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.services import a2a_service, agent_runtime, managed_runtime
from tests import test_managed_runtime as fixtures


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "explicit Docker qualification required"
)
class ManagedDockerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.ManagedRuntimeTests.asyncSetUp(self)
        self.stack.enter_context(
            patch.object(
                settings, "runtime_scope", "managed-qualification-" + uuid4().hex
            )
        )
        self.stack.enter_context(
            patch.object(
                settings, "managed_runtime_user", f"{os.getuid()}:{os.getgid()}"
            )
        )
        self.stack.enter_context(patch.object(settings, "docker_network", "bridge"))
        self.stack.enter_context(
            patch.object(settings, "diagent_image", "dios-worker-fixture:local")
        )
        self.stack.enter_context(
            patch.object(
                settings, "diagent_service_image", "dios-diagent-service:latest"
            )
        )
        self.adapter = DockerRuntimeAdapter(settings.runtime_scope)
        self.stack.enter_context(
            patch(
                "app.services.managed_runtime.managed_adapter",
                return_value=self.adapter,
            )
        )
        (self.root / "skills").mkdir()

    async def asyncTearDown(self):
        containers = await asyncio.to_thread(
            self.adapter.client.containers.list,
            all=True,
            filters={"label": "dios.runtime.scope=" + self.adapter.scope},
        )
        for container in containers:
            await asyncio.to_thread(container.remove, force=True)
        self.adapter.client.close()
        await fixtures.ManagedRuntimeTests.asyncTearDown(self)

    async def test_native_diagent_service_health_reuse_stop_and_log_retention(self):
        async with self.sessions() as db:
            url = await agent_runtime.ensure_running("agent", db)
            first = await self.adapter.observe(
                managed_runtime.handle("service", "agent")
            )
            self.assertTrue(first.alive)
            self.assertTrue(url.startswith("http://"))
            self.assertEqual(url, await agent_runtime.ensure_running("agent", db))
            second = await self.adapter.observe(
                managed_runtime.handle("service", "agent")
            )
            self.assertEqual(first.process_instance, second.process_instance)
            await agent_runtime.stop_agent("agent", db)
        self.assertEqual(
            "absent",
            (
                await self.adapter.observe(managed_runtime.handle("service", "agent"))
            ).state,
        )
        self.assertEqual(
            1, len(list((self.root / "managed-runtime-artifacts").glob("*/*")))
        )

    async def test_native_running_task_cancel_confirms_exit_and_retains_output(self):
        await fixtures.ManagedRuntimeTests.task(self)
        async with self.sessions() as db:
            db.add(
                E2AGToolGrant(
                    id="qualification-grant",
                    token_hash="0" * 64,
                    trace_id="fixture-trace",
                    task_id="task",
                    agent_id="agent",
                    mcp_server_id="fixture-only",
                    allowed_tools=["read"],
                    status="active",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await db.commit()
        runtime_handle = await managed_runtime.start_task(
            "task",
            "task",
            self.root / "agent",
            {"DIOS_WORK_INPUT": json.dumps({"delay": 30})},
        )
        self.assertTrue((await self.adapter.observe(runtime_handle)).alive)
        await asyncio.sleep(0.3)
        async with self.sessions() as db:
            self.assertEqual(
                "canceled", (await a2a_service.cancel_task(db, "task")).status
            )
            self.assertEqual(
                "revoked", (await db.get(E2AGToolGrant, "qualification-grant")).status
            )
        self.assertEqual("absent", (await self.adapter.observe(runtime_handle)).state)
        logs = list((self.root / "managed-runtime-artifacts").glob("*/*"))
        self.assertEqual(1, len(logs))
        self.assertIn("session", json.loads(logs[0].read_text())["stdout"])

    async def test_native_a2a_task_without_report_is_failed_and_cleaned(self):
        async with self.sessions() as db:
            task = await a2a_service.send_message(
                db,
                "agent",
                {"role": "user", "parts": [{"kind": "text", "text": "fixture only"}]},
            )
            task_id = task.id
        async with asyncio.timeout(30):
            while True:
                async with self.sessions() as db:
                    current = await db.get(A2ATask, task_id)
                    if current.status == "failed":
                        self.assertEqual("RUNTIME_RESULT_MISSING", current.error)
                        break
                await asyncio.sleep(0.1)
        async with asyncio.timeout(10):
            while (
                await self.adapter.observe(managed_runtime.handle("task", task_id))
            ).state != "absent":
                await asyncio.sleep(0.1)
        self.assertEqual(
            1, len(list((self.root / "managed-runtime-artifacts").glob("*/*")))
        )


if __name__ == "__main__":
    unittest.main()
