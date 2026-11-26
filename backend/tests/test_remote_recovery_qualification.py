"""Native mTLS Workers: single probe, independent account, restored concurrency."""

import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace

from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.work_auth_schema import grants
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import read_profile
from app.runtime.worker_identity import WorkerIdentities, tls_context
from sqlalchemy import select
from test_runtime_worker import ROOT, WorkerTests

OBSERVATIONS = []


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "isolated Docker required"
)
class RemoteRecoveryQualificationTests(unittest.IsolatedAsyncioTestCase):
    fixture_type = WorkerTests

    async def asyncSetUp(self):
        self.fixture = self.fixture_type()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.root = self.fixture.root
        self.store = self.fixture.store
        self.adapter = DockerRuntimeAdapter(self.store.scope)
        self.children = []
        self.stop = asyncio.Event()
        self.gateway_task = None
        self.addAsyncCleanup(self.cleanup_native)
        self.control = self.root / "control"
        self.control.mkdir(mode=0o755)
        image = await asyncio.to_thread(
            self.adapter.client.images.get,
            getattr(self, "workload_image", "dios-worker-fixture:local"),
        )
        self.image_id = image.id
        self.profiles = {}
        policy_profiles = {}
        registry = {}
        for profile in ("account-a", "account-b"):
            value = json.loads(
                (ROOT / "examples/runtime/fixture-profile.json").read_text()
            )
            script = (
                "import json, pathlib, runpy, sys, uuid\n"
                "outage=pathlib.Path('/run/fixture/outage')\n"
                f"if {profile!r} == 'account-a' and outage.exists():\n"
                " print(json.dumps({'type':'session','session_id':str(uuid.uuid4())}),flush=True)\n"
                " print(json.dumps({'type':'error','code':outage.read_text()}),flush=True)\n"
                " sys.exit(7)\n"
                "runpy.run_path('/opt/dios/fixture_agent.py',run_name='__main__')\n"
            )
            value.update(
                id=profile,
                governance={"mode": "e2ag"},
                recovery={
                    "resource": "model:" + profile,
                    "replay_safe": True,
                    "max_attempts": 3,
                    "base_delay": 1,
                    "max_delay": 2,
                    "quota_delay": 2,
                },
            )
            value["launch"].update(
                image=self.image_id,
                command=["python", "-u", "-c", script],
                mounts=[
                    {
                        "source": str(self.control),
                        "target": "/run/fixture",
                        "read_only": True,
                    }
                ],
            )
            self.configure_launch(profile, value)
            path = self.root / (profile + ".json")
            path.write_text(json.dumps(value))
            _, spec = read_profile(path)
            self.profiles[profile] = (path, spec)
            registry[profile] = {"enabled": True, "path": str(path)}
            permissions = [
                {
                    "action": "runtime." + action,
                    "resource_kind": "runtime_profile",
                    "resource_id": profile,
                    "resource_version": spec.fingerprint(),
                    "constraint_revision": "recovery-qualification",
                }
                for action in ("execute", "accept_result")
            ]
            policy_profiles[profile] = {
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "work_ttl_seconds": 86400,
                "permissions": {
                    layer: permissions
                    for layer in ("caller", "service", "workload", "resource")
                },
            }
        policy_path = self.root / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "policy_id": "recovery-qualification",
                    "subject": "operator:test",
                    "actor": "service:test",
                    "profiles": policy_profiles,
                }
            )
        )
        self.policy = FileWorkPolicy(policy_path)
        profiles_path = self.root / "profiles.json"
        profiles_path.write_text(json.dumps({"version": 1, "profiles": registry}))
        await asyncio.to_thread(self.create_certificates)
        self.fingerprint = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert((self.root / "client.pem").read_text())
        ).hexdigest()
        identity_path = self.root / "machines.json"
        identity_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "audience": "qualification",
                    "machines": [
                        {
                            "enabled": True,
                            "issuance_enabled": True,
                            "revision": 1,
                            "subject": "worker:qualification",
                            "worker_id": "qualified",
                            "slots": 3,
                            "certificate_sha256": self.fingerprint,
                            "not_before": time.time() - 10,
                            "expires_at": time.time() + 3600,
                            "bindings": [
                                {"scope": self.store.scope, "profile": p}
                                for p in self.profiles
                            ],
                        }
                    ],
                }
            )
        )
        identities = WorkerIdentities(identity_path, "qualification")
        self.server = RemoteControl(
            self.store, identities, profiles_path, self.policy, self.fixture.provider
        )
        self.gateway = ToolGateway(
            SimpleNamespace(store=self.store), identities, profiles_path
        )
        self.gateway.runtime_control = self.server
        context = tls_context(
            self.root / "ca.pem",
            self.root / "server.pem",
            self.root / "server.key",
            server=True,
        )
        self.gateway_task = asyncio.create_task(
            self.gateway.serve(
                getattr(self, "gateway_host", "127.0.0.1"), 0, context, self.stop
            )
        )
        async with asyncio.timeout(5):
            while self.gateway.server is None:
                await asyncio.sleep(0.01)
        self.client_file = self.root / "client.json"
        self.client_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "enabled": True,
                    "worker_id": "qualified",
                    "audience": "qualification",
                    "host": getattr(self, "gateway_host", "127.0.0.1"),
                    "server_name": "localhost",
                    "port": self.gateway.server.sockets[0].getsockname()[1],
                    "ca_file": str(self.root / "ca.pem"),
                    "certificate_file": str(self.root / "client.pem"),
                    "key_file": str(self.root / "client.key"),
                    "credential_mode": "automatic",
                }
            )
        )

    def create_certificates(self):
        def command(*args):
            subprocess.run(
                ["openssl", *args],
                cwd=self.root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        command(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
            "-subj",
            "/CN=Recovery test CA",
            "-days",
            "1",
        )
        for name, usage in (("server", "serverAuth"), ("client", "clientAuth")):
            command(
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                name + ".key",
                "-out",
                name + ".csr",
                "-subj",
                "/CN=" + name,
            )
            (self.root / (name + ".ext")).write_text(
                "extendedKeyUsage=" + usage + "\nsubjectAltName=DNS:localhost\n"
            )
            command(
                "x509",
                "-req",
                "-in",
                name + ".csr",
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-out",
                name + ".pem",
                "-days",
                "1",
                "-extfile",
                name + ".ext",
            )

    def configure_launch(self, profile, value):
        """Let a native Harness qualification reuse the same recovery assertions."""

    async def cleanup_native(self):
        for process in self.children:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        if self.gateway_task is not None:
            self.stop.set()
            await asyncio.wait_for(self.gateway_task, 15)
        containers = await asyncio.to_thread(
            self.adapter.client.containers.list,
            all=True,
            filters={"label": "dios.runtime.scope=" + self.adapter.scope},
        )
        for container in containers:
            await asyncio.to_thread(container.remove, force=True)
        self.adapter.client.close()

    async def submit(self, key, profile="account-a"):
        _, spec = self.profiles[profile]
        return await self.store.submit(
            key,
            {
                "runtime_profile": profile,
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"barrier": True, "delay": 0.1},
            },
            authorization=self.policy.admission(profile, spec.fingerprint(), []),
        )

    async def launch(self, profile="account-a"):
        path, _ = self.profiles[profile]
        env = dict(os.environ, PYTHONPATH=str(ROOT / "backend"))
        env.pop("DIOS_RUNTIME_DATABASE_URL", None)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--scope",
            self.store.scope,
            "remote-worker",
            "--profile",
            str(path),
            "--control-file",
            str(self.client_file),
            "--artifacts",
            str(self.root / ("worker-" + str(len(self.children)))),
            "--once",
            "--poll-interval",
            ".1",
            cwd=ROOT / "backend",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.children.append(process)
        return process

    async def collect(self, process):
        out, err = await asyncio.wait_for(process.communicate(), 40)
        self.assertEqual(process.returncode, 0, err.decode())
        return json.loads(out)

    async def concurrency(self, count):
        async with asyncio.timeout(40):
            while True:
                status = await self.store.runtime_status()
                for process in self.children:
                    if process.returncode not in (None, 0):
                        _, err = await process.communicate()
                        self.fail("remote worker exited: " + err.decode()[-4000:])
                if status["actual_concurrency"] == count:
                    self.assertEqual(
                        len({a["process_id"] for a in status["attempts"]}), count
                    )
                    self.assertEqual(
                        len({a["session_id"] for a in status["attempts"]}), count
                    )
                    active_workers = {a["worker_id"] for a in status["attempts"]}
                    workers = [
                        w for w in status["workers"] if w["id"] in active_workers
                    ]
                    self.assertEqual(len({w["pid"] for w in workers}), count)
                    return {
                        "actual_concurrency": count,
                        "observed_at": status["observed_at"],
                        "workers": [
                            {k: w[k] for k in ("id", "pid", "instance")}
                            for w in workers
                        ],
                        "attempts": [
                            {
                                k: a[k]
                                for k in (
                                    "run_id",
                                    "attempt_id",
                                    "worker_id",
                                    "session_id",
                                    "process_id",
                                    "process_instance",
                                    "freshness",
                                )
                            }
                            for a in status["attempts"]
                        ],
                    }
                await asyncio.sleep(0.1)

    async def qualification(self, code):
        (self.control / "outage").write_text(code)
        run = await self.submit("initial")
        failed = await self.collect(await self.launch())
        self.assertEqual(failed["state"], "retry_wait")
        self.assertEqual(failed["recovery"]["code"], code)
        first = (await self.store.get(run["id"]))["attempts"][0]
        old_binding = (
            run["id"],
            first["id"],
            first["worker_id"],
            first["worker_instance"],
            first["epoch"],
        )
        queued = [await self.submit("queued-" + str(i)) for i in range(3)]
        (self.control / "outage").unlink()
        # The real server clock drives cooldown; no database deadlines are rewritten.
        delay = failed["recovery"]["retry_at"] - time.time()
        if delay > 0:
            await asyncio.sleep(delay + 0.1)
        probes = [await self.launch() for _ in range(3)]
        probe = await self.concurrency(1)
        state = await self.store.get(run["id"])
        self.assertEqual(state["recovery"]["circuit"]["state"], "half_open")
        self.assertEqual(
            state["recovery"]["circuit"]["probe_attempt"],
            probe["attempts"][0]["attempt_id"],
        )
        # The other resource runs while account-a remains half-open.
        other = await self.submit("independent", "account-b")
        other_process = await self.launch("account-b")
        independent = await self.concurrency(2)
        (self.control / "release").touch()
        outcomes = await asyncio.gather(*(self.collect(p) for p in probes))
        self.assertEqual(
            sorted(x["state"] for x in outcomes), ["idle", "idle", "succeeded"]
        )
        self.assertEqual((await self.collect(other_process))["state"], "succeeded")
        recovered = await self.store.get(run["id"])
        self.assertEqual(recovered["state"], "succeeded")
        self.assertEqual(len(recovered["attempts"]), 2)
        self.assertEqual(recovered["recovery"]["circuit"]["state"], "closed")
        self.assertEqual(
            recovered["input"]["work_authorization_ref"],
            run["input"]["work_authorization_ref"],
        )
        self.assertGreater(recovered["attempts"][-1]["epoch"], first["epoch"])
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            transport = RemoteToolProvider(
                self.client_file,
                SimpleNamespace(scope=self.store.scope),
                "account-a",
                self.profiles["account-a"][1],
            )
            transport.worker_id = first["worker_id"]
            stale = RemoteRuntimeStore(self.store.scope, transport)
            stale.instance = first["worker_instance"]
            await stale.report(
                *old_binding,
                999,
                "streaming",
                session_id=first["session_id"],
                output=True,
            )
        (self.control / "release").unlink()
        resumed = [await self.launch() for _ in range(3)]
        concurrent = await self.concurrency(3)
        self.assertEqual(
            {a["run_id"] for a in concurrent["attempts"]}, {r["id"] for r in queued}
        )
        (self.control / "release").touch()
        results = await asyncio.gather(*(self.collect(p) for p in resumed))
        self.assertTrue(all(r["state"] == "succeeded" for r in results))
        async with self.fixture.db.read(self.store.scope) as connection:
            grant_rows = (
                (
                    await connection.execute(
                        select(grants).where(grants.c.run_id == run["id"])
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(grant_rows), 2)
        self.assertEqual(len({g["attempt_id"] for g in grant_rows}), 2)
        self.assertEqual((await self.store.get(other["id"]))["state"], "succeeded")
        OBSERVATIONS.append(
            {
                "code": code,
                "image": self.image_id,
                "run_id": run["id"],
                "original_attempt": first["id"],
                "recovery_attempt": recovered["attempts"][-1]["id"],
                "probe": probe,
                "independent": independent,
                "restored": concurrent,
                "old_epoch_rejected": True,
                "original_work_retained": True,
                "distinct_grants": 2,
                "final_states": [r["state"] for r in results],
            }
        )

    async def test_rate_limit_probe_then_three_remote_workers(self):
        await self.qualification("PROVIDER_RATE_LIMITED")

    async def test_quota_probe_then_three_remote_workers(self):
        await self.qualification("PROVIDER_QUOTA_EXHAUSTED")
