"""Usage snapshots preserve provenance, scope, replay identity and unknown values."""

import asyncio
import os
import unittest

import test_runtime_remote_control as remote_fixtures
import test_runtime_worker as worker_fixtures
from app.runtime.artifacts import LocalArtifacts
from app.runtime.bridge import JsonlBridge
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import encoded
from app.runtime.usage import view
from app.runtime.usage_schema import observations
from sqlalchemy import func, select
from test_runtime_capacity import CapacityScenarios


def frame(seq=1, amount="0.1", used=10, unit="USD"):
    return {
        "type": "usage",
        "session_id": "session",
        "sequence": seq,
        "kind": "acp_session",
        "quality": "reported",
        "values": {
            "used": used,
            "size": 20000,
            "cost": {"amount": amount, "unit": unit},
        },
    }


class UsageScenarios:
    postgres = False
    asyncTearDown = CapacityScenarios.asyncTearDown

    async def asyncSetUp(self):
        await CapacityScenarios.asyncSetUp(self)
        self.run = await self.store.submit("one", {"runtime_profile": "one"})
        who = await CapacityScenarios.worker(self)
        self.attempt = await self.store.claim(*who)
        self.binding = (self.run["id"], self.attempt["id"], *who, self.attempt["epoch"])
        await self.store.report(*self.binding, 1, "preparing")
        await self.store.report(*self.binding, 2, "creating_session")
        await self.store.report(*self.binding, 3, "waiting_model", session_id="session")
        self.provider = LocalArtifacts(self.root / "artifacts")

    async def test_live_snapshots_are_not_added_and_replay_is_idempotent(self):
        values = [frame(), frame(2, "0.25", used=7)]
        await self.store.report(*self.binding, 4, "waiting_model", usage=values)
        self.assertFalse(
            await self.store.report(*self.binding, 4, "waiting_model", usage=values)
        )
        await self.store.report(*self.binding, 5, "waiting_model", usage=values)
        current = (await self.store.get(self.run["id"]))["model_usage"]
        self.assertEqual(len(current["items"]), 1)
        self.assertEqual(current["items"][0]["values"]["cost"]["amount"], "0.25")
        self.assertEqual(current["items"][0]["values"]["used"], 7)
        self.assertEqual(current["settlement"], "unreconciled")
        async with self.db.read(self.scope) as c:
            count = (
                await c.execute(
                    select(func.count())
                    .select_from(observations)
                    .where(observations.c.scope == self.scope)
                )
            ).scalar_one()
        self.assertEqual(count, 2)

    async def test_conflict_session_and_unknown_fields_rejected_atomically(self):
        await self.store.report(*self.binding, 4, "waiting_model", usage=[frame()])
        for value, code in [
            (frame(amount="0.2"), "USAGE_CONFLICT"),
            (dict(frame(2), session_id="other"), "SESSION_CHANGED"),
            (dict(frame(2), secret="never-store"), "INVALID_USAGE"),
        ]:
            with self.assertRaisesRegex(RuntimeErrorCode, code):
                await self.store.report(
                    *self.binding, 5, "waiting_model", usage=[value]
                )
        self.assertEqual(
            (await self.store.get(self.run["id"]))["attempts"][0]["last_sequence"], 4
        )

    async def test_retained_output_recovers_and_links_existing_usage(self):
        await self.store.report(*self.binding, 4, "waiting_model", usage=[frame()])
        raw = (
            b"\n".join(
                [
                    encoded({"type": "session", "session_id": "session"}),
                    encoded(frame()),
                    encoded(frame(2, "0.3")),
                ]
            )
            + b"\n"
        )
        blob = self.provider.put([raw])
        await self.store.retain_output(self.binding, self.provider, blob.key, "stdout")
        await self.store.retain_output(self.binding, self.provider, blob.key, "stdout")
        current = (await self.store.get(self.run["id"]))["model_usage"]
        self.assertEqual(current["items"][0]["values"]["cost"]["amount"], "0.3")
        self.assertEqual(current["items"][0]["evidence"]["artifact_key"], blob.key)
        async with self.db.read(self.scope) as c:
            old = (
                (
                    await c.execute(
                        select(observations).where(
                            observations.c.scope == self.scope,
                            observations.c.sequence == 1,
                        )
                    )
                )
                .mappings()
                .one()
            )
            self.assertEqual(old["evidence"]["report_sequence"], 4)
            self.assertEqual(old["evidence"]["artifact_key"], blob.key)

    async def test_invalid_report_and_missing_data_are_not_zero(self):
        self.assertEqual(
            (await self.store.get(self.run["id"]))["model_usage"]["items"], []
        )
        invalid = dict(frame(), quality="invalid", values={})
        await self.store.report(*self.binding, 4, "waiting_model", usage=[invalid])
        current = (await self.store.get(self.run["id"]))["model_usage"]["items"][0]
        self.assertEqual(current["quality"], "invalid")
        self.assertEqual(current["values"], {})
        async with self.db.read("another-scope") as c:
            self.assertEqual(
                (await view(c, "another-scope", self.run["id"]))["items"], []
            )

    async def test_prompt_and_context_have_separate_semantics(self):
        prompt = dict(
            frame(2),
            kind="acp_prompt",
            values={
                "inputTokens": 20,
                "outputTokens": 5,
                "totalTokens": 25,
                "thoughtTokens": 3,
            },
        )
        await self.store.report(
            *self.binding, 4, "waiting_model", usage=[frame(unit="credits"), prompt]
        )
        rows = (await self.store.get(self.run["id"]))["model_usage"]["items"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            next(x for x in rows if x["kind"] == "acp_prompt")["aggregation"],
            "unspecified_acp_scope",
        )
        self.assertEqual(
            next(x for x in rows if x["kind"] == "acp_session")["values"]["cost"][
                "unit"
            ],
            "credits",
        )


