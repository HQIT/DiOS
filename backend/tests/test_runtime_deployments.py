"""Real governed Runs gate activation; restart and concurrency preserve old work."""

import asyncio
import json
import os
import unittest

import test_runtime_capacity as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.deployment_schema import rollouts
from app.runtime.deployments import Deployments
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.revisions import Revisions
from app.runtime.store import RuntimeStore
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from sqlalchemy import select
from test_runtime_worker import FakeAdapter
from work_auth_fixtures import policy_fixture


class DeploymentScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def snapshot(self, name):
        raw = {
            "id": "deployment-agent",
            "bridge": "jsonl-v1",
            "governance": {"mode": "e2ag"},
            "launch": {"image": "sha256:" + "1" * 64, "command": [name]},
        }
        saved = await Revisions(self.db, self.scope).create(
            name, compile_profile(raw), actor="operator"
        )
        ref = saved["revision"]["id"]
        _, profile, spec, _, _ = await load_profile(self.db, self.scope, ref)
        self.root / "work-policy.json"
        old = (
            json.loads(self.policy.path.read_text())
            if hasattr(self, "policy")
            else None
        )
        self.policy, _ = policy_fixture(self.root, profile, spec)
        config = json.loads(self.policy.path.read_text())
        if old:
            prior = old["profiles"][profile]
            config["profiles"][profile]["revisions"] = {
                **prior.get("revisions", {}),
                prior["profile_digest"]: {
                    k: v for k, v in prior.items() if k != "revisions"
                },
            }
            self.policy.path.write_text(json.dumps(config))
        self.deployments = Deployments(self.db, self.scope, self.policy)
        return ref, profile, spec

    async def request(self, snap, key, version, rollback=False):
        return await self.deployments.request(
            "primary",
            None if rollback else snap[0],
            key,
            version,
            "operator",
            {"payload": {"text": "health"}},
            rollback=rollback,
        )

    async def execute(self, snap, fail=False, store=None):
        store = store or self.store
        data = b'{"type":"error","code":"EVIDENCE_INSUFFICIENT"}\n' if fail else None
        adapter = FakeAdapter(data) if data else FakeAdapter()
        worker = Worker(
            store,
            adapter,
            snap[1],
            snap[2],
            LocalArtifacts(self.root / "artifacts"),
            revision_ref=snap[0],
            poll_interval=0.01,
            authorization_verifier=WorkAuthorizationVerifier(store, self.policy),
        )
        return await worker.once()

    async def activate(self, snap, key="first", version=0):
        requested = await self.request(snap, key, version)
        self.assertEqual(requested["deployment"]["state"], "pending")
        self.assertIsNone(requested["status"]["active_revision_id"])
        self.assertEqual((await self.execute(snap))["state"], "succeeded")
        return await self.deployments.reconcile("primary")

    async def enqueue(self, snap, key):
        grant = self.policy.admission(snap[1], snap[2].fingerprint(), [])
        return await self.store.submit(
            key,
            {
                "runtime_profile": snap[1],
                "profile_digest": snap[2].fingerprint(),
                "secret_refs": [],
                "payload": {},
            },
            authorization=grant,
            revision_ref=snap[0],
            deployment_id="primary",
        )

    async def test_health_failure_keeps_known_good_version_and_no_duplicate_probe(self):
        old = await self.snapshot("old")
        ready = await self.activate(old)
        new = await self.snapshot("new")
        version = ready["deployment"]["version"]
        requests = await asyncio.gather(
            *(self.request(new, "upgrade", version) for _ in range(4))
        )
        ids = {r["receipt"]["rollout_id"] for r in requests}
        self.assertEqual(len(ids), 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.request(old, "upgrade", version)
        self.assertEqual((await self.execute(new, fail=True))["state"], "failed")
        failed = await self.deployments.reconcile("primary")
        self.assertEqual(failed["status"]["active_revision_id"], old[0])
        self.assertEqual(failed["status"]["error"], "DEPLOYMENT_HEALTH_FAILED")
        self.assertEqual(failed["deployment"]["state"], "ready")
        work = await self.enqueue(old, "after-failure")
        self.assertEqual((await self.execute(old))["id"], work["id"])
        async with self.db.read(self.scope) as c:
            probes = (
                await c.execute(select(rollouts).where(rollouts.c.scope == self.scope))
            ).all()
        self.assertEqual(len(probes), 2)

    async def test_natural_drain_restart_and_rollback_recheck_current_authority(self):
        old = await self.snapshot("old")
        ready = await self.activate(old)
        work = await self.enqueue(old, "existing-work")
        new = await self.snapshot("new")
        await self.request(new, "upgrade", ready["deployment"]["version"])
        self.assertEqual((await self.execute(new))["state"], "succeeded")
        draining = await self.deployments.reconcile("primary")
        self.assertEqual(draining["deployment"]["state"], "draining")
        self.assertEqual(draining["unsettled_runs"], 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "DEPLOYMENT_NOT_READY"):
            await self.enqueue(old, "blocked-admission")
        self.assertEqual((await self.store.get(work["id"]))["state"], "queued")
        reopened = Database(self.url)
        try:
            after_restart = Deployments(reopened, self.scope, self.policy)
            self.assertEqual(
                (await after_restart.show("primary"))["deployment"]["state"], "draining"
            )
            self.assertEqual(
                (await self.execute(old, store=RuntimeStore(reopened, self.scope)))[
                    "id"
                ],
                work["id"],
            )
            active = await after_restart.reconcile("primary")
            self.assertEqual(active["status"]["active_revision_id"], new[0])
            self.assertEqual(active["status"]["previous_revision_id"], old[0])
        finally:
            await reopened.close()
        rollback = await self.request(
            old, "undo", active["deployment"]["version"], rollback=True
        )
        self.assertNotEqual(rollback["probe_run_id"], work["id"])
        await self.execute(old)
        result = await self.deployments.reconcile("primary")
        self.assertEqual(result["status"]["active_revision_id"], old[0])
        detail = await self.deployments.show("primary")
        self.assertEqual(detail["rollout"]["operation"], "rollback")
        self.assertEqual(detail["events"][0]["type"], "deployment.activated")

    async def test_deadline_never_kills_probe_or_accepts_its_late_result(self):
        snap = await self.snapshot("old")
        requested = await self.request(snap, "create", 0)
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                rollouts.update()
                .where(rollouts.c.id == requested["receipt"]["rollout_id"])
                .values(deadline=0)
            )
        failed = await self.deployments.reconcile("primary")
        self.assertEqual(failed["status"]["error"], "DEPLOYMENT_HEALTH_DEADLINE")
        self.assertEqual(
            (await self.store.get(requested["probe_run_id"]))["state"], "queued"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "DEPLOYMENT_PROBE_UNSETTLED"):
            await self.request(snap, "retry", failed["deployment"]["version"])
        await self.execute(snap)
        self.assertEqual(
            (await self.deployments.reconcile("primary"))["deployment"]["state"],
            "failed",
        )

    async def test_policy_revoked_after_canary_cannot_activate_or_forge_binding(self):
        snap = await self.snapshot("old")
        await self.request(snap, "create", 0)
        await self.execute(snap)
        config = json.loads(self.policy.path.read_text())
        config["profiles"][snap[1]]["permissions"] = {}
        self.policy.path.write_text(json.dumps(config))
        rejected = await self.deployments.reconcile("primary")
        self.assertEqual(rejected["status"]["error"], "WORK_PERMISSION_LAYER_MISSING")
        self.assertIsNone(rejected["status"]["active_revision_id"])
        with self.assertRaisesRegex(
            RuntimeErrorCode, "DEPLOYMENT_BINDING_SERVER_OWNED"
        ):
            await self.store.submit("forged", {"deployment_id": "primary"})
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await Deployments(self.db, self.scope + "-foreign", self.policy).show(
                "primary"
            )


class SQLiteDeploymentTests(DeploymentScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgresDeploymentTests(DeploymentScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


if __name__ == "__main__":
    unittest.main()
