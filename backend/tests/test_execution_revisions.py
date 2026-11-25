"""Version-qualified queueing and live E2AG authorization across profile edits."""

import json
import os
import unittest
from uuid import uuid4

import test_runtime_capacity as fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.revisions import Revisions
from app.runtime.store import RuntimeStore
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from test_runtime_worker import FakeAdapter
from work_auth_fixtures import policy_fixture


def profile(command="old-command", mode="trusted-local"):
    return {
        "id": "versioned-agent",
        "bridge": "jsonl-v1",
        "launch": {"image": "sha256:" + "1" * 64, "command": [command]},
        "governance": {"mode": mode},
    }


class BindingScenarios:
    postgres = False
    asyncSetUp = fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = fixtures.CapacityScenarios.asyncTearDown

    async def snapshot(self, raw=None):
        value = compile_profile(raw or profile())
        row = await Revisions(self.db, self.scope).create(
            str(uuid4()), value, actor="test-operator"
        )
        ref = row["revision"]["id"]
        _, name, spec, _mode, refs = await load_profile(self.db, self.scope, ref)
        return ref, name, spec, refs

    async def enqueue(self, snapshot, key="run", authorization=None):
        ref, name, spec, refs = snapshot
        return await self.store.submit(
            key,
            {
                "runtime_profile": name,
                "profile_digest": spec.fingerprint(),
                "secret_refs": refs,
                "payload": {"message": "执行原版本"},
            },
            authorization=authorization,
            revision_ref=ref,
        )

    async def worker_id(self, store=None):
        pair = (str(uuid4()), str(uuid4()))
        await (store or self.store).register_worker(*pair, "fixture-host", os.getpid())
        return pair

    async def test_new_version_skips_old_queue_without_claiming_or_failing_it(self):
        old = await self.snapshot()
        new = await self.snapshot(profile("new-command"))
        first = await self.enqueue(old, "old")
        second = await self.enqueue(new, "new")
        worker = await self.worker_id()
        claimed = await self.store.claim(
            *worker,
            profile_id=new[1],
            profile_digest=new[2].fingerprint(),
            revision_ref=new[0],
        )
        self.assertEqual(claimed["run_id"], second["id"])
        original = await self.store.get(first["id"])
        self.assertEqual(original["state"], "queued")
        self.assertEqual(original["attempts"], [])
        self.assertEqual(original["input"]["revision_ref"], old[0])
        unversioned = await self.worker_id()
        self.assertIsNone(
            await self.store.claim(
                *unversioned, profile_id=old[1], profile_digest=old[2].fingerprint()
            )
        )

    async def test_binding_validation_is_atomic_and_scope_limited(self):
        snap = await self.snapshot()
        value = {
            "runtime_profile": snap[1],
            "profile_digest": snap[2].fingerprint(),
            "secret_refs": [],
        }
        with self.assertRaisesRegex(
            RuntimeErrorCode, "REVISION_EXECUTION_BINDING_INVALID"
        ):
            await self.store.submit(
                "bad", dict(value, profile_digest="0" * 64), revision_ref=snap[0]
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "REVISION_BINDING_SERVER_OWNED"):
            await self.store.submit("spoof", dict(value, revision_ref=snap[0]))
        other = RuntimeStore(self.db, self.scope + "-foreign")
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await other.submit("cross-scope", value, revision_ref=snap[0])
        run = await self.store.submit("bad", value, revision_ref=snap[0])
        self.assertEqual(run["state"], "queued")

    async def test_e2ag_required_and_revision_cannot_bypass_current_policy(self):
        snap = await self.snapshot(profile(mode="e2ag"))
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_AUTHORIZATION_REQUIRED"):
            await self.enqueue(snap)
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_AUTHORIZATION_REQUIRED"):
            await self.store.submit(
                "forged-grant",
                {
                    "runtime_profile": snap[1],
                    "profile_digest": snap[2].fingerprint(),
                    "secret_refs": [],
                    "work_authorization_ref": "forged-work",
                },
                revision_ref=snap[0],
            )
        policy, grant = policy_fixture(self.root, snap[1], snap[2])
        run = await self.enqueue(snap, authorization=grant)
        config = json.loads(policy.path.read_text())
        config["profiles"] = {}
        policy.path.write_text(json.dumps(config))
        adapter = FakeAdapter()
        worker = Worker(
            self.store,
            adapter,
            snap[1],
            snap[2],
            LocalArtifacts(self.root / "artifacts"),
            revision_ref=snap[0],
            authorization_verifier=WorkAuthorizationVerifier(self.store, policy),
            poll_interval=0.01,
        )
        await worker.once()
        self.assertEqual(adapter.started, 0)
        self.assertNotEqual((await self.store.get(run["id"]))["state"], "succeeded")

    async def test_restart_loads_old_snapshot_while_new_profile_and_policy_coexist(
        self,
    ):
        raw = profile(mode="e2ag")
        old = await self.snapshot(raw)
        policy, grant = policy_fixture(self.root, old[1], old[2])
        old_config = json.loads(policy.path.read_text())
        queued = await self.enqueue(old, "before-edit", grant)
        raw["launch"]["command"] = ["new-command"]
        newer = await self.snapshot(raw)
        policy, new_grant = policy_fixture(self.root, newer[1], newer[2])
        config = json.loads(policy.path.read_text())
        config["profiles"][old[1]]["revisions"] = {
            old[2].fingerprint(): old_config["profiles"][old[1]]
        }
        policy.path.write_text(json.dumps(config))
        self.assertEqual(
            policy.admission(old[1], old[2].fingerprint(), [])["policy_digest"],
            grant["policy_digest"],
        )
        await self.enqueue(newer, "after-edit", new_grant)
        reopened = Database(self.url)
        try:
            _, name, spec, _, refs = await load_profile(reopened, self.scope, old[0])
            self.assertEqual(spec.command, ("old-command",))
            store = RuntimeStore(reopened, self.scope)
            adapter = FakeAdapter()
            worker = Worker(
                store,
                adapter,
                name,
                spec,
                LocalArtifacts(self.root / "restarted"),
                revision_ref=old[0],
                authorization_verifier=WorkAuthorizationVerifier(store, policy),
                secret_requests=refs,
                poll_interval=0.01,
            )
            result = await worker.once()
            self.assertEqual(result["id"], queued["id"])
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(adapter.started, 1)
        finally:
            await reopened.close()
        # Removing an old version is an effective revocation, even while the new version remains allowed.
        del config["profiles"][old[1]]["revisions"][old[2].fingerprint()]
        policy.path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_PROFILE_CHANGED"):
            policy.admission(old[1], old[2].fingerprint(), [])
        self.assertEqual(
            policy.admission(newer[1], newer[2].fingerprint(), [])["policy_digest"],
            new_grant["policy_digest"],
        )


class SQLiteBindingTests(BindingScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_idempotency_receipt_cannot_alias_a_different_revision(self):
        old = await self.snapshot()
        raw = profile()
        raw["console"] = {"description": "Another immutable configuration"}
        new = await self.snapshot(raw)
        self.assertEqual(old[2].fingerprint(), new[2].fingerprint())
        value = {
            "runtime_profile": old[1],
            "profile_digest": old[2].fingerprint(),
            "secret_refs": [],
        }
        await self.store.submit(
            "receipt", value, submission_digest="a" * 64, revision_ref=old[0]
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.store.submit(
                "receipt", value, submission_digest="a" * 64, revision_ref=new[0]
            )


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgresBindingTests(BindingScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


if __name__ == "__main__":
    unittest.main()