class SQLiteUsageTests(UsageScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_backup_preserves_usage_evidence(self):
        from app.runtime.recovery import backup_sqlite, verify_backup

        await self.store.report(*self.binding, 4, "waiting_model", usage=[frame()])
        target = self.root / "backup"
        await asyncio.to_thread(backup_sqlite, self.url, self.provider.root, target)
        self.assertIn("runtime_usage_schema", verify_backup(target)["schemas"])


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL fixture disabled"
)
class PostgresUsageTests(UsageScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class WorkerUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_receipt_recovery_keeps_usage_without_relaunch(self):
        import test_runtime_reconciliation as recovery_fixtures

        fixture = recovery_fixtures.RecoveryTests()
        await fixture.asyncSetUp()
        try:
            data = worker_fixtures.protocol().replace(b"fixture-session", b"session")
            first, rest = data.split(b"\n", 1)
            adapter = worker_fixtures.FakeAdapter(
                first + b"\n" + encoded(frame()) + b"\n" + rest
            )
            run, adapter = await fixture.interrupted(adapter)
            result = await fixture.recover(run, adapter)
            self.assertEqual(result["state"], "succeeded")
            self.assertEqual(adapter.started, 1)
            self.assertEqual(len(result["model_usage"]["items"]), 1)
            self.assertIn("exit_receipt", result["model_usage"]["items"][0]["evidence"])
        finally:
            await fixture.asyncTearDown()

    async def test_remote_worker_preserves_live_and_artifact_observations_once(self):
        fixture = remote_fixtures.RemoteControlTests()
        await fixture.asyncSetUp()
        try:
            data = worker_fixtures.protocol().replace(b"fixture-session", b"session")
            first, rest = data.split(b"\n", 1)
            fixture.adapter.data = first + b"\n" + encoded(frame()) + b"\n" + rest
            result = await fixture.worker.once()
            self.assertEqual(result["state"], "succeeded")
            rows = result["model_usage"]["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["values"]["cost"]["amount"], "0.1")
            self.assertIn("report_sequence", rows[0]["evidence"])
            self.assertIn("artifact_key", rows[0]["evidence"])
        finally:
            await fixture.f.asyncTearDown()

    def test_bridge_accepts_usage_but_does_not_treat_it_as_output_or_result(self):
        data = (
            encoded({"type": "session", "session_id": "session"})
            + b"\n"
            + encoded(frame())
            + b"\n"
        )
        bridge = JsonlBridge()
        bridge.feed(data)
        bridge.feed(data)
        self.assertEqual(len(bridge.usage), 1)
        self.assertEqual(bridge.output_count, 0)
        with self.assertRaisesRegex(RuntimeErrorCode, "RESULT_INCOMPLETE"):
            bridge.feed(data, final=True)
