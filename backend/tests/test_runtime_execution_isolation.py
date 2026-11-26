"""Native isolation composes with the existing E2AG approval and dispatch budget."""

import asyncio
import copy
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

from app.runtime.adapter import LaunchSpec, Mount
from app.runtime.database import RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.execution_isolation import (
    POLICY_ENV,
    READONLY,
    validate,
    verify_container,
)
from app.runtime.worker import Worker, read_profile
import test_runtime_tool_budgets as budgets
import test_runtime_worker as workers
from work_auth_fixtures import policy_fixture
from app.runtime.work_authorizations import WorkAuthorizationVerifier

NATIVE_OBSERVATIONS = []


def strict(**kwargs):
    return LaunchSpec(
        "python:3.12-slim",
        ("python", "-c", "import time; time.sleep(60)"),
        environment={POLICY_ENV: json.dumps(READONLY)},
        **kwargs,
    )


class ProfileIsolationTests(unittest.TestCase):
    def test_fingerprint_governance_and_legacy_compatibility(self):
        value = dict(
            id="test-profile",
            bridge="jsonl-v1",
            governance={"mode": "e2ag"},
            launch=dict(image="fixture", command=["fixture"]),
        )
        _, legacy = read_profile("unused", value=value)
        self.assertEqual(
            legacy.fingerprint(), LaunchSpec("fixture", ("fixture",)).fingerprint()
        )
        value["isolation"] = READONLY
        _, governed = read_profile("unused", value=value)
        self.assertNotEqual(legacy.fingerprint(), governed.fingerprint())
        for change, reason in [
            ({"governance": {}}, "WORK_AUTHORIZATION_REQUIRED"),
            ({"bridge": "a2a-0.3-jsonrpc"}, "EXECUTION_ISOLATION_UNSUPPORTED"),
            (
                {"isolation": dict(READONLY, network="bridge")},
                "EXECUTION_ISOLATION_UNSUPPORTED",
            ),
            (
                {"isolation": dict(READONLY, version=True)},
                "EXECUTION_ISOLATION_UNSUPPORTED",
            ),
            (
                {"launch": dict(value["launch"], environment={POLICY_ENV: "{}"})},
                "EXECUTION_ISOLATION_ENV_RESERVED",
            ),
        ]:
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(RuntimeErrorCode, reason),
            ):
                read_profile("unused", value=dict(value, **change))

    def test_native_escape_configurations_are_rejected(self):
        for change in [
            dict(network="host"),
            dict(user="0:0"),
            dict(user="root"),
            dict(read_only_root=False),
            dict(host_aliases={"host": "host-gateway"}),
        ]:
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(
                    RuntimeErrorCode, "EXECUTION_ISOLATION_CONFLICT"
                ),
            ):
                validate(replace(strict(), **change))
        for target, readonly in [
            ("/workspace", False),
            ("/var/run/docker.sock", True),
            ("/workspace/../run", True),
            ("/workspace//data", True),
            ("/run", True),
            ("/etc", True),
        ]:
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(
                    RuntimeErrorCode, "EXECUTION_ISOLATION_MOUNT_DENIED"
                ),
            ):
                validate(strict(mounts=(Mount("/tmp", target, readonly),)))

    def test_workspace_rejects_symlinks_and_host_ipc(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            spec = strict(mounts=(Mount(root, "/workspace"),))
            (path / "input").write_text("data")
            validate(spec, host_paths=True)
            (path / "link").symlink_to(path / "input")
            with self.assertRaisesRegex(
                RuntimeErrorCode, "EXECUTION_ISOLATION_SOURCE_DENIED"
            ):
                validate(spec, host_paths=True)
            (path / "link").unlink()
            sock = socket.socket(socket.AF_UNIX)
            try:
                sock.bind(str(path / "host.sock"))
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "EXECUTION_ISOLATION_SOURCE_DENIED"
                ):
                    validate(spec, host_paths=True)
            finally:
                sock.close()
            (path / "host.sock").unlink()
            os.mkfifo(path / "fifo")
            with self.assertRaisesRegex(
                RuntimeErrorCode, "EXECUTION_ISOLATION_SOURCE_DENIED"
            ):
                validate(spec, host_paths=True)

    def test_daemon_drift_and_implicit_writable_volumes_are_rejected(self):
        attrs = dict(
            Config={"User": "65534:65534"},
            Mounts=[],
            NetworkSettings={"Networks": {"none": {}}},
            HostConfig=dict(
                NetworkMode="none",
                ReadonlyRootfs=True,
                CapDrop=["ALL"],
                SecurityOpt=["no-new-privileges:true"],
                Tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                Mounts=[],
            ),
        )
        container = SimpleNamespace(attrs=attrs, reload=lambda: None)
        verify_container(container, strict())
        for key, value in [
            ("Privileged", True),
            ("NetworkMode", "host"),
            ("ReadonlyRootfs", False),
            ("CapAdd", ["NET_ADMIN"]),
            ("PidMode", "host"),
            ("ExtraHosts", ["host:1.1.1.1"]),
            ("Devices", [{"PathOnHost": "/dev/kvm"}]),
            ("Tmpfs", {"/tmp": "rw,size=1g"}),
        ]:
            container.attrs = copy.deepcopy(attrs)
            container.attrs["HostConfig"][key] = value
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(RuntimeErrorCode, "EXECUTION_ISOLATION_DRIFT"),
            ):
                verify_container(container, strict())
        container.attrs = copy.deepcopy(attrs)
        container.attrs["Mounts"] = [dict(Type="volume", Destination="/data", RW=True)]
        with self.assertRaisesRegex(RuntimeErrorCode, "EXECUTION_ISOLATION_DRIFT"):
            verify_container(container, strict())

    def test_immutable_revision_preserves_isolation_policy(self):
        from app.runtime.execution_revisions import compile_profile, decode

        raw = dict(
            id="isolated",
            bridge="jsonl-v1",
            governance={"mode": "e2ag"},
            isolation=READONLY,
            launch=dict(image="sha256:" + "a" * 64, command=["fixture"]),
        )
        manifest = compile_profile(raw)
        preserved, _, spec, _, _ = decode({"manifest": manifest})
        self.assertEqual(preserved["isolation"], READONLY)
        self.assertEqual(json.loads(spec.environment[POLICY_ENV]), READONLY)
        manifest["configuration"]["execution_profile"].pop("isolation")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_EXECUTION_BINDING_INVALID"
        ):
            decode({"manifest": manifest})


class WorkerIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = workers.WorkerTests.asyncSetUp
    asyncTearDown = workers.WorkerTests.asyncTearDown

    async def test_unsupported_adapter_fails_before_launch_with_authorization(self):
        self.spec = strict()
        policy, _ = policy_fixture(self.root, "test-profile", self.spec)
        await self.store.submit(
            "isolated",
            dict(
                runtime_profile="test-profile",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
            ),
            authorization=policy.admission("test-profile", self.spec.fingerprint(), []),
        )
        adapter = workers.FakeAdapter()
        result = await Worker(
            self.store,
            adapter,
            "test-profile",
            self.spec,
            self.provider,
            authorization_verifier=WorkAuthorizationVerifier(self.store, policy),
        ).once()
        self.assertEqual(result["state"], "failed")
        self.assertIn("EXECUTION_ISOLATION_UNSUPPORTED", json.dumps(result))
        self.assertEqual(adapter.started, 0)

    @unittest.skipUnless(
        os.environ.get("DIOS_TEST_DOCKER") == "1", "Native Docker qualification opt-in"
    )
    async def test_native_worker_retains_authorization_session_and_result(self):
        self.spec = replace(
            strict(),
            command=(
                "python",
                "-c",
                "import sys,time; sys.stdout.buffer.write("
                + repr(workers.protocol())
                + "); sys.stdout.flush(); time.sleep(.5)",
            ),
        )
        policy, _ = policy_fixture(self.root, "test-profile", self.spec)
        await self.store.submit(
            "isolated",
            dict(
                runtime_profile="test-profile",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
            ),
            authorization=policy.admission("test-profile", self.spec.fingerprint(), []),
        )
        adapter = DockerRuntimeAdapter(self.scope)
        try:
            result = await Worker(
                self.store,
                adapter,
                "test-profile",
                self.spec,
                self.provider,
                poll_interval=0.05,
                authorization_verifier=WorkAuthorizationVerifier(self.store, policy),
            ).once()
            self.assertEqual(result["state"], "succeeded", result)
            attempt = result["attempts"][0]
            self.assertEqual(attempt["session_id"], "fixture-session")
            self.assertEqual(attempt["remote"]["runtime"]["adapter"], "local-docker-v1")
            from sqlalchemy import select
            from app.runtime.schema import artifacts

            async with self.db.read(self.scope) as c:
                rows = (
                    (
                        await c.execute(
                            select(artifacts).where(artifacts.c.run_id == result["id"])
                        )
                    )
                    .mappings()
                    .all()
                )
            self.assertTrue(rows)
            self.assertTrue(
                all(self.provider.verify(row["storage_key"]) for row in rows)
            )
            self.assertTrue(result["input"]["work_authorization_ref"])
        finally:
            adapter.client.close()


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER") == "1", "Native Docker qualification opt-in"
)
class NativeIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = budgets.HttpToolBudgetTests()
        await self.fixture.asyncSetUp()
        self.adapter = DockerRuntimeAdapter(self.fixture.scope)
        self.handle = "isolation-" + uuid4().hex
        self.channel = None

    async def asyncTearDown(self):
        try:
            await self.adapter.stop(self.handle, 0)
            await self.adapter.remove(self.handle)
            if self.channel:
                await self.channel.close()
        finally:
            self.adapter.client.close()
            await self.fixture.asyncTearDown()

    async def run_python(self, code, *args):
        container = await asyncio.to_thread(self.adapter._get, self.handle)
        result = await asyncio.to_thread(
            container.exec_run, ["python", "-c", code, *args]
        )
        self.assertEqual(result.exit_code, 0, result.output.decode(errors="replace"))
        return json.loads(result.output)

    async def request(self, method, params):
        return await self.run_python(
            "import socket,json,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(20); "
            "s.connect('/run/dios-tools/channel.sock'); s.sendall(sys.argv[1].encode()+b'\\n'); "
            "print(s.makefile('rb').readline().decode()); s.close()",
            json.dumps(dict(method=method, params=params)),
        )

    async def test_readonly_native_network_and_governed_budget_in_same_process(self):
        from app.runtime.worker_tool_channel import WorkerToolChannel

        f = self.fixture
        self.channel = WorkerToolChannel(
            f.gate,
            f.binding,
            {"tools": {"search": "search"}, "acp_names": {}},
            os.getuid(),
        )
        channel_mount = await self.channel.open()
        await self.channel.bind_session("session-one")
        workspace = f.root / "inputs"
        workspace.mkdir()
        (workspace / "evidence.txt").write_text("来源证据🙂", encoding="utf-8")
        spec = strict(
            user=f"{os.getuid()}:{os.getgid()}",
            mounts=(Mount(str(workspace), "/workspace"), channel_mount),
        )
        observed = await self.adapter.start(self.handle, spec)
        self.assertTrue(observed.alive)
        checks = await self.run_python(
            """
import json,pathlib,socket
result={'read':pathlib.Path('/workspace/evidence.txt').read_text(), 'uid':__import__('os').getuid()}
for name in ['/workspace/evidence.txt','/etc/forbidden']:
    try: pathlib.Path(name).write_text('bad'); result[name]=False
    except OSError: result[name]=True
pathlib.Path('/tmp/scratch').write_text('ok'); result['tmp']=True
for address in [('1.1.1.1',443), ('127.0.0.1',int(__import__('sys').argv[1]))]:
    try: socket.create_connection(address,.3).close(); result[str(address)]=False
    except OSError: result[str(address)]=True
result['interfaces']=sorted(pathlib.Path('/sys/class/net').iterdir(),key=str)
result['interfaces']=[p.name for p in result['interfaces']]
print(json.dumps(result))
""",
            str(f.server.server_port),
        )
        self.assertEqual(checks["read"], "来源证据🙂")
        self.assertGreater(checks["uid"], 0)
        self.assertTrue(
            all(
                value is True
                for key, value in checks.items()
                if key not in {"read", "uid", "interfaces"}
            )
        )
        self.assertEqual(checks["interfaces"], ["lo"])
        self.assertEqual((workspace / "evidence.txt").read_text(), "来源证据🙂")
        for index in range(3):
            response = await self.request(
                "request",
                dict(
                    tool="search",
                    arguments={"query": "evidence"},
                    key=f"native-{index}",
                ),
            )
            self.assertTrue(response["ok"], response)
            item = response["value"]
            waiting = await self.request("advance", {"invocation_id": item["id"]})
            self.assertEqual(waiting["value"]["state"], "awaiting_approval")
            await f.approve(item)
            result = await self.request("advance", {"invocation_id": item["id"]})
            if index < 2:
                self.assertEqual(result["value"]["state"], "succeeded", result)
                replay = await self.request("advance", {"invocation_id": item["id"]})
                self.assertEqual(replay["value"]["state"], "succeeded")
            else:
                self.assertEqual(result["error"], "TOOL_BUDGET_EXHAUSTED")
        self.assertEqual(len(f.transport.calls), 2)
        run = await f.store.get(f.run["id"])
        await f.store.revoke(run["id"], run["version"])
        revoked = await self.request(
            "request",
            dict(tool="search", arguments={"query": "evidence"}, key="revoked"),
        )
        self.assertEqual(revoked["error"], "AUTH_REVOKED")
        NATIVE_OBSERVATIONS.append(
            dict(
                process_instance=observed.process_instance,
                pid=observed.pid,
                checks=checks,
                http_dispatches=len(f.transport.calls),
                revoked=True,
            )
        )

    async def test_image_volume_is_rejected_before_process_start(self):
        client = self.adapter.client
        image, _ = await asyncio.to_thread(
            client.images.build,
            fileobj=io.BytesIO(b"FROM python:3.12-slim\nVOLUME /writable\n"),
            rm=True,
            pull=False,
            network_mode="none",
        )
        try:
            with self.assertRaisesRegex(RuntimeErrorCode, "EXECUTION_ISOLATION_DRIFT"):
                await self.adapter.start(self.handle, replace(strict(), image=image.id))
            observed = await self.adapter.observe(self.handle)
            self.assertEqual(observed.state, "created")
            self.assertFalse(observed.alive)
            # Remove only this test's verified scoped unstarted container and volume.
            container = await asyncio.to_thread(self.adapter._get, self.handle)
            await asyncio.to_thread(container.remove, v=True)
        finally:
            await asyncio.to_thread(client.images.remove, image.id)
