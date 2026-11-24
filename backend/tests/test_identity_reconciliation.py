"""Source rechecks preserve approvals and fence crashed/replaced reconcilers."""

import asyncio
import json
import unittest
from unittest.mock import patch

import test_runtime_oauth_sessions as sessions
import test_runtime_oauth_source as fixtures
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.identity_check_schema import checks
from app.runtime.identity_reconciliation import IdentityReconciler
from app.runtime.recovery import backup_sqlite, verify_backup
from app.runtime.store import RuntimeStore
from app.runtime.work_auth_schema import decisions
from app.runtime.work_authorizations import view
from sqlalchemy import select


class IdentityReconciliationTests(
    fixtures.SourceFixture, unittest.IsolatedAsyncioTestCase
):
    async def asyncSetUp(self):
        await sessions.MultiSourceTests.asyncSetUp(self)
        self.reconciler = IdentityReconciler(self.console.store, self.policy)

    asyncTearDown = fixtures.OAuthPrincipalTests.asyncTearDown
    save = sessions.MultiSourceTests.save
    add_source = sessions.MultiSourceTests.add_source
    headers_for = sessions.MultiSourceTests.headers_for
    submit_as = sessions.MultiSourceTests.submit_as
    get = sessions.MultiSourceTests.get
    context = sessions.MultiSourceTests.context

    async def due(self, run_id):
        async with self.db.transaction("one") as c:
            await c.execute(
                checks.update()
                .where(checks.c.run_id == run_id)
                .values(next_check_at=0, lease_until=0)
            )

    async def test_confirmed_revocation_is_persistent_and_peer_survives(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        await self.verifier(context, "execute")
        peer = await self.submit_as("peer", "peer")
        self.claims[self.access]["active"] = False
        result = await self.reconciler.batch()
        self.assertEqual({x["outcome"] for x in result["items"]}, {"valid", "revoked"})
        detail = await view(self.console.store, run["id"])
        self.assertEqual(detail["state"], "revoked")
        self.assertEqual(detail["grant"]["state"], "revoked")
        self.assertEqual(
            (await view(self.console.store, peer["id"]))["state"], "active"
        )
        self.claims[self.access]["active"] = True
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await self.verifier(context, "execute")
        self.assertEqual(
            (await self.console.store.get(run["id"]))["state"], "executing"
        )

    async def test_outage_preserves_work_then_automatically_recovers(self):
        run = await self.submit_as("a")
        context = await self.context(run)
        await self.verifier(context, "execute")
        before = await view(self.console.store, run["id"])
        self.statuses[self.access] = 503
        result = await self.reconciler.batch()
        self.assertEqual(result["items"][0]["outcome"], "unavailable")
        failed = await view(self.console.store, run["id"])
        self.assertEqual(failed["state"], "active")
        self.assertEqual(failed["grant"], before["grant"])
        self.assertEqual(failed["source_check"]["failures"], 1)
        self.assertEqual((await self.reconciler.batch())["checked"], 0)
        await self.due(run["id"])
        await self.reconciler.batch()
        failed = await view(self.console.store, run["id"])
        self.assertAlmostEqual(
            failed["source_check"]["next_check_at"]
            - failed["source_check"]["checked_at"],
            30,
            places=2,
        )
        self.statuses.clear()
        await self.due(run["id"])
        await self.reconciler.batch()
        after = await view(self.console.store, run["id"])
        self.assertEqual(after["source_check"]["outcome"], "valid")
        self.assertEqual(after["source_check"]["failures"], 0)
        self.assertEqual(after["expires_at"], before["expires_at"])
        self.assertEqual(after["grant"], before["grant"])

    async def test_concurrent_process_claims_are_exclusive(self):
        run = await self.submit_as("a")
        db2 = Database(str(self.db.engine.url))
        other = IdentityReconciler(RuntimeStore(db2, "one"), self.policy)
        try:
            claims = await asyncio.gather(self.reconciler.claim(), other.claim())
            self.assertEqual(sum(x is not None for x in claims), 1)
            self.assertEqual(next(x for x in claims if x)["run_id"], run["id"])
        finally:
            await db2.close()

    async def test_crash_lease_expiry_and_stale_response_fence(self):
        run = await self.submit_as("a")
        old = await self.reconciler.claim()
        self.assertIsNone(await self.reconciler.claim())
        await self.due(run["id"])
        new = await self.reconciler.claim()
        self.assertGreater(new["generation"], old["generation"])
        result = await self.reconciler.finish(old, "revoked", "AUTH_PRINCIPAL_REVOKED")
        self.assertEqual(result["outcome"], "stale")
        await self.reconciler.finish(new, "valid", "IDENTITY_SOURCE_CURRENT")
        self.assertEqual((await view(self.console.store, run["id"]))["state"], "active")

    async def test_late_success_cannot_resurrect_explicit_revocation(self):
        run = await self.submit_as("a")
        claim = await self.reconciler.claim()
        await self.console.store.revoke(run["id"], run["version"])
        result = await self.reconciler.finish(claim, "valid", "IDENTITY_SOURCE_CURRENT")
        self.assertEqual(result["outcome"], "superseded")
        self.assertEqual(
            (await view(self.console.store, run["id"]))["state"], "revoked"
        )

    async def test_network_check_holds_no_database_transaction(self):
        run = await self.submit_as("a")
        claim = await self.reconciler.claim()
        await asyncio.wait_for(
            self.console.store.register_worker("other", "other", "host", 2), timeout=1
        )
        await self.reconciler.finish(claim, "valid", "IDENTITY_SOURCE_CURRENT")
        self.assertEqual(
            (await view(self.console.store, run["id"]))["source_check"]["outcome"],
            "valid",
        )

    async def test_bounded_batches_cover_all_eligible_work(self):
        ids = {(await self.submit_as("a", str(i)))["id"] for i in range(3)}
        observed = []
        for _ in range(3):
            observed.extend(
                x["run_id"] for x in (await self.reconciler.batch(limit=1))["items"]
            )
        self.assertEqual(set(observed), ids)
        self.assertEqual(len(observed), 3)
        self.assertEqual((await self.reconciler.batch(limit=1))["checked"], 0)

    async def test_repeated_valid_checks_have_one_transition_audit(self):
        run = await self.submit_as("a")
        await self.reconciler.batch()
        await self.due(run["id"])
        await self.reconciler.batch()
        async with self.db.read("one") as c:
            records = (
                (
                    await c.execute(
                        select(decisions).where(
                            decisions.c.run_id == run["id"],
                            decisions.c.action == "identity.reconcile",
                        )
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(records), 1)
        self.assertNotIn(self.access, json.dumps([dict(x) for x in records]))

    async def test_restart_keeps_backoff_and_scope_isolation(self):
        run = await self.submit_as("a")
        self.statuses[self.access] = 503
        await self.reconciler.batch()
        db2 = Database(str(self.db.engine.url))
        try:
            restarted = IdentityReconciler(RuntimeStore(db2, "one"), self.policy)
            self.assertEqual((await restarted.batch())["checked"], 0)
            self.assertEqual(
                (
                    await IdentityReconciler(
                        RuntimeStore(db2, "other"), self.policy
                    ).batch()
                )["checked"],
                0,
            )
        finally:
            await db2.close()
        self.assertEqual(
            (await view(self.console.store, run["id"]))["source_check"]["outcome"],
            "unavailable",
        )

    async def test_backup_covers_observations_and_schema(self):
        await self.submit_as("a")
        await self.reconciler.batch()
        self.console.provider.root.mkdir(exist_ok=True)
        target = self.root / "backup"
        await asyncio.to_thread(
            backup_sqlite, str(self.db.engine.url), self.console.provider.root, target
        )
        result = await asyncio.to_thread(verify_backup, target)
        self.assertIsNotNone(result)

    async def test_invalid_configuration_and_provider_failure(self):
        for value in (0, -1, True, float("nan"), 4000):
            with self.assertRaises(RuntimeErrorCode):
                IdentityReconciler(self.console.store, self.policy, interval=value)
        run = await self.submit_as("a")
        with patch.object(self.policy, "identity_provider", return_value=None):
            result = await self.reconciler.batch()
        self.assertEqual(result["items"][0]["reason"], "AUTH_PROVIDER_UNAVAILABLE")
        self.assertEqual((await view(self.console.store, run["id"]))["state"], "active")
