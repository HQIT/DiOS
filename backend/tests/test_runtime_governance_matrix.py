"""One governance contract across storage and optional native policy evaluation."""

import asyncio
from contextlib import asynccontextmanager
import json
from dataclasses import replace
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.artifacts import LocalArtifacts
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.tool_schema import invocations, evidence
import app.runtime.tool_invocations as tool_module
import test_runtime_tool_invocations as fixtures


class GovernanceContract:
    # The qualification runner supplies an isolated PG URL/native OPA config.
    database_url = None
    evaluator_configuration = None
    provider_pause = None
    provider_resume = None
    observations = []

    async def asyncSetUp(self):
        self.f = fixtures.ToolInvocationTests()
        self.f.database_url = self.database_url
        self.f.postgres = bool(self.database_url)
        self.f.evaluator_configuration = self.evaluator_configuration
        await self.f.asyncSetUp()
        self.addAsyncCleanup(self.f.asyncTearDown)

    def record(self, **details):
        f = self.f
        self.observations.append(
            {
                "case": self._testMethodName,
                "storage": "postgresql" if self.database_url else "sqlite",
                "policy": "opa" if self.evaluator_configuration else "builtin",
                "run_id": f.run["id"],
                "attempt_id": f.attempt["id"],
                "session_id": "session-one",
                **details,
            }
        )

    async def binding(self, call_id):
        async with self.f.db.read(self.f.scope) as c:
            return (
                await c.execute(
                    select(invocations.c.binding).where(invocations.c.id == call_id)
                )
            ).scalar_one()

    async def test_complete_tuple_policy_parity(self):
        from app.services.e2ag import evaluate_work_permission
        from app.runtime.policy_evaluator import OpaEvaluator
        import copy

        f = self.f
        permission = f.permission
        layers = {
            name: [permission] for name in ("caller", "service", "workload", "resource")
        }
        cases = [("allow", layers, permission, True)]
        for field in (
            "action",
            "resource_kind",
            "resource_id",
            "resource_version",
            "constraint_revision",
        ):
            cases.append(
                ("changed-" + field, layers, {**permission, field: "ungranted"}, False)
            )
        for layer in layers:
            missing = copy.deepcopy(layers)
            missing[layer] = []
            cases.append(("missing-" + layer, missing, permission, False))
        decisions = []
        for name, permissions, target, expected in cases:
            local = evaluate_work_permission(permissions, target).permits_execution
            self.assertEqual(local, expected, name)
            actual = local
            if self.evaluator_configuration:
                actual = (
                    await OpaEvaluator().evaluate(
                        self.evaluator_configuration,
                        {"context": {"permission": target, "permissions": permissions}},
                    )
                )["allow"]
            self.assertEqual(actual, expected, name)
            decisions.append({"case": name, "allow": actual})
        self.record(decisions=decisions)

    async def test_approval_and_duplicate_dispatch(self):
        await fixtures.ToolInvocationTests.test_approval_dispatch_and_idempotent_replay(
            self.f
        )
        self.record(tool_calls=len(self.f.transport.calls), approval="consumed-once")

    async def test_approval_before_side_effect_and_parameter_expiry_guards(self):
        await fixtures.ToolInvocationTests.test_pending_never_reaches_transport_and_self_approval_denied(
            self.f
        )
        await fixtures.ToolInvocationTests.test_changed_session_parameters_or_expiry_cannot_dispatch(
            self.f
        )
        self.record(tool_calls=0)

    async def test_concurrent_dispatch_consumes_once(self):
        await fixtures.ToolInvocationTests.test_concurrent_dispatch_calls_upstream_once(
            self.f
        )
        self.record(tool_calls=len(self.f.transport.calls))

    async def test_committed_intent_precedes_external_call(self):
        f = self.f
        item = await f.approve(await f.request())
        original = f.transport.call
        other = Database(f.url)
        self.addAsyncCleanup(other.close)

        async def call(name, arguments, key):
            # Independent connection observes the committed decision, not pending ORM state.
            async with other.read(f.scope) as c:
                row = (
                    (
                        await c.execute(
                            select(invocations).where(invocations.c.id == key)
                        )
                    )
                    .mappings()
                    .one()
                )
                decisions = (
                    (
                        await c.execute(
                            select(evidence.c.decision).where(
                                evidence.c.invocation_id == key,
                                evidence.c.stage == "dispatch",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            self.assertEqual(
                (row["state"], row["approval_state"]), ("dispatched", "consumed")
            )
            self.assertEqual(sum(d["decision"] == "allow" for d in decisions), 1)
            intent = json.loads(f.gate.receipts.path(key, "intent").read_text())
            self.assertEqual(intent["dispatch_version"], row["version"])
            self.assertEqual(intent["context"]["binding"], row["binding"])
            self.assertEqual(
                intent["context"]["arguments_digest"], row["arguments_digest"]
            )
            return await original(name, arguments, key)

        with patch.object(f.transport, "call", side_effect=call):
            result = await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(result["state"], "succeeded")
        self.record(committed_before_call=True, tool_calls=1)

    async def test_dispatch_audit_failure_rolls_back_then_resumes_original_call(self):
        f = self.f
        item = await f.approve(await f.request())
        original = tool_module.audit

        async def failed(*args, **kwargs):
            if args[3] == "dispatch":
                raise OSError("qualification audit failure")
            return await original(*args, **kwargs)

        with patch.object(tool_module, "audit", side_effect=failed):
            with self.assertRaises(OSError):
                await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        saved = await f.gate.get(f.run["id"], item["id"])
        self.assertEqual(
            (saved["state"], saved["approval_state"], saved["version"]),
            ("prepared", "approved", item["version"]),
        )
        self.assertEqual(f.transport.calls, [])
        result = await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["id"], item["id"])
        self.record(invocation_id=item["id"], tool_calls=1, approval_preserved=True)

    async def test_receipt_disk_failure_never_calls_or_replays_tool(self):
        f = self.f
        item = await f.approve(await f.request())
        with patch.object(
            f.gate.receipts,
            "prepare",
            side_effect=OSError("qualification disk failure"),
        ):
            result = await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(result["state"], "result_unknown")
        self.assertEqual(f.transport.calls, [])
        await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(f.transport.calls, [])
        self.record(tool_calls=0, state="result_unknown")

    async def test_complete_receipt_recovers_after_control_restart(self):
        f = self.f
        item = await f.approve(await f.request())
        with patch.object(
            f.gate,
            "recover_receipt",
            side_effect=ConnectionError("qualification control failure"),
        ):
            with self.assertRaises(ConnectionError):
                await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertTrue(f.gate.receipts.available(item["id"]))
        self.assertEqual(len(f.transport.calls), 1)
        await f.db.close()
        f.db = Database(f.url)
        f.store = RuntimeStore(f.db, f.scope)
        f.provider = LocalArtifacts(f.root / "artifacts")
        f.gate = ToolInvocations(
            f.store, f.policy, f.gate.catalog, f.provider, f.transport, f.gate.approvers
        )
        result = await f.gate.recover_receipt(f.run["id"], item["id"])
        self.assertEqual(result["state"], "succeeded")
        again = await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(again["version"], result["version"])
        self.assertEqual(len(f.transport.calls), 1)
        self.assertEqual(
            (await self.binding(item["id"]))["attempt_id"], f.attempt["id"]
        )
        self.record(tool_calls=1, invocation_id=item["id"], recovered_same_binding=True)

    async def test_unknown_outcome_is_not_replayed(self):
        await fixtures.ToolInvocationTests.test_timeout_is_unknown_no_replay_and_blocks_task_success(
            self.f
        )
        self.record(tool_calls=1, state="result_unknown")

    async def test_revoke_transaction_wins_before_dispatch(self):
        f = self.f
        item = await f.approve(await f.request())
        other = Database(f.url)
        self.addAsyncCleanup(other.close)
        store = RuntimeStore(other, f.scope)
        held = asyncio.Event()
        release = asyncio.Event()
        attempted = asyncio.Event()
        original_event = store._event
        original_transaction = f.db.transaction

        async def hold(*args, **kwargs):
            result = await original_event(*args, **kwargs)
            if args[2] == "authorization.revoked":
                held.set()
                await release.wait()
            return result

        @asynccontextmanager
        async def attempting(*args, **kwargs):
            attempted.set()
            async with original_transaction(*args, **kwargs) as c:
                yield c

        run = await store.get(f.run["id"])
        tasks = []
        try:
            with patch.object(store, "_event", side_effect=hold):
                revoke = asyncio.create_task(store.revoke(run["id"], run["version"]))
                tasks.append(revoke)
                await asyncio.wait_for(held.wait(), 5)
                with patch.object(f.db, "transaction", side_effect=attempting):
                    dispatch = asyncio.create_task(
                        f.gate.dispatch(run["id"], item["id"], item["version"])
                    )
                    tasks.append(dispatch)
                    await asyncio.wait_for(attempted.wait(), 5)
                    release.set()
                    await revoke
                    with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
                        await dispatch
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(f.transport.calls, [])
        self.record(order="revoke-commit-before-dispatch", tool_calls=0)

    async def test_dispatch_commit_wins_and_later_revocation_blocks_new_work(self):
        f = self.f
        item = await f.approve(await f.request())
        committed = asyncio.Event()
        release = asyncio.Event()
        original = f.gate._execute_dispatched

        async def held(*args):
            committed.set()
            await release.wait()
            return await original(*args)

        other = Database(f.url)
        self.addAsyncCleanup(other.close)
        store = RuntimeStore(other, f.scope)
        dispatch = None
        try:
            with patch.object(f.gate, "_execute_dispatched", side_effect=held):
                dispatch = asyncio.create_task(
                    f.gate.dispatch(f.run["id"], item["id"], item["version"])
                )
                await asyncio.wait_for(committed.wait(), 5)
                async with other.read(f.scope) as c:
                    row = (
                        (
                            await c.execute(
                                select(invocations).where(
                                    invocations.c.id == item["id"]
                                )
                            )
                        )
                        .mappings()
                        .one()
                    )
                self.assertEqual(row["state"], "dispatched")
                run = await store.get(f.run["id"])
                await store.revoke(run["id"], run["version"])
                release.set()
                result = await dispatch
        finally:
            release.set()
            if dispatch:
                await asyncio.gather(dispatch, return_exceptions=True)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(len(f.transport.calls), 1)
        run = await f.store.get(f.run["id"])
        self.assertTrue(run["revoked"])
        self.assertNotEqual(run["state"], "succeeded")
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await f.verifier(
                replace(f.context, session_id="session-one"), "accept_result"
            )
        with self.assertRaisesRegex(RuntimeErrorCode, "AUTH_REVOKED"):
            await f.request("after-revoke")
        self.record(
            order="dispatch-commit-before-revoke",
            tool_calls=1,
            original_tool_fact_retained=True,
            new_work_denied=True,
        )

    async def test_policy_outage_then_recovery_preserves_approval_and_identity(self):
        f = self.f
        item = await f.approve(await f.request())
        binding = await self.binding(item["id"])
        if self.evaluator_configuration:
            await self.provider_pause()
            try:
                with self.assertRaisesRegex(
                    RuntimeErrorCode,
                    "POLICY_EVALUATOR_TIMEOUT|POLICY_EVALUATOR_UNAVAILABLE",
                ):
                    await f.gate.dispatch(f.run["id"], item["id"], item["version"])
            finally:
                await self.provider_resume()
        else:
            path = f.policy.path
            held = path.with_suffix(".held")
            path.rename(held)
            try:
                with self.assertRaises(OSError):
                    await f.gate.dispatch(f.run["id"], item["id"], item["version"])
            finally:
                held.rename(path)
        current = await f.gate.get(f.run["id"], item["id"])
        self.assertEqual(
            (current["state"], current["approval_state"]), ("prepared", "approved")
        )
        self.assertEqual(f.transport.calls, [])
        result = await f.gate.dispatch(f.run["id"], item["id"], item["version"])
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(await self.binding(item["id"]), binding)
        self.record(tool_calls=1, same_approval=True, same_binding=True)


class SqliteGovernanceTests(GovernanceContract, unittest.IsolatedAsyncioTestCase):
    pass
