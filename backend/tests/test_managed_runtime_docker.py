"""Opt-in real container qualification; isolated scope, database and workspace."""

import asyncio
import json
import os
import socket
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from app.config import settings
from app.models.tables import A2ATask, Agent, E2AGToolGrant, ManagedTaskExecution
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.services import a2a_service, agent_runtime, managed_runtime
from app.services.managed_recovery import ManagedRecovery
from tests import test_managed_runtime as fixtures

RESTART_OBSERVATIONS = []


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
        self.recovery = ManagedRecovery(interval=0.05)
        self.recovery.start()

    async def asyncTearDown(self):
        await self.recovery.stop()
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

    async def test_api_process_restart_recovers_same_container_and_report(self):
        await self.recovery.stop()
        self.adapter.client.close()
        self.adapter = DockerRuntimeAdapter(settings.runtime_scope + ":managed")
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.env = {"DIOS_WORK_INPUT": json.dumps({"delay": 5})}
            await db.commit()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
            DIOS_WORKSPACE_ROOT=str(self.root),
            DIOS_ACCESS_TOKEN="",
            DIOS_RUNTIME_CONSOLE_ENABLED="false",
            DIOS_CONNECTOR_POLL_ENABLED="false",
            DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
            DIOS_MANAGED_RUNTIME_USER=f"{os.getuid()}:{os.getgid()}",
            DIOS_DIAGENT_IMAGE="dios-worker-fixture:local",
            DIOS_DOCKER_NETWORK="bridge",
            DIOS_HOST_WORKSPACE_ROOT="",
            DIOS_HOST_SHARED_CLI_ROOT="",
        )
        children = []
        log = (self.root / "api-process.log").open("w")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=15
        ) as client:

            async def start():
                child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    cwd=Path(__file__).parents[1],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                children.append(child)
                async with asyncio.timeout(20):
                    while True:
                        self.assertIsNone(
                            child.returncode,
                            (self.root / "api-process.log").read_text()[-3000:],
                        )
                        try:
                            if (await client.get("/health")).status_code == 200:
                                return child
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)

            try:
                first_api = await start()
                response = await client.post(
                    "/api/os/a2a/agents/agent",
                    json={
                        "jsonrpc": "2.0",
                        "id": "fixture",
                        "method": "message/send",
                        "params": {
                            "message": {
                                "role": "user",
                                "parts": [{"kind": "text", "text": "fixture"}],
                            }
                        },
                    },
                )
                response.raise_for_status()
                task_id = response.json()["result"]["id"]
                handle = managed_runtime.handle("task", task_id)
                original = await self.adapter.observe(handle)
                self.assertTrue(original.alive)
                async with self.sessions() as db:
                    self.assertEqual(
                        (await db.get(ManagedTaskExecution, task_id)).process_instance,
                        original.process_instance,
                    )
                first_api.kill()
                await first_api.wait()
                self.assertTrue((await self.adapter.observe(handle)).alive)
                self.assertEqual(
                    (await self.adapter.observe(handle)).process_instance,
                    original.process_instance,
                )
                report = (
                    self.root / "agent/output/events" / task_id[:12] / "task_result.md"
                )
                report.write_text(
                    "# Native lifecycle fixture\n\nEvidence: fixture:source",
                    encoding="utf-8",
                )
                second_api = await start()
                self.assertNotEqual(second_api.pid, first_api.pid)
                async with asyncio.timeout(25):
                    while True:
                        async with self.sessions() as db:
                            row = await db.get(ManagedTaskExecution, task_id)
                            if row and row.finished:
                                self.assertEqual(
                                    row.process_instance, original.process_instance
                                )
                                self.assertEqual(
                                    (await db.get(A2ATask, task_id)).status, "completed"
                                )
                                break
                        await asyncio.sleep(0.1)
                self.assertEqual((await self.adapter.observe(handle)).state, "absent")
                response = await client.get("/api/os/a2a/runtime/recovery")
                self.assertTrue(response.json()["executions_settled"])
                self.assertEqual(
                    len(list((self.root / "managed-runtime-artifacts").glob("*/*"))), 1
                )
                RESTART_OBSERVATIONS.append(
                    {
                        "task_id": task_id,
                        "api_pid_before": first_api.pid,
                        "api_pid_after": second_api.pid,
                        "process_instance": original.process_instance,
                        "container_survived_api_loss": True,
                        "same_task_completed": True,
                        "container_removed_after_completion": True,
                        "retained_log_artifacts": 1,
                    }
                )
            finally:
                for child in children:
                    if child.returncode is None:
                        child.terminate()
                    await asyncio.wait_for(child.wait(), 15)
                log.close()


if __name__ == "__main__":
    unittest.main()
