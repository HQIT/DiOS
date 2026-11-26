"""R13: fixed-image Compose cutover and failed-candidate rollback with real ACP tasks."""

import asyncio
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import httpx
from app.runtime.adapter import LaunchSpec, Mount
from app.runtime.database import Database
from app.runtime.store import RuntimeStore
from work_auth_fixtures import policy_fixture

ROOT = Path(__file__).resolve().parents[2]
OBSERVATIONS = []
REQUIRED = (
    "DIOS_TEST_UPGRADE_OLD",
    "DIOS_TEST_UPGRADE_NEW",
    "DIOS_TEST_UPGRADE_BAD",
    "DIOS_TEST_FRONTEND_IMAGE",
    "DIOS_TEST_ACP_IMAGE",
)


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1"
    and all(os.environ.get(k) for k in REQUIRED),
    "explicit isolated old/new/bad/frontend/ACP images required",
)
class NativeInstallationUpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def command(self, *args, check=True, timeout=45):
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if check:
            self.assertEqual(process.returncode, 0, err.decode()[-2000:])
        return process.returncode, out.decode(), err.decode()

    async def compose(self, *args, **kwargs):
        return await self.command(
            "docker", "compose", "--project-directory", str(self.root), *args, **kwargs
        )

    async def api(self, path, value=None, expected=200):
        method = self.http.post if value is not None else self.http.get
        response = await method(
            "/api/os/" + path, **({"json": value} if value is not None else {})
        )
        self.assertEqual(response.status_code, expected, response.text[:200])
        return response.json()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dios-cutover-")
        self.root = Path(self.temp.name) / "install"
        self.children = []
        self.db = None
        self.http = None
        self.addAsyncCleanup(self.cleanup)
        sockets = [socket.socket(), socket.socket()]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        ports = [listener.getsockname()[1] for listener in sockets]
        for listener in sockets:
            listener.close()
        self.name = "dios-cutover-" + uuid4().hex[:12]
        await self.command(
            sys.executable,
            "deploy/linux/install.py",
            "init",
            "--root",
            str(self.root),
            "--name",
            self.name,
            "--backend-image",
            os.environ["DIOS_TEST_UPGRADE_OLD"],
            "--frontend-image",
            os.environ["DIOS_TEST_FRONTEND_IMAGE"],
            "--api-port",
            str(ports[0]),
            "--web-port",
            str(ports[1]),
        )
        self.env = self.environment(".env")
        self.settings = self.environment("service.env")
        self.old_image = self.env["DIOS_BACKEND_IMAGE"]
        _, new, _ = await self.command(
            "docker",
            "image",
            "inspect",
            os.environ["DIOS_TEST_UPGRADE_NEW"],
            "--format",
            "{{.Id}}",
        )
        self.new_image = new.strip()
        self.assertNotEqual(self.old_image, self.new_image)
        await self.compose("run", "--rm", "-T", "migrate")
        await self.compose("up", "-d", "--wait", "backend", "frontend")
        self.http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{ports[0]}",
            trust_env=False,
            timeout=5,
            headers={"X-DiOS-Access-Token": self.settings["DIOS_ACCESS_TOKEN"]},
        )
        self.journal = self.root / "workspace/journal"
        self.journal.mkdir(mode=0o777)
        self.journal.chmod(0o777)
        self.spec = LaunchSpec(
            os.environ["DIOS_TEST_ACP_IMAGE"],
            ("python", "/qualification/runtime_acp_fault_fixture.py", "bridge"),
            environment={
                "PYTHONPATH": "/app",
                "PYTHONDONTWRITEBYTECODE": "1",
                "ACP_EXPECTED_SOURCE_SHA256": hashlib.sha256(
                    (ROOT / "backend/app/runtime/acp_bridge.py").read_bytes()
                ).hexdigest(),
            },
            mounts=(
                Mount(str(ROOT / "backend/tests"), "/qualification"),
                Mount(str(self.journal), "/run/qualification", False),
            ),
        )
        self.policy, self.authorization = policy_fixture(
            self.root / "config", "cutover", self.spec
        )
        self.profile = self.root / "config/profiles/cutover.json"
        self.profile.write_text(
            json.dumps(
                {
                    "id": "cutover",
                    "bridge": "jsonl-v1",
                    "governance": {"mode": "e2ag"},
                    "launch": asdict(self.spec),
                }
            )
        )
        self.db = Database(self.settings["DIOS_RUNTIME_DATABASE_URL"])
        self.store = RuntimeStore(self.db, self.name)

    def environment(self, name):
        return dict(
            line.split("=", 1)
            for line in (self.root / name).read_text().splitlines()
            if line
        )

    async def cleanup(self):
        for process in self.children:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        if self.http:
            await self.http.aclose()
        if self.db:
            await self.db.close()
        if (self.root / "compose.yaml").exists():
            await self.compose("down", "--remove-orphans", check=False)
        # Only task containers created under this installation's unique scope are eligible.
        _, ids, _ = await self.command(
            "docker", "ps", "-aq", "--filter", "label=dios.runtime.scope=" + self.name
        )
        for identity in ids.split():
            await self.command("docker", "rm", "-f", identity, check=False)
        self.temp.cleanup()

    async def submit(self, key):
        return await self.store.submit(
            key,
            {
                "origin": "task-console/v1",
                "runtime_profile": "cutover",
                "profile_digest": self.spec.fingerprint(),
                "secret_refs": [],
                "payload": {"mode": "barrier", "sample": {"text": "只读合成评审"}},
            },
            authorization=self.authorization,
        )

    async def worker(self, *, explicit_scope=True):
        process = await asyncio.create_subprocess_exec(
            "docker",
            "compose",
            "--project-directory",
            str(self.root),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--entrypoint",
            "/usr/local/bin/python",
            "backend",
            "-m",
            "app.runtime.cli",
            *(("--scope", self.name) if explicit_scope else ()),
            "worker",
            "--profile",
            str(self.profile),
            "--policy-file",
            str(self.root / "config/work-policy.json"),
            "--artifacts",
            self.settings["DIOS_RUNTIME_ARTIFACTS_ROOT"],
            "--poll-interval",
            ".1",
            "--once",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.children.append(process)
        return process

    async def collect(self, process):
        out, err = await asyncio.wait_for(process.communicate(), 20)
        self.assertEqual(process.returncode, 0, err.decode()[-2000:])
        return json.loads(out)

    async def wait_stream(self, run):
        async with asyncio.timeout(20):
            while True:
                value = await self.store.get(run["id"])
                self.assertNotIn(
                    value["state"], ("failed", "cancelled"), str(value.get("attempts"))
                )
                if self.children[-1].returncode is not None:
                    self.fail(
                        "Worker exited before streaming: "
                        + str(await self.collect(self.children[-1]))
                    )
                if value["attempts"] and value["attempts"][0]["state"] == "streaming":
                    return value
                await asyncio.sleep(0.1)

    async def switch(self, image, success=True):
        self.env["DIOS_BACKEND_IMAGE"] = image
        (self.root / ".env").write_text(
            "".join(f"{k}={v}\n" for k, v in self.env.items())
        )
        code, _, _ = await self.compose(
            "up", "-d", "--wait", "--wait-timeout", "12", "backend", check=success
        )
        if not success:
            self.assertNotEqual(code, 0)
        else:
            _, actual, _ = await self.compose("images", "-q", "backend")
            _, locked, _ = await self.command(
                "docker", "image", "inspect", actual.strip(), "--format", "{{.Id}}"
            )
            self.assertEqual(locked.strip(), image)

    async def test_drain_cutover_and_failed_image_rollback_preserve_all_task_identities(
        self,
    ):
        first = await self.submit("active")
        process = await self.worker()
        running = await self.wait_stream(first)
        queued = await self.submit("queued")
        drain = await self.api("upgrade/drain", {})
        self.assertFalse(drain["ready_for_cutover"])
        self.assertEqual((await self.collect(await self.worker()))["state"], "idle")
        self.assertIsNone(process.returncode)
        self.assertEqual(
            (await self.store.get(first["id"]))["attempts"][0]["process_instance"],
            running["attempts"][0]["process_instance"],
        )
        self.journal.joinpath("release").touch()
        self.assertEqual((await self.collect(process))["state"], "succeeded")
        self.journal.joinpath("release").unlink()
        settled = await self.api("upgrade/settle-services", {})
        self.assertTrue(settled["ready_for_cutover"])
        stable = await self.store.get(first["id"])
        queued_before = await self.store.get(queued["id"])
        api_before = {
            row["id"]: await self.api("runtime/runs/" + row["id"])
            for row in (stable, queued_before)
        }
        config_hashes = {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (
                self.profile,
                self.root / "config/work-policy.json",
                self.root / "service.env",
            )
        }
        plan = {
            "format": 1,
            "runtime_database_url": self.settings["DIOS_RUNTIME_DATABASE_URL"],
            "service_database_url": self.settings["DIOS_DATABASE_URL"],
            "artifacts": self.settings["DIOS_RUNTIME_ARTIFACTS_ROOT"],
            "configuration": [
                {"id": "profile", "path": str(self.profile)},
                {"id": "policy", "path": str(self.root / "config/work-policy.json")},
            ],
            "workspaces": [{"id": "workspace", "path": str(self.root / "workspace")}],
            "spools": [],
            "external": [],
        }
        plan_path = self.root / "data/backup-plan.json"
        plan_path.write_text(json.dumps(plan))
        backup = str(self.root / "data/cutover-backup")
        await self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-m",
            "app.runtime.cli",
            "installation-backup",
            "--plan",
            str(plan_path),
            "--destination",
            backup,
        )
        await self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-m",
            "app.runtime.cli",
            "installation-verify",
            backup,
        )
        await self.switch(os.environ["DIOS_TEST_UPGRADE_BAD"], success=False)
        await self.switch(self.old_image)
        self.assertEqual((await self.api("upgrade/status"))["mode"], "draining")
        await self.switch(self.new_image)
        after = await self.api("upgrade/status")
        self.assertEqual(after["revision"], settled["revision"])
        self.assertEqual(after["kernel"]["revision"], settled["kernel"]["revision"])
        self.assertTrue(after["ready_for_cutover"])
        for before in (stable, queued_before):
            current = await self.store.get(before["id"])
            for field in (
                "id",
                "input",
                "state",
                "epoch",
                "attempt_id",
                "attempts",
            ):
                self.assertEqual(current[field], before[field], field)
            # Both identities are accessible through the newly deployed HTTP API.
            detail = await self.api("runtime/runs/" + before["id"])
            self.assertEqual(detail["id"], before["id"])
            for field in ("attempts", "artifacts"):
                self.assertEqual(detail[field], api_before[before["id"]][field])
            for artifact in detail["artifacts"]:
                response = await self.http.get(
                    "/api/os/runtime/runs/"
                    + before["id"]
                    + "/artifacts/"
                    + artifact["id"]
                    + "/download"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    hashlib.sha256(response.content).hexdigest(), artifact["digest"]
                )
        self.assertEqual(
            config_hashes,
            {
                name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in config_hashes
            },
        )
        self.assertEqual((await self.collect(await self.worker()))["state"], "idle")
        await self.api(
            "upgrade/resume",
            {
                "revision": after["revision"],
                "kernel_revision": after["kernel"]["revision"],
            },
        )
        resumed_process = await self.worker(explicit_scope=False)
        resumed = await self.wait_stream(queued)
        self.assertEqual(len(resumed["attempts"]), 1)
        self.journal.joinpath("release").touch()
        self.assertEqual((await self.collect(resumed_process))["state"], "succeeded")
        self.assertEqual(
            len((self.journal / "prompts.jsonl").read_text().splitlines()), 2
        )
        self.assertEqual(
            (await self.store.get(first["id"]))["attempts"], stable["attempts"]
        )
        OBSERVATIONS.append(
            {
                "case": "R13",
                "old_image": self.old_image,
                "new_image": self.new_image,
                "failed_candidate": os.environ["DIOS_TEST_UPGRADE_BAD"],
                "runs": [first["id"], queued["id"]],
                "old_attempt": stable["attempts"][0]["id"],
                "new_attempt": resumed["attempts"][0]["id"],
                "old_session": stable["attempts"][0]["session_id"],
                "new_session": resumed["attempts"][0]["session_id"],
                "backup_verified": True,
                "natural_drain": True,
                "failed_image_rolled_back": True,
                "configuration_and_history_preserved": True,
                "queued_identity_preserved": True,
                "dispatch_held_until_explicit_resume": True,
                "worker_uses_installation_scope_environment": True,
                "prompt_calls": 2,
                "final_states": ["succeeded", "succeeded"],
                "model": "synthetic ACP endpoint",
                "e2ag": "enforced",
            }
        )
