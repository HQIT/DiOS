"""Package review decisions bind evidence and fence stale execution authority."""

import asyncio
import copy
import json
import unittest
from uuid import uuid4

import test_runtime_capacity as fixtures
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.deployments import Deployments
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.package_admission import PackageAdmission, check_work, require_lock
from app.runtime.package_admission_schema import reviews
from app.runtime.package_declarations import declaration
from app.runtime.package_lock import manifest, resolve
from app.runtime.package_payloads import tree_descriptor
from app.runtime.revisions import Revisions
from app.runtime.store import digest
from package_admission_fixtures import declared, review_fixture
from sqlalchemy.exc import IntegrityError
from test_execution_revisions import profile
from test_package_lock import package, requirement
from work_auth_fixtures import policy_fixture


class DeclarationTests(unittest.TestCase):
    def test_spdx_expression_and_pinned_source(self):
        value = declared(package())["declaration"]
        value["license"] = "mit OR apache-2.0"
        self.assertEqual(declaration(value)["license"], "MIT OR Apache-2.0")
        for source in (
            {"uri": "https://u:p@example.test/repo", "revision": "a" * 40},
            {"uri": "https://example.test/repo", "revision": "main"},
        ):
            with self.assertRaises(RuntimeErrorCode):
                declaration(dict(value, source=source))
        with self.assertRaisesRegex(RuntimeErrorCode, "LICENSE"):
            declaration(dict(value, license="Made-Up-License"))

    def test_declaration_change_alters_package_and_lock_identity(self):
        first = declared(package())
        second = copy.deepcopy(first)
        second["declaration"]["capabilities"].append("files.write")
        self.assertNotEqual(digest(manifest(first)), digest(manifest(second)))
        self.assertNotEqual(
            resolve([first], [requirement("agent")])["digest"],
            resolve([second], [requirement("agent")])["digest"],
        )


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def setup_package(self):
        self.package = manifest(declared(package()))
        self.lock = resolve([self.package], [requirement("agent")])
        self.attestation = await review_fixture(
            self.db, self.scope, self.root, self.package
        )
        self.manager = PackageAdmission(self.db, self.scope)
        self.checksum = "sha256:" + digest(self.package)

    async def record(self, decision="admitted", version=0, **kwargs):
        return await self.manager.review(
            self.package,
            decision,
            kwargs.pop("attestation", self.attestation),
            expected_version=version,
            key=kwargs.pop("key", str(uuid4())),
            actor="operator",
            **kwargs,
        )

    async def test_pending_admitted_rejected_and_version_change_require_review(self):
        await self.setup_package()
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
                await require_lock(c, self.scope, self.lock)
        await self.record()
        async with self.db.read(self.scope) as c:
            self.assertEqual(
                await require_lock(c, self.scope, self.lock),
                [{"digest": self.checksum, "version": 1}],
            )
            for field in ("version", "declaration"):
                changed = copy.deepcopy(self.package)
                if field == "version":
                    changed["version"] = "2"
                else:
                    changed["declaration"]["capabilities"].append("write")
                with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
                    await require_lock(
                        c, self.scope, resolve([changed], [requirement("agent")])
                    )
        await self.record("rejected", 1)
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
                await require_lock(c, self.scope, self.lock)

    async def test_incomplete_failed_subject_and_evidence_checks_rejected(self):
        await self.setup_package()
        for change, error in [
            ("missing", "INCOMPLETE"),
            ("failed", "FAILED"),
            ("subject", "SUBJECT"),
            ("digest", "DIGEST"),
        ]:
            value = copy.deepcopy(self.attestation)
            if change == "missing":
                del value["predicate"]["checks"]["capability:fixture.read"]
            if change == "failed":
                value["predicate"]["checks"]["source"]["result"] = "failed"
            if change == "subject":
                value["subject"][0]["digest"]["sha256"] = "0" * 64
            if change == "digest":
                value["predicate"]["checks"]["source"]["digest"] = "sha256:" + "0" * 64
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(RuntimeErrorCode, error),
            ):
                await self.record(attestation=value)
        self.assertEqual(
            (await self.manager.show(self.checksum))["current"]["state"], "pending"
        )

    async def test_other_scope_cannot_use_evidence_or_admission(self):
        await self.setup_package()
        other = PackageAdmission(self.db, self.scope + "-other")
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await other.review(
                self.package,
                "admitted",
                self.attestation,
                expected_version=0,
                key="x",
                actor="operator",
            )
        await self.record()
        async with self.db.read(self.scope + "-other") as c:
            with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
                await require_lock(c, self.scope + "-other", self.lock)

    async def test_idempotency_concurrent_review_and_immutable_history(self):
        await self.setup_package()
        first = await self.record(key="once")
        self.assertEqual(first, await self.record(key="once"))
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY"):
            await self.record("rejected", key="once")
        results = await asyncio.gather(
            self.record("revoked", 1),
            self.record("rejected", 1),
            return_exceptions=True,
        )
        self.assertEqual(
            sum(
                isinstance(r, RuntimeErrorCode) and r.code == "VERSION_CONFLICT"
                for r in results
            ),
            1,
        )
        with self.assertRaises(IntegrityError):
            async with self.db.transaction(self.scope) as c:
                await c.execute(
                    reviews.update()
                    .where(reviews.c.scope == self.scope)
                    .values(decision="admitted")
                )
        await self.db.close()
        self.db = Database(self.url)
        history = await PackageAdmission(self.db, self.scope).show(self.checksum)
        self.assertEqual([r["version"] for r in history["items"]], [1, 2])
        self.assertFalse((await self.db.migrate())["changed"])

    async def test_reapproval_never_resurrects_captured_authority(self):
        from app.runtime.faults import category

        self.assertEqual(category("PACKAGE_ADMISSION_CHANGED"), "authorization")
        await self.setup_package()
        await self.record()
        run = {
            "scope": self.scope,
            "input": {"package_admissions": [{"digest": self.checksum, "version": 1}]},
        }
        async with self.db.read(self.scope) as c:
            await check_work(c, run)
        await self.record("revoked", 1)
        await self.record("admitted", 2)
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(RuntimeErrorCode, "ADMISSION_CHANGED"):
                await check_work(c, run)

    async def test_deployment_submission_and_e2ag_recheck_use_same_gate(self):
        from app.runtime.artifacts import LocalArtifacts
        from app.runtime.work_authorizations import WorkAuthorizationVerifier
        from app.runtime.worker import Worker
        from test_runtime_worker import FakeAdapter

        await self.setup_package()
        raw = profile(mode="e2ag")
        raw.update(package_lock=self.lock, packaged_mounts=[tree_descriptor(self.lock)])
        saved = await Revisions(self.db, self.scope).create(
            "revision", compile_profile(raw), actor="operator"
        )
        ref = saved["revision"]["id"]
        _, name, spec, _, _ = await load_profile(self.db, self.scope, ref)
        policy, grant = policy_fixture(self.root, name, spec)
        manager = Deployments(self.db, self.scope, policy)
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
            await manager.request(
                "dep", ref, "candidate", 0, "operator", {"payload": {}}
            )
        value = {
            "runtime_profile": name,
            "profile_digest": spec.fingerprint(),
            "secret_refs": [],
            "payload": {},
        }
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_ADMITTED"):
            await self.store.submit(
                "pending", value, authorization=grant, revision_ref=ref
            )
        await self.record()
        run = await self.store.submit(
            "approved", value, authorization=grant, revision_ref=ref
        )
        self.assertEqual(
            run["input"]["package_admissions"],
            [{"digest": self.checksum, "version": 1}],
        )
        await self.record("revoked", 1)
        await self.record("admitted", 2)
        adapter = FakeAdapter()
        worker = Worker(
            self.store,
            adapter,
            name,
            spec,
            LocalArtifacts(self.root / "artifacts"),
            revision_ref=ref,
            authorization_verifier=WorkAuthorizationVerifier(self.store, policy),
            poll_interval=0.01,
        )
        await worker.once()
        self.assertEqual(adapter.started, 0)
        self.assertNotEqual((await self.store.get(run["id"]))["state"], "succeeded")

    async def test_review_cli_roundtrip(self):
        from contextlib import redirect_stdout
        from io import StringIO

        from app.runtime.cli import main

        await self.setup_package()
        path = self.root / "review.json"
        path.write_text(
            json.dumps(
                {
                    "manifest": self.package,
                    "decision": "admitted",
                    "statement": self.attestation,
                }
            )
        )

        def invoke():
            with redirect_stdout(StringIO()) as output:
                code = main(
                    [
                        "--database-url",
                        self.url,
                        "--scope",
                        self.scope,
                        "package-review",
                        "record",
                        "--file",
                        str(path),
                        "--expected-version",
                        "0",
                        "--key",
                        "cli",
                        "--actor",
                        "operator",
                    ]
                )
            return code, json.loads(output.getvalue())

        code, value = await asyncio.to_thread(invoke)
        self.assertEqual(code, 0, value)
        self.assertEqual(value["decision"], "admitted")

    async def test_backup_restore_preserves_reviews_and_evidence(self):
        from app.runtime.recovery import backup_sqlite, restore_sqlite, verify_backup

        await self.setup_package()
        await self.record()
        await self.record("revoked", 1)
        before = await self.manager.show(self.checksum)
        bundle = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, self.root / "evidence", bundle)
        report = await asyncio.to_thread(verify_backup, bundle)
        self.assertIn("runtime_package_admission_schema", report["schemas"])
        destination = self.root / "restored"
        restored = await restore_sqlite(bundle, destination)
        self.assertEqual(restored["mode"], "restore_hold")
        db = Database("sqlite+aiosqlite:///" + str(destination / "state.sqlite"))
        try:
            self.assertEqual(
                await PackageAdmission(db, self.scope).show(self.checksum), before
            )
        finally:
            await db.close()
