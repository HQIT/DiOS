"""Terminal task closure cannot erase a dispatched tool's unknown outcome."""

import unittest
from unittest.mock import patch

import test_runtime_tool_invocations as fixtures
from app.runtime.adapter import Observation
from app.runtime.database import RuntimeErrorCode
from app.runtime.schema import runs
from app.runtime.tool_invocations import require_settled
from app.runtime.tool_schema import evidence, invocations
from sqlalchemy import select


class ToolLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ToolInvocationTests.asyncSetUp
    asyncTearDown = fixtures.ToolInvocationTests.asyncTearDown
    request = fixtures.ToolInvocationTests.request
    approve = fixtures.ToolInvocationTests.approve

    async def exited(self):
        await self.store.observe_process(
            self.binding,
            Observation(
                self.attempt["id"],
                "exited",
                False,
                None,
                "fixture-process",
                1,
                "unknown",
                "fixture-host",
            ),
            adapter="fixture",
            boot_id="fixture-boot",
        )

    async def test_terminal_task_closes_unstarted_calls_and_preserves_approval(self):
        pending = await self.request("pending")
        approved = await self.approve(await self.request("approved"))
        await self.exited()
        await self.store.finish_failed(self.binding, "PROCESS_EXITED")
        for item in (pending, approved):
            saved = await self.gate.get(self.run["id"], item["id"])
            self.assertEqual(saved["state"], "failed")
            self.assertEqual(saved["reason"], "TOOL_NOT_DISPATCHED_RUN_TERMINAL")
            self.assertEqual(saved["approval_state"], item["approval_state"])
            self.assertEqual(saved["approver"], item["approver"])
            self.assertEqual(saved["version"], item["version"] + 1)
            self.assertEqual(
                len([e for e in saved["evidence"] if e["stage"] == "lifecycle"]), 1
            )
            replay = await self.gate.dispatch(
                self.run["id"], item["id"], item["version"]
            )
            self.assertEqual(replay["state"], "failed")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.opened, 0)

    async def test_dispatched_and_unknown_calls_remain_reconcilable(self):
        records = []
        for state in ("dispatched", "result_unknown"):
            item = await self.approve(await self.request(state))
            async with self.db.transaction(self.scope) as c:
                await c.execute(
                    invocations.update()
                    .where(invocations.c.id == item["id"])
                    .values(state=state, approval_state="consumed", dispatch_deadline=0)
                )
            records.append((item, state))
        await self.exited()
        await self.store.finish_failed(self.binding, "PROCESS_EXITED")
        for item, state in records:
            saved = await self.gate.get(self.run["id"], item["id"])
            self.assertEqual(saved["state"], state)
            self.assertEqual(saved["version"], item["version"])
            self.assertFalse(any(e["stage"] == "lifecycle" for e in saved["evidence"]))
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(
                RuntimeErrorCode, "TOOL_RECONCILIATION_REQUIRED"
            ):
                await require_settled(c, self.scope, self.run["id"])

    async def test_cancelled_task_closes_unapproved_request(self):
        item = await self.request()
        current = await self.store.get(self.run["id"])
        await self.store.cancel(self.run["id"], current["version"])
        await self.exited()
        await self.store.confirm_cancel(*self.binding, "fixture-exit-confirmed")
        saved = await self.gate.get(self.run["id"], item["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["approval_state"], "pending")
        self.assertEqual((await self.store.get(self.run["id"]))["state"], "cancelled")

    async def test_successful_task_closes_unused_approved_request(self):
        from app.runtime.bridge import JsonlBridge
        from app.runtime.store import encoded

        item = await self.approve(await self.request())
        await self.store.report(
            *self.binding, 4, "validating", session_id="session-one"
        )
        await self.exited()
        result = self.provider.put(
            [encoded({"summary": "completed", "evidence": ["fixture"]})]
        )
        await self.store.complete(
            *self.binding, self.provider, result.key, JsonlBridge.validate
        )
        saved = await self.gate.get(self.run["id"], item["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["approval_state"], "approved")
        self.assertEqual((await self.store.get(self.run["id"]))["state"], "succeeded")

    async def test_terminal_closure_is_atomic_with_audit_and_task_state(self):
        item = await self.approve(await self.request())
        await self.exited()
        with (
            patch(
                "app.runtime.tool_invocations.audit",
                side_effect=RuntimeErrorCode("AUDIT_UNAVAILABLE"),
            ),
            self.assertRaisesRegex(RuntimeErrorCode, "AUDIT_UNAVAILABLE"),
        ):
            await self.store.finish_failed(self.binding, "PROCESS_EXITED")
        self.assertEqual((await self.store.get(self.run["id"]))["state"], "executing")
        saved = await self.gate.get(self.run["id"], item["id"])
        self.assertEqual(saved["state"], "prepared")
        self.assertEqual(saved["version"], item["version"])

    async def test_historical_repair_is_profile_scoped_idempotent_and_keeps_live_calls(
        self,
    ):
        item = await self.approve(await self.request())
        await self.gate.recover_pending("test-profile")
        self.assertEqual(
            (await self.gate.get(self.run["id"], item["id"]))["state"], "prepared"
        )
        # Existing records from before atomic closure was introduced.
        async with self.db.transaction(self.scope) as c:
            await c.execute(
                runs.update().where(runs.c.id == self.run["id"]).values(state="failed")
            )
        await self.gate.recover_pending("other-profile")
        self.assertEqual(
            (await self.gate.get(self.run["id"], item["id"]))["state"], "prepared"
        )
        await self.gate.recover_pending("test-profile")
        await self.gate.recover_pending("test-profile")
        saved = await self.gate.get(self.run["id"], item["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["version"], item["version"] + 1)
        async with self.db.read(self.scope) as c:
            rows = (
                await c.execute(
                    select(evidence).where(
                        evidence.c.invocation_id == item["id"],
                        evidence.c.stage == "lifecycle",
                    )
                )
            ).all()
        self.assertEqual(len(rows), 1)
