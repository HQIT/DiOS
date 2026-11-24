"""Real API restart and container HTTP stream drain, with synthetic model output."""

import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path

import httpx
from app.config import settings
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.services import managed_runtime
from tests import test_managed_runtime_docker as fixtures

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "explicit Docker qualification required"
)
class NativeUpgradeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedDockerTests.asyncSetUp
    asyncTearDown = fixtures.ManagedDockerTests.asyncTearDown

    async def test_live_stream_drain_restart_and_revision_resume(self):
        await self.recovery.stop()
        self.adapter.client.close()
        self.adapter = DockerRuntimeAdapter(settings.runtime_scope + ":managed")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env = dict(
            os.environ,
            DIOS_DATABASE_URL=str(self.engine.url),
            DIOS_RUNTIME_DATABASE_URL="",
            DIOS_RUNTIME_SCOPE=settings.runtime_scope,
            DIOS_WORKSPACE_ROOT=str(self.root),
            DIOS_ACCESS_TOKEN="qualification-operator",
            DIOS_RUNTIME_CONSOLE_ENABLED="false",
            DIOS_CONNECTOR_POLL_ENABLED="false",
            DIOS_MANAGED_RUNTIME_LOCK_DIR=str(self.root / "locks"),
            DIOS_MANAGED_RUNTIME_USER=f"{os.getuid()}:{os.getgid()}",
            DIOS_DIAGENT_SERVICE_IMAGE="dios-upgrade-fixture:local",
            DIOS_DOCKER_NETWORK="bridge",
            DIOS_HOST_WORKSPACE_ROOT="",
            DIOS_HOST_SHARED_CLI_ROOT="",
        )
        children = []
        log = (self.root / "upgrade-api.log").open("w")
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            timeout=20,
            headers={"Authorization": "Bearer qualification-operator"},
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
                            (self.root / "upgrade-api.log").read_text()[-3000:],
                        )
                        try:
                            if (await client.get("/health")).status_code == 200:
                                return child
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)

            async def status():
                response = await client.get("/api/os/upgrade/status")
                response.raise_for_status()
                return response.json()

            try:
                first = await start()
                payload = {
                    "agent_id": "agent",
                    "messages": [{"role": "user", "content": "fixture"}],
                }
                async with client.stream(
                    "POST", "/api/apps/chat/completions", json=payload
                ) as response:
                    response.raise_for_status()
                    lines = response.aiter_lines()
                    self.assertIn("fixture", await anext(lines))
                    handle = managed_runtime.handle("service", "agent")
                    original = await self.adapter.observe(handle)
                    self.assertTrue(original.alive)
                    drained = await client.post("/api/os/upgrade/drain")
                    drained.raise_for_status()
                    self.assertEqual(drained.json()["active_service_calls"], 1)
                    self.assertFalse(drained.json()["ready_for_cutover"])
                    rejected = await client.post(
                        "/api/apps/chat/completions", json=payload
                    )
                    self.assertEqual(rejected.status_code, 503)
                    self.assertEqual(
                        (
                            await client.post("/api/os/upgrade/settle-services")
                        ).status_code,
                        409,
                    )
                    self.assertEqual(
                        (await self.adapter.observe(handle)).process_instance,
                        original.process_instance,
                    )
                    (self.root / "agent/release").touch()
                    output = [line async for line in lines]
                    self.assertIn("data: [DONE]", output)
                async with asyncio.timeout(10):
                    while not (await status())["ready_to_stop_services"]:
                        await asyncio.sleep(0.1)
                settled = await client.post("/api/os/upgrade/settle-services")
                settled.raise_for_status()
                self.assertTrue(settled.json()["ready_for_cutover"])
                self.assertEqual((await self.adapter.observe(handle)).state, "absent")
                self.assertEqual(
                    len(list((self.root / "managed-runtime-artifacts").glob("*/*"))), 1
                )
                first.terminate()
                await asyncio.wait_for(first.wait(), 15)
                second = await start()
                snapshot = await status()
                self.assertEqual(snapshot["revision"], drained.json()["revision"])
                self.assertTrue(snapshot["ready_for_cutover"])
                self.assertEqual(
                    (
                        await client.post("/api/apps/chat/completions", json=payload)
                    ).status_code,
                    503,
                )
                self.assertEqual(
                    (
                        await client.post(
                            "/api/os/upgrade/resume", json={"revision": 999}
                        )
                    ).status_code,
                    409,
                )
                resumed = await client.post(
                    "/api/os/upgrade/resume", json={"revision": snapshot["revision"]}
                )
                resumed.raise_for_status()
                self.assertEqual(resumed.json()["mode"], "running")
                OBSERVATIONS.append(
                    {
                        "api_pid_before": first.pid,
                        "api_pid_after": second.pid,
                        "stream_active_during_drain": True,
                        "new_request_rejected": True,
                        "original_process_preserved_until_done": True,
                        "idle_service_logs_retained": True,
                        "drain_persisted_after_restart": True,
                        "stale_resume_rejected": True,
                    }
                )
            finally:
                for child in children:
                    if child.returncode is None:
                        child.terminate()
                    await asyncio.wait_for(child.wait(), 15)
                log.close()
