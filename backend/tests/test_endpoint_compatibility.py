"""Versioned execution stops on changed declarations without replaying remote work."""

import asyncio
import os
import unittest
from unittest.mock import patch

import test_runtime_a2a_outbound as remote_fixtures
import test_runtime_capacity as capacity_fixtures
from a2a.types import AgentCard
from app.runtime.compatibility_schema import endpoints
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.endpoint_compatibility import Compatibility, a2a_snapshot
from app.runtime.execution_revisions import compile_profile
from app.runtime.revisions import Revisions
from app.runtime.store import digest
from sqlalchemy import update


class CompatibilityScenarios:
    postgres = False
    asyncSetUp = capacity_fixtures.CapacityScenarios.asyncSetUp
    asyncTearDown = capacity_fixtures.CapacityScenarios.asyncTearDown

    async def test_changed_then_restored_declaration_still_requires_new_review(self):
        manager = Compatibility(self.db, self.scope)
        profile = digest("profile")
        old = await manager.observe(profile, {"version": "1"})
        accepted = await manager.accept(
            profile,
            old["version"],
            old["snapshot_digest"],
            "operator",
            "Verified contract",
        )
        same = await manager.observe(profile, {"version": "1"})
        self.assertEqual(same["version"], accepted["version"])
        changed = await manager.observe(profile, {"version": "2"})
        self.assertEqual(changed["state"], "review_required")
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await manager.accept(
                profile,
                accepted["version"],
                old["snapshot_digest"],
                "operator",
                "Stale review",
            )
        restored = await manager.observe(profile, {"version": "1"})
        self.assertIsNone(restored["accepted_digest"])
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(
                RuntimeErrorCode, "COMPATIBILITY_REVIEW_REQUIRED"
            ):
                await manager.require_accepted(c, profile)
        history = (await manager.show(profile))["events"]
        self.assertEqual(len(history), 4)
        self.assertEqual(history[-1]["snapshot"], {"version": "1"})
        with self.assertRaisesRegex(RuntimeErrorCode, "NOT_FOUND"):
            await Compatibility(self.db, self.scope + "other").show(profile)

    async def test_network_recovery_freshness_and_stale_observation_fencing(self):
        manager = Compatibility(self.db, self.scope)
        profile = digest("profile")
        one = await manager.observe(profile, {"version": "1"}, expected_version=0)
        accepted = await manager.accept(
            profile,
            one["version"],
            one["snapshot_digest"],
            "operator",
            "Verified contract",
        )
        await manager.unavailable(
            profile, "PROVIDER_NETWORK_ERROR", expected_version=accepted["version"]
        )
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(
                RuntimeErrorCode, "COMPATIBILITY_OBSERVATION_STALE"
            ):
                await manager.require_accepted(c, profile)
        restored = await manager.observe(profile, {"version": "1"})
        self.assertEqual(restored["state"], "accepted")
        with self.assertRaisesRegex(RuntimeErrorCode, "VERSION_CONFLICT"):
            await manager.observe(
                profile, {"version": "2"}, expected_version=accepted["version"]
            )
        await manager.unavailable(
            profile, "PROVIDER_NETWORK_ERROR", expected_version=accepted["version"]
        )
        self.assertIsNone((await manager.show(profile))["error"])
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                update(endpoints)
                .where(endpoints.c.scope == self.scope)
                .values(observed_at=0)
            )
        self.assertTrue((await manager.show(profile))["observed_stale"])
        with self.assertRaisesRegex(
            RuntimeErrorCode, "COMPATIBILITY_OBSERVATION_STALE"
        ):
            await manager.accept(
                profile,
                restored["version"],
                one["snapshot_digest"],
                "operator",
                "Too old",
            )

    async def test_concurrent_reviews_have_one_winner_and_survive_reopen(self):
        manager = Compatibility(self.db, self.scope)
        profile = digest("profile")
        row = await manager.observe(profile, {"version": "1"})
        results = await asyncio.gather(
            *(
                manager.accept(
                    profile,
                    row["version"],
                    row["snapshot_digest"],
                    "operator",
                    "Reviewed",
                )
                for _ in range(4)
            ),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(x, dict) for x in results), 1)
        self.assertEqual(
            sum(
                isinstance(x, RuntimeErrorCode) and x.code == "VERSION_CONFLICT"
                for x in results
            ),
            3,
        )
        db = Database(self.db.engine.url.render_as_string(hide_password=False))
        try:
            self.assertEqual(
                (await Compatibility(db, self.scope).show(profile))["state"], "accepted"
            )
        finally:
            await db.close()


class SQLiteCompatibilityTests(
    CompatibilityScenarios, unittest.IsolatedAsyncioTestCase
):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"),
    "PostgreSQL qualification requires isolated URL",
)
class PostgreSQLCompatibilityTests(
    CompatibilityScenarios, unittest.IsolatedAsyncioTestCase
):
    postgres = True


