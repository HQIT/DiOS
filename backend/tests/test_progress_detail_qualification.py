"""Independent progress and interrupted binary detail delivery over real mTLS."""

import asyncio
import base64
import hashlib
import json
import ssl
import subprocess
import time
import unittest
from types import SimpleNamespace

import test_runtime_remote_control as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.remote_schema import chunks, uploads
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.schema import artifacts, projections
from app.runtime.worker import Worker
from app.runtime.worker_identity import tls_context
from sqlalchemy import select

OBSERVATIONS = []


class ProgressDetailQualification(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RemoteControlTests.asyncSetUp
    write_machine = fixtures.RemoteControlTests.write_machine
    client = fixtures.RemoteControlTests.client
    submit = fixtures.RemoteControlTests.submit
    claim = fixtures.RemoteControlTests.claim

    async def connect_tls(self):
        root = self.f.root / "tls"
        root.mkdir()

        def certs():
            def cmd(*args):
                subprocess.run(
                    ["openssl", *args],
                    cwd=root,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            cmd(
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
                "/CN=Qualification CA",
                "-days",
                "1",
            )
            for name, usage in [("server", "serverAuth"), ("client", "clientAuth")]:
                cmd(
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
                cmd(
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

        await asyncio.to_thread(certs)
        self.cert = hashlib.sha256(
            ssl.PEM_cert_to_DER_cert((root / "client.pem").read_text())
        ).hexdigest()
        self.machine["certificate_sha256"] = self.cert
        self.write_machine()
        gate = ToolGateway(
            SimpleNamespace(store=self.f.store), self.server.identities, self.profiles
        )
        gate.runtime_control = self.server
        stop = asyncio.Event()
        task = asyncio.create_task(
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

        async def cleanup():
            stop.set()
            await asyncio.wait_for(task, 15)

        self.addAsyncCleanup(cleanup)
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
        transport = RemoteToolProvider(
            path, SimpleNamespace(scope=self.f.scope), "test-profile", self.f.spec
        )
        self.remote = RemoteRuntimeStore(self.f.scope, transport)
        self.worker = Worker(
            self.remote,
            self.adapter,
            "test-profile",
            self.f.spec,
            self.client_provider,
            poll_interval=0.05,
            authorization_verifier=self.remote.verifier,
        )
        return gate

    async def test_progress_survives_stalled_detail_and_resumes_verified_utf8(self):
        gate = await self.connect_tls()
        attempt, binding = await self.claim()
        data = json.dumps({"内容": "中文🙂" * 300000}, ensure_ascii=False).encode()
        split_count = 0
        for offset in range(0, len(data), 65536):
            try:
                data[offset : offset + 65536].decode("utf-8")
            except UnicodeDecodeError:
                split_count += 1
        self.assertGreater(split_count, 0)
        blob = self.client_provider.put([data])
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.server.handle

        async def fault(certificate, expiry, envelope):
            if (
                envelope["message"]["method"] == "runtime.upload.chunk"
                and not entered.is_set()
            ):
                entered.set()
                await release.wait()
                await original(certificate, expiry, envelope)
                raise RuntimeErrorCode("WORKER_GATEWAY_UNAVAILABLE")
            return await original(certificate, expiry, envelope)

        self.server.handle = fault
        delivery = asyncio.create_task(
            self.remote.upload(binding, self.client_provider, blob.key)
        )

        async def settle():
            release.set()
            await asyncio.gather(delivery, return_exceptions=True)

        self.addAsyncCleanup(settle)
        await asyncio.wait_for(entered.wait(), 10)
        started = time.monotonic()
        async with asyncio.timeout(10):
            for sequence, phase in enumerate(
                ("preparing", "creating_session", "waiting_model", "streaming"), 1
            ):
                if sequence == 3:
                    await self.remote.bound(
                        "authorize",
                        binding,
                        action="execute",
                        session_id="session-original",
                    )
                await self.remote.report(
                    *binding,
                    sequence,
                    phase,
                    session_id="session-original" if sequence >= 3 else None,
                    output=sequence == 4,
                    output_observed_at=time.time() if sequence == 4 else None,
                    output_excerpt="详情等待期间的新进度" if sequence == 4 else None,
                )
            await self.remote.heartbeat(*binding)
        progress_seconds = time.monotonic() - started
        self.assertFalse(delivery.done())
        current = await self.f.store.get(self.run["id"])
        self.assertEqual(current["attempts"][0]["state"], "streaming")
        pending = sorted(
            await self.f.store.pending_events("progress-qualification"),
            key=lambda e: e["version"],
        )
        newest = pending[-1]
        self.assertTrue(await self.f.store.apply_projection("dashboard", newest))
        for event in reversed(pending):
            self.assertFalse(await self.f.store.apply_projection("dashboard", event))
        async with self.f.db.read(self.f.scope) as c:
            projected = (
                (
                    await c.execute(
                        select(projections).where(
                            projections.c.consumer == "dashboard",
                            projections.c.run_id == self.run["id"],
                        )
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(projected["version"], newest["version"])
        self.assertLess(len(json.dumps(newest).encode()), 16384)
        release.set()
        with self.assertRaises(ConnectionError):
            await delivery
        with self.assertRaisesRegex(RuntimeErrorCode, "ARTIFACT_ACCESS_DENIED"):
            await self.remote.bound("artifact.read", binding, key=blob.key, offset=0)
        with self.assertRaisesRegex(RuntimeErrorCode, "UPLOAD_OFFSET_CONFLICT"):
            await self.remote.bound(
                "upload.chunk",
                binding,
                key=blob.key,
                offset=131072,
                data=base64.b64encode(data[131072:196608]).decode(),
            )
        response = await self.remote.bound(
            "upload.chunk",
            binding,
            key=blob.key,
            offset=0,
            data=base64.b64encode(data[:65536]).decode(),
        )
        self.assertEqual(response["received"], 65536)
        with self.assertRaisesRegex(RuntimeErrorCode, "UPLOAD_CONFLICT"):
            await self.remote.bound(
                "upload.chunk",
                binding,
                key=blob.key,
                offset=0,
                data=base64.b64encode(b"x" * 65536).decode(),
            )
        gate.runtime_control = RemoteControl(
            self.f.store,
            self.server.identities,
            self.profiles,
            self.policy,
            self.f.provider,
        )
        resumed = RemoteRuntimeStore(self.f.scope, self.remote.transport)
        resumed.instance = self.remote.instance
        await resumed.upload(binding, self.client_provider, blob.key)
        await resumed.upload(binding, self.client_provider, blob.key)
        await resumed.retain_output(binding, self.client_provider, blob.key, "stdout")
        received = bytearray()
        while len(received) < len(data):
            part = await resumed.bound(
                "artifact.read", binding, key=blob.key, offset=len(received)
            )
            received.extend(base64.b64decode(part["data"]))
        self.assertEqual(bytes(received), data)
        self.assertEqual(json.loads(received), json.loads(data))
        self.assertEqual(hashlib.sha256(received).hexdigest(), blob.key)
        async with self.f.db.read(self.f.scope) as c:
            upload = (
                (
                    await c.execute(
                        select(uploads).where(
                            uploads.c.attempt_id == attempt["id"],
                            uploads.c.key == blob.key,
                        )
                    )
                )
                .mappings()
                .one()
            )
            parts = (
                (
                    await c.execute(
                        select(chunks).where(chunks.c.upload_id == upload["id"])
                    )
                )
                .mappings()
                .all()
            )
            linked = (
                (
                    await c.execute(
                        select(artifacts).where(artifacts.c.run_id == self.run["id"])
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(sum(p["size"] for p in parts), len(data))
        self.assertEqual(len(parts), (len(data) + 65535) // 65536)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["attempt_id"], attempt["id"])
        self.assertEqual(
            (await self.f.store.get(self.run["id"]))["attempts"][0]["session_id"],
            "session-original",
        )
        OBSERVATIONS.append(
            {
                "case": "mtls-independent-progress-detail",
                "bytes": len(data),
                "sha256": blob.key,
                "chunk_count": len(parts),
                "utf8_split_chunks": split_count,
                "progress_during_stall_seconds": round(progress_seconds, 3),
                "progress_event_bytes": len(json.dumps(newest).encode()),
                "run_id": self.run["id"],
                "attempt_id": attempt["id"],
                "session_id": "session-original",
                "artifact_id": linked[0]["id"],
                "resumed_offset": 65536,
                "stale_projection_rejected": True,
                "duplicate_chunks_reused": True,
                "conflicting_chunk_rejected": True,
                "incomplete_download_rejected": True,
            }
        )
