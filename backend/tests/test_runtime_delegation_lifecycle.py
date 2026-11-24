"""Actual OPA with transaction-owned delegation snapshots; no replicated data."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from sqlalchemy import select
import test_runtime_opa_integration as opa_fixtures
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from app.runtime.delegations import Delegations
from app.runtime.delegation_schema import receipts
from app.runtime.work_auth_schema import work, grants
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.policy_evaluator import OpaEvaluator


class DelegationLifecycleTests(opa_fixtures.OpaIntegrationTests):
    def delayed_policy(self):
        original=OpaEvaluator.evaluate
        async def delayed(instance,*args):
            result=await original(instance,*args)
            await asyncio.sleep(.7)
            return result
        return patch.object(OpaEvaluator,'evaluate',delayed)

    async def test_grant_expiring_during_delegation_check_blocks_heartbeat(self):
        async with self.db.transaction(self.scope) as c:
            await c.execute(grants.update().where(grants.c.attempt_id==self.attempt['id']).values(expires_at=await self.db.now(c)+.5))
        with self.delayed_policy():
            with self.assertRaisesRegex(RuntimeErrorCode,'EXECUTION_GRANT_EXPIRED'):
                await self.store.heartbeat(*self.binding)

    async def test_lease_expiring_during_delegation_check_blocks_heartbeat(self):
        from app.runtime.schema import attempts
        async with self.db.transaction(self.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==self.attempt['id']).values(lease_until=await self.db.now(c)+.5))
        with self.delayed_policy():
            with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):
                await self.store.heartbeat(*self.binding)

    async def test_call_expiring_during_delegation_check_never_dispatches(self):
        from app.runtime.tool_schema import invocations
        item=await self.approve(await self.request())
        async with self.db.transaction(self.scope) as c:
            await c.execute(invocations.update().where(invocations.c.id==item['id']).values(expires_at=await self.db.now(c)+.5))
        with self.delayed_policy():
            with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_AUTHORIZATION_EXPIRED'):
                await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[])

    def write_bundle(self, revision):
        super().write_bundle(revision)
        source = (
            Path(__file__).resolve().parents[2]
            / "examples/policies/opa-delegation/delegation.rego"
        )
        (self.bundle / "delegation.rego").write_text(source.read_text())

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.manager = Delegations(self.store)
        self.config = dict(
            kind="opa",
            url=self.url + "/v1/data/dios/delegation/allow",
            bundles={"/policy": "v1"},
            timeout_seconds=1,
        )
        policy = json.loads(self.policy.path.read_text())
        profile = policy["profiles"]["test-profile"]
        self.permissions = profile["permissions"]["caller"]
        self.deadline = time.time() + 600
        self.chain = []
        for i in range(4):
            values = dict(
                subject=policy["subject"],
                actor=policy["actor"] if i == 3 else "delegate:" + str(i),
                permissions=self.permissions,
                expires_at=self.deadline - i * 10,
            )
            if i:
                values.update(
                    parent_ref=self.chain[-1]["id"],
                    issuer_actor=self.chain[-1]["actor_ref"],
                )
            else:
                values["policy_evaluator"] = self.config
            self.chain.append(await self.manager.issue("level-" + str(i), **values))
        profile["delegation_ref"] = self.chain[-1]["id"]
        self.policy.path.write_text(json.dumps(policy))
        descriptor = self.policy.admission("test-profile", self.spec.fingerprint(), [])
        self.run = await self.store.submit(
            "delegated-run",
            dict(
                runtime_profile="test-profile",
                profile_digest=self.spec.fingerprint(),
                secret_refs=[],
            ),
            authorization=descriptor,
        )
        await self.store.register_worker(
            "delegated-worker", "instance", "host", os.getpid()
        )
        self.attempt = await self.store.claim("delegated-worker", "instance")
        self.binding = (
            self.run["id"],
            self.attempt["id"],
            "delegated-worker",
            "instance",
            self.attempt["epoch"],
        )
        self.context = WorkAuthorizationContext(
            self.scope,
            *self.binding[:2],
            "delegated-worker",
            "instance",
            self.attempt["epoch"],
            self.attempt["authorization_epoch"],
            None,
            self.run["input"]["work_authorization_ref"],
        )
        self.verifier = WorkAuthorizationVerifier(self.store, self.policy)
        await self.verifier(self.context, "execute")
        await self.store.report(*self.binding, 1, "preparing")
        await self.store.report(*self.binding, 2, "creating_session")
        await self.verifier(replace(self.context, session_id="session-one"), "execute")
        await self.store.report(
            *self.binding, 3, "waiting_model", session_id="session-one"
        )

    async def test_receipt_deadline_and_result_acceptance(self):
        async with self.db.read(self.scope) as c:
            saved = (
                (
                    await c.execute(
                        select(receipts).where(receipts.c.run_id == self.run["id"])
                    )
                )
                .mappings()
                .one()
            )
            authorization = (
                (await c.execute(select(work).where(work.c.run_id == self.run["id"])))
                .mappings()
                .one()
            )
        self.assertEqual(saved["binding"]["chain"], [x["id"] for x in self.chain])
        self.assertEqual(authorization["expires_at"], self.chain[-1]["expires_at"])
        await self.verifier(
            replace(self.context, session_id="session-one"), "accept_result"
        )
        await self.manager.revoke(self.chain[1]["id"], 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(
                replace(self.context, session_id="session-one"), "accept_result"
            )

    async def test_upstream_revoke_fences_grant_heartbeat_and_future_admission(self):
        await self.manager.revoke(self.chain[0]["id"], 1)
        async with self.db.read(self.scope) as c:
            grant = (
                (
                    await c.execute(
                        select(grants).where(grants.c.attempt_id == self.attempt["id"])
                    )
                )
                .mappings()
                .one()
            )
            before = (
                (
                    await c.execute(
                        select(receipts).where(receipts.c.run_id == self.run["id"])
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(grant["state"], "revoked")
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.store.heartbeat(*self.binding)
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(
                replace(self.context, session_id="session-one"), "execute"
            )
        descriptor = self.policy.admission("test-profile", self.spec.fingerprint(), [])
        with self.assertRaisesRegex(RuntimeErrorCode, "DELEGATION_REVOKED"):
            await self.store.submit(
                "after-revoke",
                dict(
                    runtime_profile="test-profile",
                    profile_digest=self.spec.fingerprint(),
                    secret_refs=[],
                ),
                authorization=descriptor,
            )
        async with self.db.read(self.scope) as c:
            after = (
                (
                    await c.execute(
                        select(receipts).where(receipts.c.run_id == self.run["id"])
                    )
                )
                .mappings()
                .one()
            )
        self.assertEqual(before, after)

    async def test_issuance_rejects_expansion_wrong_issuer_depth_and_revoked_parent(
        self,
    ):
        parent = self.chain[0]
        values = dict(
            subject=parent["subject_ref"],
            actor="child",
            permissions=self.permissions,
            expires_at=self.deadline - 20,
            parent_ref=parent["id"],
            issuer_actor=parent["actor_ref"],
        )
        for key, change, code in [
            (
                "expand",
                {
                    "permissions": self.permissions
                    + [dict(self.permission, action="shell.execute")]
                },
                "DELEGATION_DENIED",
            ),
            ("extend", {"expires_at": self.deadline + 1}, "DELEGATION_DENIED"),
            ("issuer", {"issuer_actor": "stranger"}, "DELEGATION_ISSUER_DENIED"),
        ]:
            with self.assertRaisesRegex(RuntimeErrorCode, code):
                await self.manager.issue(key, **dict(values, **change))
        with self.assertRaisesRegex(RuntimeErrorCode, "DELEGATION_DEPTH_EXCEEDED"):
            await self.manager.issue(
                "too-deep",
                **dict(
                    values,
                    parent_ref=self.chain[-1]["id"],
                    issuer_actor=self.chain[-1]["actor_ref"],
                ),
            )
        first = await self.manager.issue("stable", **values)
        same = await self.manager.issue("stable", **values)
        self.assertEqual(first, same)
        with self.assertRaisesRegex(RuntimeErrorCode, "IDEMPOTENCY_CONFLICT"):
            await self.manager.issue("stable", **dict(values, actor="changed"))
        await self.manager.revoke(parent["id"], 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "DELEGATION_REVOKED"):
            await self.manager.issue("late", **values)

    async def test_revoke_waits_for_snapshot_transaction_then_fences_work(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = self.manager.evaluator
        from app.runtime.policy_evaluator import OpaEvaluator

        class PausedEvaluator:
            async def evaluate(inner, *args):
                result = await OpaEvaluator().evaluate(*args)
                entered.set()
                await release.wait()
                return result

        self.manager.evaluator = PausedEvaluator()
        issuing = asyncio.create_task(
            self.manager.issue(
                "ordered-child",
                subject=self.chain[0]["subject_ref"],
                actor="ordered",
                permissions=self.permissions,
                expires_at=self.deadline - 30,
                parent_ref=self.chain[0]["id"],
                issuer_actor=self.chain[0]["actor_ref"],
            )
        )
        await asyncio.wait_for(entered.wait(), 3)
        revoking = asyncio.create_task(
            Delegations(self.store).revoke(self.chain[0]["id"], 1)
        )
        try:
            await asyncio.sleep(0.05)
            self.assertFalse(revoking.done())
        finally:
            release.set()
        child = await issuing
        await revoking
        self.manager.evaluator = original
        self.assertFalse((await self.manager.show(child["id"]))["effective"])

    async def test_backup_preserves_delegation_and_immutable_receipts(self):
        from app.runtime.recovery import restore_sqlite
        from app.runtime.database import Database
        from app.runtime.store import RuntimeStore
        from app.runtime.work_authorizations import view
        await self.manager.revoke(self.chain[0]['id'],1)
        before=await view(self.store,self.run['id'])
        destination = self.root / "delegation-backup"
        await asyncio.to_thread(
            backup_sqlite, str(self.db.engine.url), self.provider.root, destination
        )
        self.assertIn(
            "runtime_delegation_schema",
            (await asyncio.to_thread(verify_backup, destination))["schemas"],
        )
        restored=self.root/'restored'
        result=await restore_sqlite(destination,restored)
        self.assertEqual(result['mode'],'restore_hold')
        db=Database('sqlite+aiosqlite:///'+str(restored/'state.sqlite'))
        try:
            store=RuntimeStore(db,self.scope)
            after=await view(store,self.run['id'])
            self.assertEqual(after['delegation'],before['delegation'])
            self.assertEqual(after['state'],'revoked')
            self.assertFalse((await Delegations(store).show(self.chain[-1]['id']))['effective'])
        finally:await db.close()

    async def test_cli_history_and_idempotent_revocation(self):
        import subprocess, sys

        command = [
            sys.executable,
            "-m",
            "app.runtime.cli",
            "--database-url",
            str(self.db.engine.url),
            "--scope",
            self.scope,
            "delegation",
        ]
        result = await asyncio.to_thread(
            subprocess.run,
            command + ["show", self.chain[-1]["id"]],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(json.loads(result.stdout)["chain"]), 4)
        command += ["revoke", self.chain[0]["id"], "--version", "1"]
        for _ in range(2):
            result = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout)["state"], "revoked")

    async def test_untrusted_input_cannot_select_delegation(self):
        with self.assertRaisesRegex(RuntimeErrorCode, "WORK_AUTHORIZATION_REQUIRED"):
            await self.store.submit(
                "untrusted", {"delegation_ref": self.chain[-1]["id"]}
            )
