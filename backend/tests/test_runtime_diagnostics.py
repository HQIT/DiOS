"""Diagnostics keep receipt time, monotonic checks and execution authority distinct."""

import asyncio
import os
import unittest
from unittest.mock import patch

import test_runtime_tool_invocations as tool_fixtures
import test_runtime_usage as usage_fixtures
import test_runtime_work_authorizations as auth_fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.diagnostics import measured
from app.runtime.schema import events
from sqlalchemy import select


class PhaseScenarios:
    postgres = False
    asyncSetUp = usage_fixtures.UsageScenarios.asyncSetUp
    asyncTearDown = usage_fixtures.UsageScenarios.asyncTearDown

    async def detail(self):
        return (await self.store.get(self.run["id"], diagnostics=True))["diagnostics"]

    async def test_repeated_reports_do_not_restart_phase_and_replay_is_stable(self):
        first = await self.detail()
        await self.store.report(*self.binding, 4, "waiting_model")
        self.assertFalse(await self.store.report(*self.binding, 4, "waiting_model"))
        current = await self.detail()
        self.assertEqual(len(current["stages"]), 4)
        self.assertEqual(
            current["stages"][-1]["started_at"], first["stages"][-1]["started_at"]
        )
        self.assertIsNone(current["stages"][-1]["duration_seconds"])
        self.assertGreaterEqual(current["stages"][-1]["elapsed_seconds"], 0)
        self.assertEqual(current["freshness"], "stale")
        self.assertEqual(current["time_basis"], "control_plane_received")
        self.assertNotIn("diagnostics", await self.store.get(self.run["id"]))

    async def test_backwards_clock_does_not_fabricate_zero_duration(self):
        async with self.db.transaction(self.scope) as c:
            row = (
                (
                    await c.execute(
                        select(events)
                        .where(
                            events.c.run_id == self.run["id"],
                            events.c.type == "attempt.phase",
                        )
                        .order_by(events.c.version)
                    )
                )
                .mappings()
                .first()
            )
            await c.execute(
                events.update()
                .where(events.c.id == row["id"])
                .values(created_at=10**12)
            )
        value = await self.detail()
        preparing = next(x for x in value["stages"] if x["phase"] == "preparing")
        self.assertTrue(preparing["clock_anomaly"])
        self.assertIsNone(preparing["duration_seconds"])

    async def test_output_and_observation_are_independent_and_terminal_stops_elapsed(
        self,
    ):
        now = await self.db_now()
        await self.store.report(
            *self.binding,
            4,
            "streaming",
            output=True,
            output_observed_at=now - 120,
            remote={"runtime": {"observed_at": now, "alive": True}},
        )
        current = await self.detail()
        self.assertEqual(current["freshness"], "fresh")
        self.assertLess(current["last_output_at"], current["last_event_at"] - 100)
        await self.store.report(
            *self.binding,
            5,
            "streaming",
            remote={"runtime": {"observed_at": now, "alive": False, "state": "exited"}},
        )
        await self.store.finish_failed(self.binding, "PROCESS_EXITED")
        final = await self.detail()
        self.assertTrue(all(x["elapsed_seconds"] is None for x in final["stages"]))
        self.assertTrue(all(x["duration_seconds"] is not None for x in final["stages"]))

    async def db_now(self):
        async with self.db.read(self.scope) as c:
            return await self.db.now(c)


class SQLiteDiagnosticsTests(PhaseScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "PostgreSQL fixture disabled"
)
class PostgresDiagnosticsTests(PhaseScenarios, unittest.IsolatedAsyncioTestCase):
    postgres = True


class GovernanceDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = auth_fixtures.WorkAuthorizationTests.asyncSetUp
    asyncTearDown = auth_fixtures.WorkAuthorizationTests.asyncTearDown
    submit = auth_fixtures.WorkAuthorizationTests.submit
    worker = auth_fixtures.WorkAuthorizationTests.worker
    claimed = auth_fixtures.WorkAuthorizationTests.claimed

    async def test_allow_and_denial_have_timings_legacy_admission_remains_unknown(self):
        run, _attempt, context = await self.claimed()
        current = await self.store.get(run["id"])
        await self.store.revoke(run["id"], current["version"])
        with self.assertRaises(RuntimeErrorCode):
            await self.verifier(context, "execute")
        diag = (await self.store.get(run["id"], diagnostics=True))["diagnostics"]
        admission = next(x for x in diag["governance"] if x["action"] == "admit")
        self.assertIsNone(admission["timing"])
        checkpoints = [x for x in diag["governance"] if x["action"] == "execute"]
        self.assertEqual({x["outcome"] for x in checkpoints}, {"allow", "deny"})
        for item in checkpoints:
            t = item["timing"]
            self.assertAlmostEqual(
                t["before_audit_seconds"],
                sum(
                    t[k]
                    for k in [
                        "lookup_seconds",
                        "policy_refresh_seconds",
                        "transaction_wait_seconds",
                        "authorization_seconds",
                    ]
                ),
            )
            self.assertTrue(all(v >= 0 for k, v in t.items() if k != "clock"))
        self.assertIsNone(diag["coverage"]["persistence_commit_seconds"])

    def test_unknown_sensitive_and_invalid_fields_are_not_exported(self):
        self.assertIsNone(measured({"latency_us": 0}))
        self.assertEqual(
            measured(
                {
                    "clock": "monotonic",
                    "authorization_seconds": 0.1,
                    "before_audit_seconds": float("nan"),
                    "opa_request_seconds": True,
                    "secret": "hidden",
                }
            ),
            {"clock": "monotonic", "authorization_seconds": 0.1},
        )


class PolicyTimingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = tool_fixtures.ToolInvocationTests.asyncSetUp
    asyncTearDown = tool_fixtures.ToolInvocationTests.asyncTearDown
    request = tool_fixtures.ToolInvocationTests.request

    async def test_denied_request_without_invocation_retains_policy_timing(self):
        current, registration = await self.gate._configuration(self.run["id"], "search")
        registration["policy_evaluator"] = {"kind": "opa"}
        with (
            patch.object(
                self.gate, "_configuration", return_value=(current, registration)
            ),
            patch.object(
                self.gate.policy_evaluator,
                "evaluate",
                return_value={"allow": False, "provider": "opa"},
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "POLICY_DENIED"),
        ):
            await self.request()
        diag = (await self.store.get(self.run["id"], diagnostics=True))["diagnostics"]
        row = next(x for x in diag["governance"] if x["action"] == "policy")
        self.assertEqual(row["attempt_id"], self.attempt["id"])
        self.assertIsNotNone(row["timing"])
        self.assertEqual(row["outcome"], "deny")
        self.assertFalse(self.transport.calls)

    async def test_policy_timeout_is_measured_without_dispatch_or_approval(self):
        item = await self.request()

        async def unavailable(*args):
            await asyncio.sleep(0.02)
            raise RuntimeErrorCode("POLICY_EVALUATOR_TIMEOUT")

        with patch.object(
            self.gate.policy_evaluator, "evaluate", side_effect=unavailable
        ):
            async with self.db.transaction(self.scope) as c:
                stored = await self.gate._row(c, self.run["id"], item["id"])
                with self.assertRaisesRegex(
                    RuntimeErrorCode, "POLICY_EVALUATOR_TIMEOUT"
                ):
                    await self.gate._evaluate_policy(
                        c,
                        item["id"],
                        stored["binding"],
                        dict(self.registration, policy_evaluator={"kind": "opa"}),
                        {"query": "private-query"},
                    )
        diag = (await self.store.get(self.run["id"], diagnostics=True))["diagnostics"]
        row = next(x for x in diag["governance"] if x["action"] == "policy")
        self.assertEqual(row["outcome"], "deny")
        self.assertGreaterEqual(row["timing"]["opa_request_seconds"], 0.015)
        self.assertNotIn("private-query", str(diag))
        self.assertFalse(self.transport.calls)
        self.assertEqual(
            (await self.gate.get(self.run["id"], item["id"]))["state"],
            "awaiting_approval",
        )
