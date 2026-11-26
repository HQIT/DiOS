"""Report denominators, accounting identity, unknowns and private capture."""

import copy
import json
import os
import unittest

from app.runtime.evidence_report import (
    capture,
    coverage,
    distribution,
    markdown,
    summarize,
)
import test_runtime_work_authorizations as fixtures


class ReportScenarios:
    asyncSetUp = fixtures.WorkAuthorizationTests.asyncSetUp
    asyncTearDown = fixtures.WorkAuthorizationTests.asyncTearDown
    submit = fixtures.WorkAuthorizationTests.submit
    worker = fixtures.WorkAuthorizationTests.worker

    async def test_real_store_capture_recomputes_and_never_fills_missing_usage(self):
        await self.submit()
        result = await self.worker().once()
        self.assertEqual(result["state"], "succeeded")
        data = await capture(self.db, self.scope)
        report = summarize(data)
        self.assertEqual(report, summarize(json.loads(json.dumps(data))))
        self.assertEqual(report["throughput"]["counts"], {"succeeded": 1})
        self.assertEqual(report["coverage"]["terminal_outcome"], coverage(1, 1))
        self.assertEqual(report["coverage"]["session_usage_report"], coverage(0, 1))
        self.assertIsNone(report["usage"]["settled_cost"])
        self.assertIsNone(report["coverage"]["agent_internal_tool_events"])
        self.assertGreater(
            report["coverage"]["audited_checkpoint_timing"]["present"], 0
        )
        self.assertNotIn("合成结果", json.dumps(data, ensure_ascii=False))
        self.assertNotIn("permissions", json.dumps(data))
        self.assertIn("证据覆盖", markdown(report))
        self.assertEqual(
            (await self.store.get(result["id"]))["version"], result["version"]
        )

    async def test_truncation_unknown_and_usage_snapshots_are_not_additive(self):
        await self.submit()
        await self.worker().once()
        await self.submit("two")
        data = await capture(self.db, self.scope, limit=1)
        self.assertFalse(summarize(data)["bounded_records_complete"])
        r = data["runs"][0]
        aid = r["attempts"][0]["id"]
        r["usage"]["items"] = [
            {
                "attempt_id": aid,
                "kind": "acp_prompt",
                "sequence": n,
                "quality": "reported",
                "values": {"totalTokens": n * 7},
                "aggregation": "unspecified_acp_scope",
            }
            for n in (1, 2)
        ]
        report = summarize(data)
        self.assertEqual(len(report["usage"]["records"]), 1)
        self.assertEqual(report["usage"]["records"][0]["values"]["totalTokens"], 14)
        self.assertEqual(report["coverage"]["session_usage_report"], coverage(1, 1))
        data["active_observation"]["actual_concurrency"] = None
        self.assertIsNone(summarize(data)["concurrency"]["actual"])
        r["consumption"] = {
            "at_limit": False,
            "items": [
                {
                    "account_id": "a",
                    "attempt_id": aid,
                    "unit": "USD-micro",
                    "reserved": 10,
                    "actual": 4,
                    "state": "settled",
                    "evidence_recorded": True,
                },
                {
                    "account_id": "b",
                    "attempt_id": aid,
                    "unit": "USD-micro",
                    "reserved": 10,
                    "actual": None,
                    "state": "reserved_unreconciled",
                    "evidence_recorded": False,
                },
            ],
        }
        self.assertEqual(summarize(data)["settlements"]["coverage"], coverage(1, 2))
        self.assertEqual(len(summarize(data)["settlements"]["records"]), 2)
        stage = r["diagnostics"]["stages"][0]
        stage["duration_seconds"] = None
        stage["clock_anomaly"] = True
        self.assertGreater(
            summarize(data)["coverage"]["closed_phase_duration"]["missing"], 0
        )
        data["runs"].append(copy.deepcopy(r))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            summarize(data)

    async def test_empty_and_filtered_reports_have_explicit_denominators(self):
        await self.submit()
        data = await capture(self.db, self.scope, profile_id="not-selected")
        self.assertEqual(data["matching_runs"], 0)
        report = summarize(data)
        self.assertIsNone(report["coverage"]["terminal_outcome"]["missing_fraction"])
        self.assertEqual(report["throughput"]["counts"], {})
        self.assertEqual(report["concurrency"]["actual"], 0)


class SQLiteReportTests(ReportScenarios, unittest.IsolatedAsyncioTestCase):
    pass


@unittest.skipUnless(
    os.environ.get("DIOS_TEST_POSTGRES_URL"), "isolated PostgreSQL required"
)
class PostgresReportTests(ReportScenarios, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database_url = os.environ["DIOS_TEST_POSTGRES_URL"]
        await ReportScenarios.asyncSetUp(self)


class StatisticsTests(unittest.TestCase):
    def test_empty_is_unknown_and_invalid_measurements_are_not_zero(self):
        self.assertIsNone(coverage(0, 0)["missing_fraction"])
        self.assertIsNone(distribution([])["sum_seconds"])
        self.assertEqual(distribution([None, True, float("nan"), -1, 0, 2])["count"], 2)
        self.assertEqual(distribution(list(range(1, 21)))["p95_seconds"], 19)
