"""Recovery from a disconnected original Worker, with current E2AG and mTLS identity."""

import asyncio
import hashlib
import json
import ssl
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import test_runtime_remote_control as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.lifecycle import Lifecycle
from app.runtime.reconciliation import ExitReceipts
from app.runtime.recovery import backup_sqlite, restore_sqlite
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.restore_client import RestoreRemoteClient, reconcile_receipt
from app.runtime.store import RuntimeStore
from app.runtime.work_auth_schema import decisions
from app.runtime.worker_identity import tls_context
from sqlalchemy import select


class RestoreRemoteTests(unittest.IsolatedAsyncioTestCase):
    write_machine = fixtures.RemoteControlTests.write_machine
    client = fixtures.RemoteControlTests.client
    submit = fixtures.RemoteControlTests.submit

    async def asyncSetUp(self):
        await fixtures.RemoteControlTests.asyncSetUp(self)
        original = self.remote.transport.exchange

        async def disconnected(envelope):
            if envelope["message"]["method"].startswith("runtime.upload."):
                raise ConnectionError("result transport disconnected")
            return await original(envelope)

        self.remote.transport.exchange = disconnected
        with self.assertRaises(ConnectionError):
            await self.worker.once()
        self.source = await self.f.store.get(self.run["id"])
        self.original_attempt = self.source["attempts"][0]
        self.receipt = ExitReceipts(self.client_provider, self.f.scope).load(
            self.original_attempt
        )
        self.assertIsNotNone(self.receipt)
        self.binding = [
            self.run["id"],
            self.original_attempt["id"],
            self.original_attempt["worker_id"],
            self.original_attempt["worker_instance"],
            self.original_attempt["epoch"],
        ]
        bundle, target = self.f.root / "backup", self.f.root / "restored"
        await asyncio.to_thread(backup_sqlite, self.f.url, self.f.provider.root, bundle)
        await restore_sqlite(bundle, target)
        self.db = Database("sqlite+aiosqlite:///" + str(target / "state.sqlite"))
        self.addAsyncCleanup(self.db.close)
        self.restored = RuntimeStore(self.db, self.f.scope)
        self.server = RemoteControl(
            self.restored,
            self.server.identities,
            self.profiles,
            self.policy,
            LocalArtifacts(target / "objects"),
        )
        self.old_cert = self.cert
        self.cert = "c" * 64
        self.machine["certificate_sha256"] = self.cert
        self.machine["revision"] += 1
        self.write_machine()
        self.recovery = RestoreRemoteClient(self.f.scope, self.client().transport)

    async def test_rotated_identity_recovers_original_result_without_relaunch(self):
        result = await reconcile_receipt(
            self.recovery, self.client_provider, self.receipt, self.source["version"]
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["attempt_id"], self.original_attempt["id"])
        self.assertEqual(result["attempts"][0]["session_id"], "fixture-session")
        self.assertEqual(self.adapter.started, 1)
        self.assertEqual(
            (await Lifecycle(self.db, self.f.scope).status())["mode"], "restore_hold"
        )
        self.assertEqual(
            (await self.f.store.get(self.run["id"]))["state"], self.source["state"]
        )
        async with self.db.read(self.f.scope) as c:
            actions = (
                (
                    await c.execute(
                        select(decisions.c.action).where(
                            decisions.c.run_id == self.run["id"]
                        )
                    )
                )
                .scalars()
                .all()
            )
        self.assertIn("restore-result", actions)
        self.assertIn("accept_result", actions)

    async def test_lost_response_and_new_client_reuse_original_receipt(self):
        original = self.recovery.transport.exchange
        lost = False

        async def lose(envelope):
            nonlocal lost
            result = await original(envelope)
            if (
                envelope["message"]["method"] == "runtime.restore.resolve_exit"
                and not lost
            ):
                lost = True
                raise ConnectionError("lost completion acknowledgement")
            return result

        self.recovery.transport.exchange = lose
        with self.assertRaises(ConnectionError):
            await reconcile_receipt(
                self.recovery,
                self.client_provider,
                self.receipt,
                self.source["version"],
            )
        before = await self.restored.get(self.run["id"])
        fresh = RestoreRemoteClient(self.f.scope, self.client().transport)
        result = await reconcile_receipt(
            fresh, self.client_provider, self.receipt, self.source["version"]
        )
        self.assertEqual(result["state"], "succeeded")
        after = await self.restored.get(self.run["id"])
        self.assertEqual(before["version"], after["version"])
        self.assertEqual(len(after["attempts"]), 1)
        self.assertEqual(self.adapter.started, 1)

    async def test_old_certificate_other_machine_and_execution_methods_rejected(self):
        exchange = self.recovery.transport.exchange

        async def old(envelope):
            return await self.server.handle(self.old_cert, time.time() + 3600, envelope)

        self.recovery.transport.exchange = old
        with self.assertRaisesRegex(RuntimeErrorCode, "WORKER_IDENTITY_DENIED"):
            await self.recovery.prepare(self.binding, self.source["version"])
        self.recovery.transport.exchange = exchange
        with self.assertRaisesRegex(RuntimeErrorCode, "RESTORE_METHOD_DENIED"):
            await self.recovery.call("claim", {})
        with self.assertRaisesRegex(RuntimeErrorCode, "RESTORE_EXECUTION_DENIED"):
            await self.recovery.bound(
                "authorize", self.binding, action="execute", session_id=None
            )
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RESTORE_RECONCILIATION_REQUIRED"
        ):
            client = self.client()
            await client.register_worker(client.worker_id, "instance", "host", 1)
        self.machine["worker_id"] = "another"
        self.write_machine()
        self.recovery.worker_id = "another"
        with self.assertRaisesRegex(RuntimeErrorCode, "WORKER_IDENTITY_SCOPE_DENIED"):
            await self.recovery.prepare(self.binding, self.source["version"])

    async def test_current_policy_change_rolls_back_prepare_and_keeps_denial(self):
        value = json.loads(self.policy.path.read_text())
        value["profiles"]["test-profile"]["permissions"]["caller"] = []
        self.policy.path.write_text(json.dumps(value))
        with self.assertRaises(RuntimeErrorCode):
            await self.recovery.prepare(self.binding, self.source["version"])
        run = await self.restored.get(self.run["id"])
        self.assertEqual(run["version"], self.source["version"])
        async with self.db.read(self.f.scope) as c:
            denied = (
                (
                    await c.execute(
                        select(decisions.c.decision).where(
                            decisions.c.action == "restore-result"
                        )
                    )
                )
                .scalars()
                .all()
            )
        self.assertEqual(denied[-1]["decision"], "deny")

    async def test_incomplete_result_and_wrong_binding_remain_unresolved(self):
        receipt = dict(self.receipt, truncated=True)
        with self.assertRaisesRegex(RuntimeErrorCode, "RESTORE_RESULT_REQUIRES_REVIEW"):
            await reconcile_receipt(
                self.recovery, self.client_provider, receipt, self.source["version"]
            )
        binding = [*self.binding[:4], self.binding[4] + 1]
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await self.recovery.prepare(binding, self.source["version"])
        self.assertEqual(
            (await self.restored.get(self.run["id"]))["version"], self.source["version"]
        )

    async def test_real_mtls_cli_and_repeated_process_use_original_result(self):
        root = self.f.root / "restore-tls"
        root.mkdir()

        def certificates():
            def command(*args):
                subprocess.run(
                    ["openssl", *args],
                    cwd=root,
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
                "/CN=Restore CA",
                "-days",
                "1",
            )
            for name, usage in (
                ("server", "serverAuth"),
                ("client", "clientAuth"),
                ("old", "clientAuth"),
            ):
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
                (root / (name + ".ext")).write_text(
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

        await asyncio.to_thread(certificates)
        self.cert = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert((root / "client.pem").read_text())
        ).hexdigest()
        self.machine["certificate_sha256"] = self.cert
        self.write_machine()
        gate = ToolGateway(
            SimpleNamespace(store=self.restored), self.server.identities, self.profiles
        )
        gate.runtime_control = self.server
        stop = asyncio.Event()
        server = asyncio.create_task(
            gate.serve(
                "127.0.0.1",
                0,
                tls_context(
                    root / "ca.pem",
                    root / "server.pem",
                    root / "server.key",
                    server=True,
                ),
                stop,
            )
        )
        try:
            async with asyncio.timeout(5):
                while gate.server is None:
                    await asyncio.sleep(0.01)
            config = {
                "version": 1,
                "enabled": True,
                "worker_id": "remote",
                "audience": "test",
                "host": "127.0.0.1",
                "server_name": "localhost",
                "port": gate.server.sockets[0].getsockname()[1],
                "ca_file": str(root / "ca.pem"),
                "certificate_file": str(root / "client.pem"),
                "key_file": str(root / "client.key"),
                "credential_mode": "automatic",
            }
            path = root / "client.json"
            path.write_text(json.dumps(config))
            old_path = root / "old.json"
            old_path.write_text(
                json.dumps(
                    dict(
                        config,
                        certificate_file=str(root / "old.pem"),
                        key_file=str(root / "old.key"),
                    )
                )
            )
            old = RestoreRemoteClient(
                self.f.scope,
                RemoteToolProvider(
                    old_path,
                    SimpleNamespace(scope=self.f.scope),
                    "test-profile",
                    self.f.spec,
                ),
            )
            with self.assertRaisesRegex(RuntimeErrorCode, "WORKER_IDENTITY_DENIED"):
                await old.prepare(self.binding, self.source["version"])
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps(self.receipt))
            cli = Path(__file__).resolve().parents[2] / "cli/dios"
            command = [
                sys.executable,
                str(cli),
                "runtime",
                "--scope",
                self.f.scope,
                "restore-reconcile",
                "--control-file",
                str(path),
                "--profile-file",
                str(self.profile),
                "--receipt-file",
                str(receipt),
                "--artifacts",
                str(self.client_provider.root),
                "--expected-version",
                str(self.source["version"]),
            ]
            completed = []
            for _ in range(2):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(process.communicate(), 30)
                self.assertEqual(process.returncode, 0, err.decode())
                completed.append(json.loads(out))
            self.assertEqual(completed[0], completed[1])
            self.assertEqual(completed[0]["state"], "succeeded")
            self.assertEqual(completed[0]["attempt_id"], self.original_attempt["id"])
            self.assertEqual(self.adapter.started, 1)
            self.assertEqual(
                (await Lifecycle(self.db, self.f.scope).status())["mode"],
                "restore_hold",
            )
        finally:
            stop.set()
            await asyncio.wait_for(server, 15)


if __name__ == "__main__":
    unittest.main()
