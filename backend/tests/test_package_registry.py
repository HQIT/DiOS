"""Registry durability, immutable distribution and governed native rollouts."""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_runtime_capacity as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.package_lock import manifest, resolve
from app.runtime.package_registry import PackageRegistry, package_digest
from app.runtime.package_registry_schema import packages
from package_admission_fixtures import admit_fixture, declared, review_fixture
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_package_lock import package, requirement

OBSERVATIONS = []


class RegistryFixture:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    def setup_registry(self):
        self.registry = PackageRegistry(self.db, self.scope)
        self.provider = LocalArtifacts(self.root / "artifacts")
        self.addCleanup(self.provider.close)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir()

    async def register(self, version="1", content=b"original", **options):
        value = manifest(declared(package(version=version, content=content, **options)))
        (self.blobs / hashlib.sha256(content).hexdigest()).write_bytes(content)
        saved = await self.registry.register(
            value,
            self.blobs,
            self.provider,
            key=version + value["name"],
            actor="operator",
        )
        return value, saved


class RegistryTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    async def test_immutable_versions_idempotency_race_and_scope(self):
        self.setup_registry()
        value, original = await self.register()
        rows = await asyncio.gather(
            *(
                self.registry.register(
                    value, self.blobs, self.provider, key="shared", actor="operator"
                )
                for _ in range(3)
            )
        )
        self.assertTrue(all(x == original for x in rows))
        for changed in (
            dict(value, version="1.0.0"),
            declared(package(version="1", content=b"other")),
        ):
            (self.blobs / hashlib.sha256(b"other").hexdigest()).write_bytes(b"other")
            with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_IMMUTABLE"):
                await self.registry.register(
                    changed, self.blobs, self.provider, key="conflict", actor="operator"
                )
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.registry.register(
                value, self.blobs, self.provider, key="shared", actor="different"
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_REGISTERED"):
            await PackageRegistry(self.db, "other").show(original["digest"])
        async with self.db.transaction(self.scope) as c:
            with self.assertRaises(IntegrityError):
                await c.execute(packages.update().values(actor="changed"))
        reopened = Database(self.url)
        try:
            self.assertEqual(
                await PackageRegistry(reopened, self.scope).show(original["digest"]),
                original,
            )
        finally:
            await reopened.close()

    async def test_export_is_byte_verified_and_does_not_deploy(self):
        self.setup_registry()
        value, _ = await self.register(content="中文资料".encode())
        lock = await self.registry.lock([requirement("agent")])
        with patch("docker.from_env") as docker:
            await self.registry.export(lock, self.provider, self.root / "export")
            docker.assert_not_called()
        self.assertEqual(json.loads((self.root / "export/lock.json").read_text()), lock)
        key = value["files"][0]["digest"].split(":")[1]
        self.assertEqual(
            (self.root / "export/blobs" / key).read_bytes(), "中文资料".encode()
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "EXPORT_EXISTS"):
            await self.registry.export(lock, self.provider, self.root / "export")
        (self.root / "artifacts" / key).write_bytes(b"broken")
        with self.assertRaisesRegex(RuntimeErrorCode, "PAYLOAD_MISMATCH"):
            await self.registry.export(lock, self.provider, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        from app.runtime.deployments import Deployments

        self.assertEqual(await Deployments(self.db, self.scope).list(), [])

    async def test_invalid_payload_unregistered_dependency_and_admission_gate(self):
        self.setup_registry()
        value = declared(package())
        with self.assertRaisesRegex(RuntimeErrorCode, "PAYLOAD_UNAVAILABLE"):
            await self.registry.register(
                value, self.blobs, self.provider, key="bad", actor="operator"
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_REGISTERED"):
            await self.registry.show(package_digest(value))
        await self.register(dependencies=[requirement("missing")])
        with self.assertRaisesRegex(RuntimeErrorCode, "UNSATISFIED"):
            await self.registry.lock([requirement("agent")])
        value, _ = await self.register(version="2")
        lock = resolve([value], [requirement("agent")])
        with (
            patch("docker.from_env") as docker,
            self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"),
        ):
            await self.registry.publish_profile(
                lock,
                self.root / "missing.json",
                self.provider,
                key="publish",
                actor="operator",
            )
        docker.assert_not_called()

    async def test_backup_restore_preserves_catalog_content_and_receipts(self):
        from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup

        self.setup_registry()
        value, saved = await self.register()
        bundle = self.root / "backup"
        await asyncio.to_thread(
            backup_sqlite, self.url, self.root / "artifacts", bundle
        )
        report = await asyncio.to_thread(verify_backup, bundle)
        self.assertIn("runtime_package_registry_schema", report["schemas"])
        destination = self.root / "restored"
        await restore_sqlite(bundle, destination)
        db = Database("sqlite+aiosqlite:///" + str(destination / "state.sqlite"))
        try:
            registry = PackageRegistry(db, self.scope)
            self.assertEqual(await registry.show(saved["digest"]), saved)
            self.assertEqual(
                await registry.register(
                    value,
                    self.root / "absent",
                    self.provider,
                    key="1agent",
                    actor="operator",
                ),
                saved,
            )
        finally:
            await db.close()

    async def test_cli_registration_lock_and_export(self):
        self.setup_registry()
        value = declared(package())
        (self.blobs / hashlib.sha256(b"original").hexdigest()).write_bytes(b"original")
        (self.root / "manifest.json").write_text(json.dumps(value))
        (self.root / "roots.json").write_text(json.dumps([requirement("agent")]))

        def cli(*args):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.runtime.cli",
                    "--database-url",
                    self.url,
                    "--scope",
                    self.scope,
                    "registry",
                    *map(str, args),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        saved = await asyncio.to_thread(
            cli,
            "register",
            "--manifest",
            self.root / "manifest.json",
            "--blobs",
            self.blobs,
            "--artifacts",
            self.root / "artifacts",
            "--key",
            "cli",
            "--actor",
            "operator",
        )
        self.assertEqual(saved["digest"], package_digest(value))
        lock = await asyncio.to_thread(cli, "lock", "--roots", self.root / "roots.json")
        (self.root / "lock.json").write_text(json.dumps(lock))
        await asyncio.to_thread(
            cli,
            "export",
            "--lock",
            self.root / "lock.json",
            "--artifacts",
            self.root / "artifacts",
            "--destination",
            self.root / "bundle",
        )
        self.assertEqual(json.loads((self.root / "bundle/lock.json").read_text()), lock)


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_DOCKER"), "native Docker opt-in required"
)
class NativeRegistryTests(RegistryFixture, unittest.IsolatedAsyncioTestCase):
    async def test_release_failure_drain_rollback_and_stale_approval(self):
        import docker
        from app.runtime.deployments import Deployments
        from app.runtime.docker_adapter import DockerRuntimeAdapter
        from app.runtime.execution_revisions import load_profile
        from app.runtime.package_admission import PackageAdmission
        from app.runtime.work_authorizations import WorkAuthorizationVerifier
        from app.runtime.worker import Worker
        from test_execution_revisions import profile
        from work_auth_fixtures import policy_fixture

        self.setup_registry()
        code = (
            b"import json,pathlib\n"
            b"text=pathlib.Path('/opt/dios/packages/prompt/data.txt').read_text()\n"
            b"print(json.dumps({'type':'session','session_id':text}),flush=True)\n"
            b"if text=='bad': print(json.dumps({'type':'error','code':'EVIDENCE_INSUFFICIENT'}),flush=True)\n"
            b"else: print(json.dumps({'type':'result','session_id':text,'result':{'summary':text,'evidence':['fixture:registry-release']}}),flush=True)\n"
        )
        raw = profile(mode="e2ag")
        raw["launch"].update(
            image="dios-backend:package-admission-20261126",
            command=["python", "/opt/dios/packages/agent/task.py"],
        )
        path = self.root / "profile.json"
        path.write_text(json.dumps(raw))
        adapter = DockerRuntimeAdapter(self.scope)
        client = docker.from_env()
        images, handles, snapshots, manifests = set(), [], [], []
        policy = None
        try:
            for version, text in (("1", "original"), ("2", "upgrade"), ("3", "bad")):
                agent, _ = await self.register(
                    version,
                    code,
                    path="task.py",
                    dependencies=[requirement("prompt", "==" + version)],
                )
                prompt, _ = await self.register(version, text.encode(), name="prompt")
                for pkg in (agent, prompt):
                    await admit_fixture(self.db, self.scope, self.root, pkg)
                lock = await self.registry.lock([requirement("agent", "==" + version)])
                saved = await self.registry.publish_profile(
                    lock,
                    path,
                    self.provider,
                    key="release-" + version,
                    actor="operator",
                )
                ref = saved["revision"]["id"]
                _, name, spec, _, _ = await load_profile(self.db, self.scope, ref)
                images.add(spec.image)
                previous = json.loads(policy.path.read_text()) if policy else None
                policy, _ = policy_fixture(self.root, name, spec)
                if previous:
                    config = json.loads(policy.path.read_text())
                    prior = previous["profiles"][name]
                    config["profiles"][name]["revisions"] = {
                        **prior.get("revisions", {}),
                        prior["profile_digest"]: {
                            k: v for k, v in prior.items() if k != "revisions"
                        },
                    }
                    policy.path.write_text(json.dumps(config))
                snapshots.append((ref, name, spec))
                manifests.append(agent)
            deployments = Deployments(self.db, self.scope, policy)

            async def request(index, key, version, rollback=False):
                return await deployments.request(
                    "primary",
                    None if rollback else snapshots[index][0],
                    key,
                    version,
                    "operator",
                    {"payload": {}},
                    rollback=rollback,
                )

            async def execute(index):
                ref, name, spec = snapshots[index]
                worker = Worker(
                    self.store,
                    adapter,
                    name,
                    spec,
                    self.provider,
                    revision_ref=ref,
                    poll_interval=0.03,
                    authorization_verifier=WorkAuthorizationVerifier(
                        self.store, policy
                    ),
                )
                result = await worker.once()
                current = await self.store.get(result["id"])
                handles.append(current["attempt_id"])
                OBSERVATIONS.append(
                    {
                        "revision": ref,
                        "run": current["id"],
                        "attempt": current["attempt_id"],
                        "state": current["state"],
                        "session": current["attempts"][0]["session_id"],
                        "package_admissions": current["input"]["package_admissions"],
                    }
                )
                return result

            await request(0, "initial", 0)
            self.assertEqual((await execute(0))["state"], "succeeded")
            ready = await deployments.reconcile("primary")
            self.assertEqual(ready["status"]["active_revision_id"], snapshots[0][0])
            await request(2, "bad-upgrade", ready["deployment"]["version"])
            self.assertEqual((await execute(2))["state"], "failed")
            failed = await deployments.reconcile("primary")
            self.assertEqual(failed["status"]["active_revision_id"], snapshots[0][0])
            self.assertEqual(failed["status"]["error"], "DEPLOYMENT_HEALTH_FAILED")

            ref, name, spec = snapshots[0]
            retained = await self.store.submit(
                "retained",
                {
                    "runtime_profile": name,
                    "profile_digest": spec.fingerprint(),
                    "secret_refs": [],
                    "payload": {},
                },
                authorization=policy.admission(name, spec.fingerprint(), []),
                revision_ref=ref,
                deployment_id="primary",
            )
            queued = await request(1, "upgrade", failed["deployment"]["version"])
            repeated = await request(1, "upgrade", failed["deployment"]["version"])
            self.assertEqual(queued["receipt"], repeated["receipt"])
            self.assertEqual((await execute(1))["state"], "succeeded")
            self.assertEqual(
                (await deployments.reconcile("primary"))["deployment"]["state"],
                "draining",
            )
            self.assertEqual((await execute(0))["id"], retained["id"])
            ready = await deployments.reconcile("primary")
            self.assertEqual(ready["status"]["active_revision_id"], snapshots[1][0])

            await request(0, "rollback", ready["deployment"]["version"], rollback=True)
            self.assertEqual((await execute(0))["state"], "succeeded")
            # Revoking and re-approving cannot reuse the old completed probe.
            pkg = manifests[0]
            statement = await review_fixture(self.db, self.scope, self.root, pkg)
            admission = PackageAdmission(self.db, self.scope)
            for version, decision in ((1, "revoked"), (2, "admitted")):
                await admission.review(
                    pkg,
                    decision,
                    statement,
                    expected_version=version,
                    key=decision,
                    actor="reviewer",
                )
            rejected = await deployments.reconcile("primary")
            self.assertEqual(rejected["status"]["active_revision_id"], snapshots[1][0])
            self.assertIn("PACKAGE_ADMISSION", rejected["status"]["error"])
            await request(
                0, "rollback-fresh", rejected["deployment"]["version"], rollback=True
            )
            self.assertEqual((await execute(0))["state"], "succeeded")
            rolled = await deployments.reconcile("primary")
            self.assertEqual(rolled["status"]["active_revision_id"], snapshots[0][0])
            self.assertEqual(
                next(
                    x["version"]
                    for x in OBSERVATIONS[-1]["package_admissions"]
                    if x["digest"] == package_digest(pkg)
                ),
                3,
            )
            async with self.db.read(self.scope) as c:
                from app.runtime.deployment_schema import rollouts

                history = (
                    (
                        await c.execute(
                            select(rollouts).where(rollouts.c.scope == self.scope)
                        )
                    )
                    .mappings()
                    .all()
                )
            self.assertEqual(len(history), 5)
            self.assertTrue(all(row["probe_run_id"] for row in history))
        finally:
            for handle in handles:
                await adapter.remove(handle)
            for image in images:
                client.images.remove(image)
            client.close()
