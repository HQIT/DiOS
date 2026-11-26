"""R03: an actual 60-second proxy outage while one governed ACP prompt remains live."""

import asyncio
import json
import os
import sys
import time
import unittest
import urllib.request
from dataclasses import asdict, replace

import test_reference_native_acp as acp_fixtures
from work_auth_fixtures import policy_fixture

ROOT = acp_fixtures.ROOT
OBSERVATIONS = []
TOXIPROXY = "ghcr.io/shopify/toxiproxy@sha256:5d3dae09bc185dcc5ff6cf43abf6062a4a720aff0c4306a471e773acdcd107aa"


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1" and os.environ.get("DIOS_TEST_ACP_IMAGE"),
    "isolated ACP and Toxiproxy images required",
)
class NativeNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = acp_fixtures.NativeAcpReferenceTests()
        await self.f.asyncSetUp()
        self.client = self.f.f.adapter.client
        self.resources = []
        self.network = None
        self.addAsyncCleanup(self.cleanup_network)
        self.network = await asyncio.to_thread(
            self.client.networks.create, "dios-network-" + self.f.f.scope
        )
        for name, image, command, ports in (
            (
                "model",
                self.f.f.spec.image,
                ["python", "/qualification/runtime_model_network_fixture.py"],
                {},
            ),
            ("proxy", TOXIPROXY, None, {"8474/tcp": ("127.0.0.1", None)}),
        ):
            container = await asyncio.to_thread(
                self.client.containers.run,
                image,
                command=command,
                name=name + "-" + self.f.f.scope,
                network=self.network.name,
                detach=True,
                ports=ports,
                volumes={
                    str(ROOT / "backend/tests"): {
                        "bind": "/qualification",
                        "mode": "ro",
                    }
                },
                labels={"dios.qualification.scope": self.f.f.scope},
            )
            self.resources.append(container)
        await asyncio.to_thread(self.resources[-1].reload)
        port = self.resources[-1].attrs["NetworkSettings"]["Ports"]["8474/tcp"][0][
            "HostPort"
        ]
        self.api = "http://127.0.0.1:" + port
        async with asyncio.timeout(10):
            while True:
                try:
                    await self.request("/version")
                    break
                except OSError:
                    await asyncio.sleep(0.1)
        self.proxy = {
            "name": "model",
            "listen": "0.0.0.0:8666",
            "upstream": self.resources[0].name + ":8000",
            "enabled": True,
        }
        await self.request("/proxies", self.proxy)
        self.f.f.spec = replace(
            self.f.f.spec,
            network=self.network.name,
            environment={
                **self.f.f.spec.environment,
                "MODEL_FIXTURE_URL": "http://" + self.resources[1].name + ":8666",
            },
        )
        self.f.policy, self.f.authorization = policy_fixture(
            self.f.f.root, "test-profile", self.f.f.spec
        )

    async def cleanup_network(self):
        try:
            for child in self.f.f.children:
                if child.returncode is None:
                    child.kill()
                await child.communicate()
            containers = await asyncio.to_thread(
                self.client.containers.list,
                all=True,
                filters={"label": "dios.runtime.scope=" + self.f.f.scope},
            )
            for container in containers + list(reversed(self.resources)):
                await asyncio.to_thread(container.remove, force=True)
            if self.network:
                await asyncio.to_thread(self.network.remove)
        finally:
            await self.f.f.asyncTearDown()

    async def request(self, path, value=None):
        def send():
            req = urllib.request.Request(
                self.api + path,
                data=json.dumps(value).encode() if value is not None else None,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=3
            ) as response:
                raw = response.read()
                return json.loads(raw) if raw else None

        return await asyncio.to_thread(send)

    async def test_proxy_disconnected_for_sixty_seconds_preserves_attempt_and_output(
        self,
    ):
        run = await self.f.submit("network", "network")
        profile = self.f.f.root / "profile.json"
        profile.write_text(
            json.dumps(
                {
                    "id": "test-profile",
                    "bridge": "jsonl-v1",
                    "governance": {"mode": "e2ag"},
                    "launch": asdict(self.f.f.spec),
                }
            )
        )
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--database-url",
            self.f.f.url,
            "--scope",
            self.f.f.scope,
            "worker",
            "--profile",
            str(profile),
            "--policy-file",
            str(self.f.f.root / "work-policy.json"),
            "--artifacts",
            str(self.f.f.provider.root),
            "--poll-interval",
            ".1",
            "--once",
            env=dict(os.environ, PYTHONPATH=str(ROOT / "backend")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.f.f.children.append(child)
        async with asyncio.timeout(20):
            while True:
                before = await self.f.f.store.get(run["id"])
                if before["attempts"] and before["attempts"][0]["state"] == "streaming":
                    break
                await asyncio.sleep(0.1)
        original = before["attempts"][0]
        await self.request("/proxies/model", {"enabled": False})
        self.assertFalse((await self.request("/proxies/model"))["enabled"])
        disconnected = time.monotonic()
        (self.f.journal / "continue").touch()
        checks = []
        # Real time is part of R03, and no lease or deadline is rewritten.
        for _ in range(6):
            await asyncio.sleep(10)
            self.assertIsNone(child.returncode)
            current = await self.f.f.store.get(run["id"])
            attempt = current["attempts"][0]
            self.assertEqual(len(current["attempts"]), 1)
            for key in (
                "id",
                "worker_id",
                "worker_instance",
                "epoch",
                "session_id",
                "process_instance",
            ):
                self.assertEqual(attempt[key], original[key])
            self.assertGreater(attempt["lease_until"], time.time())
            checks.append(
                {
                    "elapsed": time.monotonic() - disconnected,
                    "state": current["state"],
                    "lease_until": attempt["lease_until"],
                }
            )
        self.assertTrue((self.f.journal / "connection-failures").read_text().strip())
        observer = self.f.worker()
        observer.timeout = 180
        await observer.recover()
        self.assertEqual((await observer.once())["state"], "idle")
        self.assertEqual(len(self.f.prompts()), 1)
        await self.request("/proxies/model", {"enabled": True})
        elapsed = time.monotonic() - disconnected
        self.assertGreaterEqual(elapsed, 60)
        out, err = await asyncio.wait_for(child.communicate(), 20)
        self.assertEqual(child.returncode, 0, err.decode())
        self.assertEqual(json.loads(out)["state"], "succeeded")
        result = await self.f.f.store.get(run["id"])
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(result["attempts"][0]["session_id"], original["session_id"])
        self.assertIn("首段中文🙂".encode(), await self.f.retained(run["id"]))
        self.assertEqual(len(self.f.prompts()), 1)
        grants = await self.f.grant_rows(run["id"])
        self.assertTrue(grants)
        self.assertTrue(all(g["attempt_id"] == original["id"] for g in grants))
        OBSERVATIONS.append(
            {
                "case": "R03",
                "run_id": run["id"],
                "attempt_id": original["id"],
                "session_id": original["session_id"],
                "worker_pid": child.pid,
                "process_instance": original["process_instance"],
                "outage_seconds": elapsed,
                "proxy_image": TOXIPROXY,
                "observations": checks,
                "prompt_calls": 1,
                "attempts": 1,
                "reconciled_before_claim": True,
                "final_state": result["state"],
                "e2ag_grants_verified": True,
                "provider": "synthetic HTTP stream with reconnect support",
            }
        )
