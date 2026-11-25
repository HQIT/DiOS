"""Persisted remote cooldown, single observation probe and original Work recovery."""

import asyncio
import json
import unittest
from email.utils import formatdate

import test_runtime_a2a_outbound as fixture
from app.runtime.a2a_faults import A2AFault, http_code, retry_after
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.fault_schema import circuits
from app.runtime.schema import attempts
from sqlalchemy import select


class RecoveryTests(fixture.OutboundTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.remote_failure = None
        self.get_count = 0
        self.probe_started = asyncio.Event()
        self.probe_release = asyncio.Event()
        self.hold_probe = False
        self.download_failure = False

    async def peer(self, request):
        if str(request.url) == "https://remote.example/artifacts/result.json":
            if self.download_failure:
                return self.response({}, 503)
            return self.response(
                {"summary": "Recovered transfer", "evidence": ["fixture:verified"]}
            )
        if request.method == "POST":
            body = json.loads(request.content)
            if body["method"] == "tasks/get":
                self.get_count += 1
                if self.hold_probe:
                    self.probe_started.set()
                    await self.probe_release.wait()
                if self.remote_failure == "rate":
                    return self.response({}, 429, {"retry-after": "120"})
                if self.remote_failure == "quota":
                    return self.response(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {
                                "code": -32000,
                                "message": "Quota exhausted",
                                "data": {"dios": {"code": "PROVIDER_QUOTA_EXHAUSTED"}},
                            },
                        }
                    )
        return await super().peer(request)

    async def test_completed_result_download_recovers_without_resubmission(self):
        self.complete()
        self.remote["artifacts"][0]["parts"] = [
            {
                "kind": "file",
                "file": {
                    "mimeType": "application/json",
                    "uri": "https://remote.example/artifacts/result.json",
                },
            }
        ]
        self.download_failure = True
        run = await self.submit()
        first = await self.worker().once()
        self.assertEqual("reconciliation_required", first["state"])
        self.assertEqual(
            "completed", first["attempts"][0]["remote"]["a2a"]["observed_state"]
        )
        self.download_failure = False
        await self.due(run["id"])
        await self.worker().recover()
        self.assertEqual(
            "succeeded", (await self.console.store.get(run["id"]))["state"]
        )
        self.assertEqual(1, self.submit_count)
        self.assertEqual(0, self.get_count)

    async def test_preflight_limit_blocks_new_claims_for_retry_after(self):
        async def limited(request):
            return self.response({}, 429, {"retry-after": "120"})

        self.peer = limited
        await self.submit()
        first = await self.worker().once()
        self.assertEqual("failed", first["state"])
        self.assertEqual(0, self.submit_count)
        self.assertGreaterEqual(
            first["recovery"]["circuit"]["retry_at"] - first["updated_at"], 119
        )
        await self.submit("queued")
        self.assertEqual("idle", (await self.worker().once())["state"])

    async def test_rate_limit_persists_and_respects_retry_after_on_restart(self):
        self.remote_failure = "rate"
        run = await self.submit()
        first = await self.worker().once()
        recovery = first["recovery"]
        self.assertEqual("reconciliation_required", first["state"])
        self.assertEqual("rate_limit", recovery["category"])
        self.assertGreaterEqual(recovery["retry_at"] - recovery["updated_at"], 119)
        self.assertEqual("open", recovery["circuit"]["state"])
        await self.db.close()
        self.db = Database("sqlite+aiosqlite:///" + str(self.root / "runtime.sqlite"))
        self.console.db = self.db
        from app.runtime.store import RuntimeStore

        self.console.store = RuntimeStore(self.db, "one")
        count = self.get_count
        self.remote_failure = None
        await self.worker().recover()
        self.assertEqual(count, self.get_count)
        await self.submit("queued")
        self.assertEqual("idle", (await self.worker().once())["state"])
        await self.due(run["id"])
        await self.worker().recover()
        final = await self.console.store.get(run["id"])
        self.assertEqual("succeeded", final["state"])
        self.assertEqual("closed", final["recovery"]["circuit"]["state"])
        self.assertEqual(1, self.submit_count)
        self.assertEqual(1, len(final["attempts"]))

    async def test_quota_has_long_cooldown_and_single_probe_across_workers(self):
        self.remote_failure = "quota"
        run = await self.submit()
        first = await self.worker().once()
        self.assertEqual("quota", first["recovery"]["category"])
        self.assertGreaterEqual(
            first["recovery"]["retry_at"] - first["recovery"]["updated_at"], 149
        )
        await self.due(run["id"])
        self.remote_failure = None
        self.hold_probe = True
        one = asyncio.create_task(self.worker().recover())
        await asyncio.wait_for(self.probe_started.wait(), 3)
        before = self.get_count
        await asyncio.gather(self.worker().recover(), self.worker().recover())
        self.assertEqual(before, self.get_count)
        self.probe_release.set()
        await one
        self.assertEqual(
            "succeeded", (await self.console.store.get(run["id"]))["state"]
        )

    async def test_cancel_and_revoke_during_cooldown_still_reconcile_original(self):
        for index, operation in enumerate(["cancel", "revoke"]):
            self.remote = {
                "kind": "task",
                "id": "remote-" + str(index),
                "contextId": "context-" + str(index),
                "status": {"state": "working"},
            }
            self.remote_failure = "rate"
            run = await self.submit(str(index))
            await self.worker().once()
            current = await self.console.store.get(run["id"])
            await getattr(self.console.store, operation)(run["id"], current["version"])
            self.remote_failure = None
            await self.due(run["id"])
            await self.worker().recover()
            final = await self.console.store.get(run["id"])
            self.assertEqual(
                "cancelled" if operation == "cancel" else "failed", final["state"]
            )
            self.assertEqual("canceled", self.remote["status"]["state"])
            self.assertEqual(1, len(final["attempts"]))

    async def test_probe_crash_lease_expires_and_old_probe_cannot_clear_new_one(self):
        self.fail_get = True
        run = await self.submit()
        await self.worker().once()
        await self.due(run["id"])
        old = self.worker()
        current, attempt = await old.snapshot(run["id"])
        token = await old.recovery.permit(current, attempt, reconcile=True)
        self.assertTrue(token)
        new = self.worker()
        self.assertIsNone(await new.recovery.permit(current, attempt, reconcile=True))
        async with self.db.transaction("one") as c:
            row = (
                (
                    await c.execute(
                        select(attempts).where(attempts.c.id == attempt["id"])
                    )
                )
                .mappings()
                .one()
            )
            await c.execute(
                attempts.update()
                .where(attempts.c.id == attempt["id"])
                .values(
                    remote={
                        **row["remote"],
                        "a2a_poll": {**row["remote"]["a2a_poll"], "lease_until": 0},
                    }
                )
            )
        second = await new.recovery.permit(current, attempt, reconcile=True)
        self.assertTrue(second)
        self.assertNotEqual(token, second)
        with self.assertRaisesRegex(RuntimeErrorCode, "STALE_OWNER"):
            await old.recovery.success(current, attempt, token, reconcile=True)
        await new.recovery.release(current, attempt, second, reconcile=True)
        self.fail_get = False
        await new.recover()
        self.assertEqual(
            "succeeded", (await self.console.store.get(run["id"]))["state"]
        )

    async def test_late_non_probe_success_does_not_reopen_circuit(self):
        self.fail_get = True
        run = await self.submit()
        await self.worker().once()
        worker = self.worker()
        current, attempt = await worker.snapshot(run["id"])
        async with self.db.transaction("one") as c:
            await c.execute(
                circuits.update()
                .where(circuits.c.scope == "one")
                .values(state="open", probe_attempt=None)
            )
        # Generic outcome retains the existing shared circuit protection.
        from app.runtime.faults import outcome

        async with self.db.transaction("one") as c:
            await outcome(c, "one", attempt["id"], True, await self.db.now(c))
        self.assertEqual(
            "open",
            (await self.console.store.get(current["id"]))["recovery"]["circuit"][
                "state"
            ],
        )


class HTTPFailureTests(unittest.TestCase):
    def test_retry_after_and_explicit_quota_classification(self):
        self.assertEqual(20, retry_after("20"))
        self.assertEqual(20, retry_after(formatdate(1020, usegmt=True), now=1000))
        self.assertEqual(0, retry_after("later"))
        self.assertEqual(0, retry_after("-3"))
        self.assertEqual(86400, retry_after("999999999"))
        self.assertEqual("A2A_REMOTE_PAYMENT_REQUIRED", http_code(402))
        self.assertNotEqual("PROVIDER_QUOTA_EXHAUSTED", http_code(402))
        self.assertEqual("PROVIDER_RATE_LIMITED", http_code(429))
        self.assertEqual(30, A2AFault("PROVIDER_RATE_LIMITED", 30).retry_after)