class RemoteCompatibilityTests(remote_fixtures.OutboundTests):
    async def test_deployment_activation_rejects_changed_or_reapproved_new_declaration(
        self,
    ):
        from app.runtime.deployments import Deployments

        ref = await self.publish()
        await self.accept_card()
        prepared = await self.console.prepare_submission(
            "review", "input", "qualification"
        )
        probe_input = {
            "payload": {"sample": prepared["value"]},
            "input_registration": {
                "result_schema": prepared["registered"]["contract"]["result_schema"]
            },
        }
        manager = Deployments(self.db, "one", self.policy)
        await manager.request("external", ref, "rollout", 0, "operator", probe_input)
        self.assertEqual(
            (await self.worker(revision_ref=ref).once())["state"], "succeeded"
        )
        self.card["version"] = "2"
        await self.accept_card()
        rejected = await manager.reconcile("external")
        self.assertEqual(rejected["deployment"]["state"], "failed")
        self.assertIsNone(rejected["status"]["active_revision_id"])
        self.assertEqual(
            rejected["status"]["error"], "COMPATIBILITY_DECLARATION_CHANGED"
        )

    async def publish(self):
        row = await Revisions(self.db, "one").create(
            "versioned", compile_profile(self.profile), actor="operator"
        )
        return row["revision"]["id"]

    async def versioned_submit(self, ref, key):
        original = self.console.store.submit

        async def bound(*args, **kwargs):
            kwargs["revision_ref"] = ref
            return await original(*args, **kwargs)

        with patch.object(self.console.store, "submit", side_effect=bound):
            return await self.submit(key)

    async def accept_card(self):
        manager = Compatibility(self.db, "one")
        snapshot = await manager.observe(
            self.spec.fingerprint(),
            a2a_snapshot(AgentCard.model_validate(self.card), self.spec),
        )
        return await manager.accept(
            self.spec.fingerprint(),
            snapshot["version"],
            snapshot["snapshot_digest"],
            "operator",
            "Protocol and evidence contract reviewed",
        )

    async def test_unreviewed_and_changed_versions_never_send_and_reapproval_executes(
        self,
    ):
        ref = await self.publish()
        await self.versioned_submit(ref, "unreviewed")
        result = await self.worker(revision_ref=ref).once()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.submit_count, 0)
        self.assertEqual(
            (await Compatibility(self.db, "one").show(self.spec.fingerprint()))[
                "state"
            ],
            "review_required",
        )
        await self.accept_card()
        self.card["version"] = "2"
        await self.versioned_submit(ref, "changed")
        result = await self.worker(revision_ref=ref).once()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.submit_count, 0)
        await self.accept_card()
        await self.versioned_submit(ref, "accepted")
        result = await self.worker(revision_ref=ref).once()
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(self.submit_count, 1)
        self.assertEqual(
            result["attempts"][0]["remote"]["a2a"]["declaration_digest"],
            (await Compatibility(self.db, "one").show(self.spec.fingerprint()))[
                "accepted_digest"
            ],
        )

    async def test_capability_change_with_same_version_triggers_review(self):
        ref = await self.publish()
        await self.accept_card()
        self.card["capabilities"]["streaming"] = True
        await self.versioned_submit(ref, "capabilities")
        result = await self.worker(revision_ref=ref).once()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.submit_count, 0)

    async def test_incompatible_protocol_then_restoration_still_requires_review(self):
        ref = await self.publish()
        await self.accept_card()
        self.card["protocolVersion"] = "1.0"
        await self.versioned_submit(ref, "incompatible")
        self.assertEqual(
            (await self.worker(revision_ref=ref).once())["state"], "failed"
        )
        self.card["protocolVersion"] = "0.3.0"
        await self.versioned_submit(ref, "restored")
        self.assertEqual(
            (await self.worker(revision_ref=ref).once())["state"], "failed"
        )
        self.assertEqual(self.submit_count, 0)

    async def test_operator_api_review_cas_and_rollback_visibility(self):
        ref = await self.publish()
        path = "/api/os/runtime/revisions/" + ref + "/compatibility"
        denied = await self.client.get(path)
        self.assertEqual(denied.status_code, 401)
        initial = await self.client.get(path, headers=self.headers)
        self.assertEqual(initial.json()["implementation_visibility"], "opaque")
        self.assertEqual(initial.json()["configuration_rollback"], "supported")
        self.assertEqual(initial.json()["implementation_rollback"], "unsupported")
        await self.accept_card()
        row = (await self.client.get(path, headers=self.headers)).json()[
            "qualification"
        ]
        body = {
            "expected_version": row["version"],
            "snapshot_digest": row["snapshot_digest"],
            "evidence": "Protocol review fixture",
        }
        result = await self.client.post(
            path + "/accept", json=body, headers=self.headers
        )
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(
            result.json()["qualification"]["events"][0]["actor"], "operator:console"
        )
        conflict = await self.client.post(
            path + "/accept", json=body, headers=self.headers
        )
        self.assertGreaterEqual(conflict.status_code, 400)

    async def test_recovery_of_original_task_survives_declaration_change(self):
        ref = await self.publish()
        await self.accept_card()
        self.fail_get = True
        run = await self.versioned_submit(ref, "observing")
        result = await self.worker(revision_ref=ref).once()
        self.assertEqual(result["state"], "reconciliation_required")
        self.card["version"] = "2"
        await Compatibility(self.db, "one").observe(
            self.spec.fingerprint(),
            a2a_snapshot(AgentCard.model_validate(self.card), self.spec),
        )
        self.fail_get = False
        await self.due(run["id"])
        await self.worker(revision_ref=ref).recover()
        self.assertEqual(
            (await self.console.store.get(run["id"]))["state"], "succeeded"
        )
        self.assertEqual(self.submit_count, 1)


def load_tests(loader, tests, pattern):
    result = unittest.TestSuite()
    for cls in (SQLiteCompatibilityTests, PostgreSQLCompatibilityTests):
        result.addTests(loader.loadTestsFromTestCase(cls))
    result.addTests(
        RemoteCompatibilityTests(name)
        for name in RemoteCompatibilityTests.__dict__
        if name.startswith("test_")
    )
    return result
