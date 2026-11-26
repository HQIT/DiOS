"""Exact-version evidence gates all protected deployment entry points."""

import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import test_runtime_task_console as fixtures
import test_studio_release as release_fixtures
from app.api.os import technical_releases as api
from app.runtime import technical_records as ledger
from app.runtime.database import RuntimeErrorCode
from app.runtime.deployments import Deployments
from app.runtime.environment_promotions import Promotions
from app.runtime.execution_revisions import compile_profile, load_profile
from app.runtime.revisions import Revisions
from app.runtime.schema import artifacts
from app.runtime.technical_evaluations import Evaluations
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from sqlalchemy import select


class TechnicalReleaseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.TaskConsoleTests.asyncSetUp
    asyncTearDown = fixtures.TaskConsoleTests.asyncTearDown

    async def setup_technical(self):
        await release_fixtures.ReleaseTests.setup_release(self)
        self.app.include_router(api.router, prefix="/api/os")
        self.approver_value["approvers"][0]["tools"] += [
            "environment.test.promote",
            "environment.prod.promote",
        ]
        self.approver_path.write_text(json.dumps(self.approver_value))
        self.eval = Evaluations(
            self.db, "one", self.console.policy, self.console.provider
        )
        self.prom = Promotions(self.db, "one", self.console.policy, self.provider)
        self.deploy = Deployments(self.db, "one", self.console.policy, self.provider)
        self.revision = (
            await Revisions(self.db, "one").create(
                "first", compile_profile(self.raw), actor="operator"
            )
        )["revision"]["id"]
        self.suite = {
            "name": "Protocol and result contract",
            "cases": [
                {
                    "id": "first",
                    "input": self.raw["console"]["input_catalog_data"]["inputs"][0],
                    "result_schema": {
                        "type": "object",
                        "required": ["summary"],
                        "properties": {"summary": {"const": "healthy"}},
                    },
                    "max_seconds": 60,
                }
            ],
        }
        self.suite_ref = (
            await self.eval.put_suite("contract", self.suite, 0, "operator")
        )["ref"]
        self.rules = {
            "stage": "test",
            "suite_ref": self.suite_ref,
            "max_age_seconds": 3600,
            "source_environment": None,
            "external_approval_required": False,
        }
        await self.prom.environment("test", self.rules, 0, "operator")
        await self.prom.environment(
            "prod",
            dict(
                self.rules,
                stage="production",
                source_environment="test",
                external_approval_required=True,
            ),
            0,
            "operator",
        )
        await self.prom.bind("test-agent", "test", "review", "operator")
        await self.prom.bind("prod-agent", "prod", "review", "operator")

    async def execute(self, ref=None, healthy=True):
        ref = ref or self.revision
        _, profile, spec, _, _ = await load_profile(self.db, "one", ref)
        data = (
            "\n".join(
                json.dumps(x)
                for x in [
                    {"type": "session", "session_id": "evaluation-session"},
                    {
                        "type": "result",
                        "session_id": "evaluation-session",
                        "result": {
                            "summary": "healthy" if healthy else "unexpected",
                            "evidence": ["fixture:source"],
                        },
                    },
                ]
            )
            + "\n"
        ).encode()
        return await Worker(
            self.console.store,
            fixtures.FakeAdapter(data),
            profile,
            spec,
            self.console.provider,
            revision_ref=ref,
            authorization_verifier=WorkAuthorizationVerifier(
                self.console.store, self.console.policy
            ),
        ).once()

    async def evaluate(self, key="evaluation", healthy=True, ref=None):
        value = await self.eval.start(
            ref or self.revision, self.suite_ref, key, "operator"
        )
        self.assertEqual((await self.execute(ref, healthy))["state"], "succeeded")
        return await self.eval.reconcile(value["id"])

    async def promotion(
        self,
        evaluation,
        target="test-agent",
        key="test",
        version=0,
        source=None,
        external=None,
        rollback=False,
    ):
        value = await self.prom.request(
            target,
            evaluation["id"],
            version,
            source,
            external,
            key,
            "operator",
            rollback=rollback,
        )
        await self.prom.decide(value["id"], 1, "approved", "reviewer-test", "verified")
        return await self.prom.publish(value["id"], 2, "operator")

    async def test_exact_evaluation_and_approval_gate_development_to_production(self):
        await self.setup_technical()
        pending = await self.eval.start(
            self.revision, self.suite_ref, "first", "operator"
        )
        self.assertEqual(
            (await self.eval.start(self.revision, self.suite_ref, "first", "operator"))[
                "id"
            ],
            pending["id"],
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "EVALUATION_GATE_FAILED"):
            await self.prom.request(
                "test-agent", pending["id"], 0, None, None, "missing", "operator"
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "ENVIRONMENT_PROMOTION_REQUIRED"):
            await self.deploy.request(
                "test-agent", self.revision, "bypass", 0, "operator", {"payload": {}}
            )
        await self.execute()
        passed = await self.eval.reconcile(pending["id"])
        self.assertEqual(passed["document"]["state"], "passed", passed)
        requested = await self.prom.request(
            "test-agent", passed["id"], 0, None, None, "release", "operator"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "ENVIRONMENT_PROMOTION_REQUIRED"):
            await self.prom.publish(requested["id"], 1, "operator")
        await self.prom.decide(requested["id"], 1, "approved", "reviewer-test", "test")
        published = await asyncio.gather(
            *(self.prom.publish(requested["id"], 2, "operator") for _ in range(3))
        )
        self.assertEqual(
            len({x["document"]["deployment"]["probe_run_id"] for x in published}), 1
        )
        await self.execute()
        ready = await self.deploy.reconcile("test-agent")
        self.assertEqual(ready["status"]["active_revision_id"], self.revision)
        with self.assertRaisesRegex(
            RuntimeErrorCode, "EXTERNAL_APPROVAL_REFERENCE_REQUIRED"
        ):
            await self.prom.request(
                "prod-agent",
                passed["id"],
                0,
                "test-agent",
                None,
                "prod-missing",
                "operator",
            )
        external = {
            "uri": "https://review.example.test/decisions/1",
            "version": "1",
            "digest": "sha256:" + "a" * 64,
        }
        production = await self.promotion(
            passed, "prod-agent", "prod", source="test-agent", external=external
        )
        await self.execute()
        ready = await self.deploy.reconcile("prod-agent")
        self.assertEqual(ready["status"]["active_revision_id"], self.revision)
        self.assertEqual(production["document"]["external_approval"], external)

    async def test_failed_or_missing_result_evidence_never_qualifies(self):
        await self.setup_technical()
        failed = await self.evaluate(healthy=False)
        self.assertEqual(failed["document"]["state"], "failed")
        with self.assertRaisesRegex(RuntimeErrorCode, "EVALUATION_GATE_FAILED"):
            await self.promotion(failed)
        pending = await self.eval.start(
            self.revision, self.suite_ref, "missing", "operator"
        )
        run = await self.execute()
        async with self.db.read("one") as c:
            blob = (
                (
                    await c.execute(
                        select(artifacts)
                        .where(artifacts.c.run_id == run["id"])
                        .order_by(artifacts.c.created_at.desc())
                    )
                )
                .mappings()
                .first()
            )
        path = self.console.provider.path(blob["storage_key"])
        saved = path.read_bytes()
        path.write_bytes(b"corrupted")
        inconclusive = await self.eval.reconcile(pending["id"])
        self.assertEqual(inconclusive["document"]["state"], "inconclusive")
        path.write_bytes(saved)
        recovered = await self.eval.reconcile(pending["id"])
        self.assertEqual(recovered["document"]["state"], "passed")
        comparison = await self.eval.compare(failed["id"], recovered["id"])
        self.assertEqual(comparison["changes"][0]["before"], "failed")
        self.assertEqual(comparison["changes"][0]["after"], "passed")

    async def test_environment_policy_and_approver_rechecked_at_activation(self):
        await self.setup_technical()
        passed = await self.evaluate()
        await self.promotion(passed)
        await self.execute()
        await self.prom.environment(
            "test", dict(self.rules, max_age_seconds=120), 1, "operator"
        )
        failed = await self.deploy.reconcile("test-agent")
        self.assertEqual(failed["status"]["error"], "ENVIRONMENT_POLICY_CHANGED")
        self.assertIsNone(failed["status"]["active_revision_id"])
        current = await self.prom.request(
            "test-agent",
            passed["id"],
            failed["deployment"]["version"],
            None,
            None,
            "second",
            "operator",
        )
        await self.prom.decide(
            current["id"], 1, "approved", "reviewer-test", "verified"
        )
        await self.prom.publish(current["id"], 2, "operator")
        await self.execute()
        self.approver_value["approvers"][0]["enabled"] = False
        self.approver_path.write_text(json.dumps(self.approver_value))
        failed = await self.deploy.reconcile("test-agent")
        self.assertEqual(failed["status"]["error"], "APPROVER_FORBIDDEN")
        self.assertIsNone(failed["status"]["active_revision_id"])

    async def test_versioned_suites_cannot_substitute_results_and_api_is_operator_only(
        self,
    ):
        await self.setup_technical()
        passed = await self.evaluate()
        different = copy.deepcopy(self.suite)
        different["cases"][0]["max_seconds"] = 120
        new_ref = (await self.eval.put_suite("contract", different, 1, "operator"))[
            "ref"
        ]
        self.assertNotEqual(new_ref["digest"], self.suite_ref["digest"])
        await self.prom.environment(
            "test", dict(self.rules, suite_ref=new_ref), 1, "operator"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "EVALUATION_GATE_FAILED"):
            await self.promotion(passed)
        self.assertEqual(
            (
                await self.client.get("/api/os/runtime/technical/records/suite")
            ).status_code,
            401,
        )
        rows = (
            await self.client.get(
                "/api/os/runtime/technical/records/suite", headers=self.headers
            )
        ).json()
        self.assertEqual(rows["items"][0]["version"], 2)
        async with self.db.read("other") as c:
            self.assertEqual(await ledger.page(c, "other", "evaluation"), [])
        invalid = dict(self.suite, cases=[])
        response = await self.client.put(
            "/api/os/runtime/technical/suites/invalid",
            json={"document": invalid, "expected_version": 0},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)

    async def test_rollback_and_failed_upgrade_preserve_known_revision(self):
        await self.setup_technical()
        first = await self.evaluate()
        await self.promotion(first)
        await self.execute()
        ready = await self.deploy.reconcile("test-agent")
        changed = copy.deepcopy(self.raw)
        changed["console"]["title"] = "Updated capability"
        second_ref = (
            await Revisions(self.db, "one").create(
                "second", compile_profile(changed), actor="operator"
            )
        )["revision"]["id"]
        self.assertNotEqual(second_ref, self.revision)
        second = await self.evaluate("second", ref=second_ref)
        await self.promotion(
            second, key="upgrade", version=ready["deployment"]["version"]
        )
        await self.execute(second_ref)
        ready = await self.deploy.reconcile("test-agent")
        self.assertEqual(ready["status"]["previous_revision_id"], self.revision)
        await self.promotion(
            first, key="rollback", version=ready["deployment"]["version"], rollback=True
        )
        await self.execute()
        restored = await self.deploy.reconcile("test-agent")
        self.assertEqual(restored["status"]["active_revision_id"], self.revision)
        await self.promotion(
            second, key="bad-upgrade", version=restored["deployment"]["version"]
        )
        self.approver_value["approvers"][0]["enabled"] = False
        self.approver_path.write_text(json.dumps(self.approver_value))
        await self.execute(second_ref)
        rejected = await self.deploy.reconcile("test-agent")
        self.assertEqual(rejected["status"]["active_revision_id"], self.revision)
        self.assertEqual(rejected["status"]["error"], "APPROVER_FORBIDDEN")

    async def test_observed_revocation_is_durable_and_expired_evidence_rejected(self):
        await self.setup_technical()
        pending = await self.eval.start(
            self.revision, self.suite_ref, "pending", "operator"
        )
        with self.assertRaisesRegex(
            RuntimeErrorCode, "EVALUATION_COMPARISON_NOT_READY"
        ):
            await self.eval.compare(pending["id"], pending["id"])
        await self.execute()
        passed = await self.eval.reconcile(pending["id"])
        requested = await self.prom.request(
            "test-agent", passed["id"], 0, None, None, "revoke", "operator"
        )
        await self.prom.decide(
            requested["id"], 1, "approved", "reviewer-test", "verified"
        )
        self.approver_value["approvers"][0]["enabled"] = False
        self.approver_path.write_text(json.dumps(self.approver_value))
        with self.assertRaisesRegex(RuntimeErrorCode, "APPROVER_FORBIDDEN"):
            await self.prom.publish(requested["id"], 2, "operator")
        self.approver_value["approvers"][0]["enabled"] = True
        self.approver_path.write_text(json.dumps(self.approver_value))
        async with self.db.read("one") as c:
            record = await ledger.get(c, "one", "promotion", requested["id"])
        self.assertEqual(record["document"]["state"], "invalidated")
        with self.assertRaisesRegex(RuntimeErrorCode, "ENVIRONMENT_PROMOTION_REQUIRED"):
            await self.prom.publish(requested["id"], 3, "operator")
        with (
            patch.object(
                self.db, "now", return_value=passed["document"]["completed_at"] + 3601
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "EVALUATION_EXPIRED"),
        ):
            await self.prom.request(
                "test-agent", passed["id"], 0, None, None, "expired", "operator"
            )
