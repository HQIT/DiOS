"""Remote ownership, retries and recovery remain confined to admitted revisions."""

import json
import os
import unittest

import test_runtime_remote_control as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile, load_profile, read_export
from app.runtime.revisions import Revisions
from app.runtime.schema import attempts
from app.runtime.worker import Worker
from sqlalchemy import update
from work_auth_fixtures import policy_fixture


class RemoteRevisionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RemoteControlTests.asyncSetUp
    write_machine = fixtures.RemoteControlTests.write_machine
    client = fixtures.RemoteControlTests.client
    submit = fixtures.RemoteControlTests.submit

    async def snapshot(self, command="old-command", image=None, argv=None):
        raw = json.loads(self.profile.read_text())
        raw["launch"]["image"] = image or "sha256:" + "1" * 64
        raw["launch"]["command"] = argv or [command]
        saved = await Revisions(self.f.db, self.f.scope).create(
            command, compile_profile(raw), actor="test-operator"
        )
        ref = saved["revision"]["id"]
        _, _, spec, _, _ = await load_profile(self.f.db, self.f.scope, ref)
        previous = json.loads(self.policy.path.read_text())["profiles"]["test-profile"]
        self.policy, grant = policy_fixture(self.f.root, "test-profile", spec)
        config = json.loads(self.policy.path.read_text())
        old = {k: v for k, v in previous.items() if k != "revisions"}
        config["profiles"]["test-profile"]["revisions"] = {
            **previous.get("revisions", {}),
            previous["profile_digest"]: old,
        }
        self.policy.path.write_text(json.dumps(config))
        registry = json.loads(self.profiles.read_text())
        registry["profiles"]["test-profile"].setdefault("revisions", []).append(ref)
        self.profiles.write_text(json.dumps(registry))
        run = await self.f.store.submit(
            command,
            {
                "runtime_profile": "test-profile",
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {},
            },
            revision_ref=ref,
            authorization=grant,
        )
        return ref, spec, run

    def version_worker(self, snap):
        ref, spec, _ = snap
        remote = self.client()
        remote.transport.spec = spec
        remote.transport.revision_ref = ref
        return Worker(
            remote,
            self.adapter,
            "test-profile",
            spec,
            self.client_provider,
            revision_ref=ref,
            poll_interval=0.01,
            authorization_verifier=remote.verifier,
        )

    async def test_remote_uses_explicit_old_revision_after_profile_file_deleted(self):
        old = await self.snapshot()
        new = await self.snapshot("new-command")
        self.profile.unlink()
        worker = self.version_worker(new)
        result = await worker.once()
        self.assertEqual(result["id"], new[2]["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual((await self.f.store.get(old[2]["id"]))["attempts"], [])
        result = await self.version_worker(old).once()
        self.assertEqual(result["id"], old[2]["id"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.adapter.started, 2)
        self.assertEqual((await self.f.store.get(self.run["id"]))["state"], "queued")

    async def test_removed_admission_rejects_revision_without_claim_or_launch(self):
        snap = await self.snapshot()
        self.profiles.write_text(
            json.dumps(
                {
                    "version": 1,
                    "profiles": {"test-profile": {"enabled": True, "revisions": []}},
                }
            )
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "WORKER_REVISION_DENIED"):
            await self.version_worker(snap).once()
        self.assertEqual(self.adapter.started, 0)
        self.assertEqual((await self.f.store.get(snap[2]["id"]))["attempts"], [])

    async def test_recovery_filters_version_and_keeps_original_artifact_identity(self):
        old = await self.snapshot()
        new = await self.snapshot("new-command")
        worker = self.version_worker(old)
        original = worker.store.transport.exchange

        async def disconnected(e):
            if e["message"]["method"] == "runtime.upload.begin":
                raise ConnectionError("disconnected")
            return await original(e)

        worker.store.transport.exchange = disconnected
        with self.assertRaises(ConnectionError):
            await worker.once()
        run = await self.f.store.get(old[2]["id"])
        aid = run["attempt_id"]
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(
                update(attempts).where(attempts.c.id == aid).values(lease_until=0)
            )
        self.assertEqual(await self.version_worker(new).recover(), [])
        results = await self.version_worker(old).recover()
        self.assertEqual(results[0]["state"], "succeeded")
        result = await self.f.store.get(old[2]["id"])
        self.assertEqual(result["attempt_id"], aid)
        self.assertEqual(result["input"]["revision_ref"], old[0])
        self.assertEqual(self.adapter.started, 1)

    async def test_remote_export_checks_content_scope_and_request_revision(self):
        old = await self.snapshot()
        new = await self.snapshot("new-command")
        exported = await Revisions(self.f.db, self.f.scope).show(
            old[0], include_manifest=True
        )
        path = self.f.root / "revision.json"
        path.write_text(json.dumps(exported))
        self.assertEqual(read_export(path, self.f.scope)[0], old[0])
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_INTEGRITY_FAILED"):
            read_export(path, "another-scope")
        exported["manifest"]["configuration"]["execution_profile"]["launch"][
            "command"
        ] = ["altered"]
        path.write_text(json.dumps(exported))
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_INTEGRITY_FAILED"):
            read_export(path, self.f.scope)
        worker = self.version_worker(old)
        remote = worker.store
        await remote.register_worker(
            worker.worker_id, worker.instance, "test", os.getpid()
        )
        await remote.call("claim", {}, "fixed-request")
        remote.transport.spec = new[1]
        remote.transport.revision_ref = new[0]
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await remote.call("claim", {}, "fixed-request")

    @unittest.skipUnless(
        os.environ.get("DIOS_TEST_DOCKER"), "native Docker opt-in required"
    )
    async def test_actual_mtls_docker_uses_admitted_revision_without_source_profile(
        self,
    ):
        from app.runtime.docker_adapter import DockerRuntimeAdapter

        adapter = DockerRuntimeAdapter(self.f.scope)
        try:
            image = adapter.client.images.get(
                os.environ.get(
                    "DIOS_REVISION_TEST_IMAGE",
                    "dios-backend:execution-revisions-20261125",
                )
            ).id
            command = "import json; print(json.dumps({'type':'session','session_id':'remote-version'})); print(json.dumps({'type':'result','session_id':'remote-version','result':{'summary':'frozen','evidence':['fixture:mtls']}}))"
            snap = await self.snapshot("mtls", image, ["python", "-c", command])
            self.f.spec = snap[1]
            self.profile.unlink()
            exported = self.f.root / "remote-revision.json"
            exported.write_text(
                json.dumps(
                    await Revisions(self.f.db, self.f.scope).show(
                        snap[0], include_manifest=True
                    )
                )
            )
            result = await fixtures.RemoteControlTests.actual_tls(
                self, adapter, snap[0], exported
            )
            self.assertEqual(result["id"], snap[2]["id"])
            self.assertEqual(result["input"]["revision_ref"], snap[0])
            self.assertEqual(result["attempts"][0]["session_id"], "remote-version")
        finally:
            for container in adapter.client.containers.list(
                all=True, filters={"label": "dios.runtime.scope=" + adapter.scope}
            ):
                container.remove(force=True)
            adapter.client.close()


if __name__ == "__main__":
    unittest.main()
