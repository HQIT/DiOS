"""Only proven-unsent requests may retry initial discovery; remote sends never replay."""

import asyncio
import unittest
from unittest.mock import patch

from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.fault_schema import plans, circuits
from app.runtime.schema import attempts
from app.runtime.store import RuntimeStore
import test_runtime_a2a_outbound as fixture


class PreflightTests(fixture.OutboundTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.card_status = 503
        self.cards = 0
        self.card_entered = asyncio.Event()
        self.card_release = asyncio.Event()
        self.hold_card = False

    async def peer(self, request):
        if request.method == "GET":
            self.cards += 1
            if self.hold_card:
                self.card_entered.set()
                await self.card_release.wait()
            if self.card_status != 200:
                return self.response({}, self.card_status, {"retry-after": "120"})
        return await super().peer(request)

    async def due_preflight(self, run_id):
        async with self.db.transaction("one") as c:
            await c.execute(
                plans.update().where(plans.c.run_id == run_id).values(retry_at=0)
            )
            await c.execute(
                circuits.update().where(circuits.c.scope == "one").values(retry_at=0)
            )

    async def test_retry_persists_across_restart_with_one_actual_remote_submission(
        self,
    ):
        run = await self.submit()
        first = await self.worker().once()
        self.assertEqual(first["state"], "retry_wait")
        self.assertEqual(first["recovery"]["reason"], "RETRY_UNLAUNCHED")
        self.assertGreaterEqual(
            first["recovery"]["retry_at"] - first["updated_at"], 119
        )
        self.assertEqual(first["attempts"][0]["remote"], {})
        await self.db.close()
        self.db = Database("sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"))
        self.console.db = self.db
        self.console.store = RuntimeStore(self.db, "one")
        self.assertEqual((await self.worker().once())["state"], "idle")
        self.assertEqual(self.cards, 1)
        self.card_status = 200
        await self.due_preflight(run["id"])
        final = await self.worker().once()
        self.assertEqual(final["id"], run["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(len(final["attempts"]), 2)
        self.assertEqual(self.submit_count, 1)
        self.assertEqual(final["recovery"]["circuit"]["state"], "closed")
        self.assertEqual((await self.submit())["id"], run["id"])

    async def test_bounded_preflight_budget_and_no_remote_side_effect(self):
        run = await self.submit()
        for index in range(3):
            final = await self.worker().once()
            self.assertEqual(final["state"], "retry_wait" if index < 2 else "failed")
            if index < 2:
                await self.due_preflight(run["id"])
        self.assertEqual(final["recovery"]["reason"], "RETRY_BUDGET_EXHAUSTED")
        self.assertEqual(len(final["attempts"]), 3)
        self.assertEqual(self.submit_count, 0)

    async def test_unauthorized_discovery_is_terminal(self):
        self.card_status = 401
        await self.submit()
        final = await self.worker().once()
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["recovery"]["category"], "authorization")
        self.assertEqual(self.submit_count, 0)

    async def test_only_one_recovery_probe_across_workers(self):
        run = await self.submit()
        await self.worker().once()
        await self.submit("second")
        await self.due_preflight(run["id"])
        self.card_status = 200
        self.hold_card = True
        pending = asyncio.create_task(self.worker().once())
        await asyncio.wait_for(self.card_entered.wait(), 5)
        self.assertEqual((await self.worker().once())["state"], "idle")
        self.assertEqual(self.cards, 2)
        self.card_release.set()
        self.assertEqual((await pending)["state"], "succeeded")

    async def test_cancellation_or_revocation_during_wait_prevents_send(self):
        for index, operation in enumerate(["cancel", "revoke"]):
            run = await self.submit(str(index))
            await self.due_preflight(run["id"])
            current = await self.worker().once()
            self.assertEqual(current["state"], "retry_wait")
            await getattr(self.console.store, operation)(run["id"], current["version"])
            await self.due_preflight(run["id"])
            self.assertEqual((await self.worker().once())["state"], "idle")
            final = await self.console.store.get(run["id"])
            self.assertIn(final["state"], {"failed", "cancelled"})
        self.assertEqual(self.submit_count, 0)

    async def test_changed_identity_at_retry_does_not_restore_old_grant(self):
        run = await self.submit()
        await self.worker().once()
        await self.due_preflight(run["id"])
        worker = self.worker()
        with patch.object(
            worker, "authority", side_effect=RuntimeErrorCode("AUTH_PRINCIPAL_REVOKED")
        ):
            final = await worker.once()
        self.assertEqual(final["state"], "failed")
        self.assertEqual(self.cards, 1)
        self.assertEqual(self.submit_count, 0)

    async def test_late_launch_evidence_invalidates_preflight_retry(self):
        run = await self.submit()
        first = await self.worker().once()
        async with self.db.transaction("one") as c:
            await c.execute(
                attempts.update()
                .where(attempts.c.id == first["attempt_id"])
                .values(remote={"a2a": {"stage": "dispatched"}})
            )
        await self.due_preflight(run["id"])
        self.assertEqual((await self.worker().once())["state"], "idle")
        final = await self.console.store.get(run["id"])
        self.assertEqual(final["recovery"]["reason"], "REPLAY_DISABLED")
        self.assertEqual(self.submit_count, 0)

    async def test_ambiguous_post_is_never_retried_after_successful_discovery(self):
        self.card_status = 200
        self.fail_submit = True
        run = await self.submit()
        first = await self.worker().once()
        self.assertEqual(first["state"], "reconciliation_required")
        await self.due(run["id"])
        await self.worker().recover()
        self.assertEqual((await self.worker().once())["state"], "idle")
        self.assertEqual(self.submit_count, 1)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        PreflightTests(name)
        for name in PreflightTests.__dict__
        if name.startswith("test_")
    )
