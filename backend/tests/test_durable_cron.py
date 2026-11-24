import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
from app.config import settings
from app.models.tables import (
    A2ATask,
    Agent,
    CronCursor,
    CronOccurrence,
    EventLog,
    ManagedRuntimeControl,
    Subscription,
)
from app.runtime.database import RuntimeErrorCode
from app.services import a2a_service, schedule_control
from app.services import durable_cron as cron
from app.services.e2ag import Decision, verify_audit_chain
from fastapi import FastAPI
from sqlalchemy import select
from tests import test_managed_runtime as fixtures


class DurableCronTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.ManagedRuntimeTests.asyncSetUp
    asyncTearDown = fixtures.ManagedRuntimeTests.asyncTearDown
    start = datetime(2026, 11, 24, 0, 0, 0, tzinfo=UTC)

    async def setup_schedule(self):
        self.stack.enter_context(patch.object(settings, "e2ag_mode", "enforce"))
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.capabilities = {
                "governance": {
                    "allowed_event_sources": ["cron/*"],
                    "allowed_event_types": ["cron.tick"],
                }
            }
            sub = Subscription(
                id="timer",
                agent_id="agent",
                source_pattern="cron/*",
                event_types=["cron.tick"],
                cron_expression="* * * * *",
                enabled=True,
            )
            db.add(sub)
            await db.flush()
            await cron.synchronize(db, sub, self.start)
            await db.commit()

        async def execute(db, agent_id, message, context_id, trace_id):
            task = await a2a_service.create_task(
                db, agent_id, message, context_id, trace_id
            )
            task.status = "working"
            await db.commit()
            return task

        self.send = self.stack.enter_context(
            patch.object(a2a_service, "send_message", side_effect=execute)
        )

    async def tick(self, minute):
        await cron.CronScheduler()._tick(self.start + timedelta(minutes=minute))

    async def records(self, model):
        async with self.sessions() as db:
            return list((await db.scalars(select(model))).all())

    async def test_persistent_first_fire_restart_and_duplicate_tick(self):
        await self.setup_schedule()
        await self.tick(0)
        self.assertEqual([], await self.records(EventLog))
        await self.tick(1)
        await self.tick(1)
        await asyncio.gather(self.tick(1), self.tick(1))
        self.assertEqual(1, len(await self.records(A2ATask)))
        self.assertEqual(1, len(await self.records(EventLog)))
        self.assertEqual(1, self.send.call_count)
        occurrence = (await self.records(CronOccurrence))[0]
        self.assertEqual("running", occurrence.state)
        self.assertEqual(
            self.start + timedelta(minutes=1), cron.utc(occurrence.first_due)
        )

    async def test_overlapping_ticks_buffer_one_and_keep_all_trigger_counts(self):
        await self.setup_schedule()
        await self.tick(1)
        await self.tick(4)
        await self.tick(7)
        self.assertEqual(1, self.send.call_count)
        rows = await self.records(CronOccurrence)
        buffered = next(r for r in rows if r.state == "buffered")
        self.assertEqual(6, buffered.trigger_count)
        async with self.sessions() as db:
            task = await db.scalar(select(A2ATask))
            task.status = "completed"
            await db.commit()
        await self.tick(7)
        self.assertEqual(2, self.send.call_count)
        self.assertEqual(2, len(await self.records(CronOccurrence)))
        self.assertEqual(2, len(await self.records(A2ATask)))

    async def test_unknown_task_blocks_replacement_and_failed_task_is_dead_letter(self):
        await self.setup_schedule()
        await self.tick(1)
        async with self.sessions() as db:
            task = await db.scalar(select(A2ATask))
            task.error = "RPC_OBSERVATION_TIMEOUT"
            await db.commit()
        await self.tick(10)
        self.assertEqual(1, self.send.call_count)
        async with self.sessions() as db:
            task = await db.scalar(select(A2ATask))
            task.status = "failed"
            await db.commit()
        await self.tick(11)
        item = next(
            x for x in await self.records(CronOccurrence) if x.state == "dead_letter"
        )
        with self.assertRaisesRegex(RuntimeErrorCode, "RECONCILIATION"):
            await schedule_control.resolve(
                item.id,
                action="retry",
                actor="operator",
                reason="diagnosed",
                idempotency_key="r1",
            )
        result = await schedule_control.resolve(
            item.id,
            action="discard",
            actor="operator",
            reason="failure reviewed",
            idempotency_key="r2",
        )
        self.assertEqual("discarded", result["state"])
        await schedule_control.resolve(
            item.id,
            action="discard",
            actor="operator",
            reason="failure reviewed",
            idempotency_key="r2",
        )
        await self.tick(11)
        self.assertEqual(2, self.send.call_count)
        log = (await self.records(EventLog))[0]
        self.assertTrue(verify_audit_chain(log.audit_chain))

    async def test_outbox_survives_crash_before_delivery(self):
        await self.setup_schedule()
        with (
            patch.object(cron, "deliver", side_effect=asyncio.CancelledError),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.tick(1)
        item = (await self.records(CronOccurrence))[0]
        self.assertEqual("pending", item.state)
        self.assertEqual([], await self.records(EventLog))
        await self.tick(1)
        self.assertEqual(item.event_id, (await self.records(EventLog))[0].id)
        self.assertEqual(1, self.send.call_count)

    async def test_event_receipt_survives_crash_before_task_creation(self):
        await self.setup_schedule()
        from app.services import event_dispatcher

        with (
            patch.object(
                event_dispatcher,
                "_send_event_message",
                side_effect=asyncio.CancelledError,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.tick(1)
        self.assertEqual(1, len(await self.records(EventLog)))
        self.assertEqual([], await self.records(A2ATask))
        await self.tick(1)
        self.assertEqual(1, len(await self.records(EventLog)))
        self.assertEqual(1, len(await self.records(A2ATask)))

    async def test_task_receipt_survives_crash_and_no_second_model_call(self):
        await self.setup_schedule()
        original = self.send.side_effect

        async def crash(**kwargs):
            await original(**kwargs)
            raise asyncio.CancelledError()

        self.send.side_effect = crash
        with self.assertRaises(asyncio.CancelledError):
            await self.tick(1)
        self.send.side_effect = original
        await self.tick(2)
        self.assertEqual(1, self.send.call_count)
        self.assertEqual(1, len(await self.records(A2ATask)))

    async def test_drain_keeps_pending_without_spending_retries(self):
        await self.setup_schedule()
        async with self.sessions() as db:
            db.add(ManagedRuntimeControl(scope=settings.runtime_scope, mode="draining"))
            await db.commit()
        await self.tick(2)
        self.assertEqual(0, self.send.call_count)
        item = (await self.records(CronOccurrence))[0]
        self.assertEqual(("pending", 0), (item.state, item.attempts))
        async with self.sessions() as db:
            control = await db.get(ManagedRuntimeControl, settings.runtime_scope)
            control.mode = "running"
            await db.commit()
        await self.tick(2)
        self.assertEqual(1, self.send.call_count)

    async def test_catchup_is_bounded_and_expired_range_visible(self):
        await self.setup_schedule()
        await self.tick(24 * 60 * 100)
        rows = await self.records(CronOccurrence)
        expired = next(x for x in rows if x.state == "expired")
        self.assertIsNone(expired.trigger_count)
        active = next(x for x in rows if x.state == "running")
        self.assertEqual(1440, active.trigger_count)
        self.assertEqual(1, self.send.call_count)

    async def test_disabled_changed_and_deleted_keep_original_active_task(self):
        await self.setup_schedule()
        await self.tick(1)
        await self.tick(3)
        async with self.sessions() as db:
            sub = await db.get(Subscription, "timer")
            sub.enabled = False
            await db.commit()
        await self.tick(4)
        self.assertEqual(1, self.send.call_count)
        self.assertIn(
            "cancelled", [x.state for x in await self.records(CronOccurrence)]
        )
        async with self.sessions() as db:
            await db.delete(await db.get(Subscription, "timer"))
            await db.commit()
        await self.tick(5)
        self.assertEqual(1, self.send.call_count)
        self.assertIsNotNone((await self.records(CronCursor))[0].active_id)

    async def test_pre_dispatch_failure_backoff_dead_letter_and_explicit_retry(self):
        await self.setup_schedule()
        with patch(
            "app.services.event_dispatcher.dispatch_event", side_effect=ConnectionError
        ):
            await self.tick(1)
            await self.tick(2)
            self.assertEqual(1, (await self.records(CronOccurrence))[0].attempts)
            await self.tick(3)
            await self.tick(7)
        item = next(
            x for x in await self.records(CronOccurrence) if x.state == "dead_letter"
        )
        self.assertEqual(3, item.attempts)
        await schedule_control.resolve(
            item.id,
            action="retry",
            actor="operator",
            reason="recovered",
            idempotency_key="retry-1",
        )
        await self.tick(7)
        self.assertEqual(1, self.send.call_count)

    async def test_e2ag_denial_and_approval_do_not_launch(self):
        await self.setup_schedule()
        with patch(
            "app.services.event_dispatcher.evaluate_policy",
            return_value=Decision(
                stage="policy", decision="approval_required", reason_codes=("REVIEW",)
            ),
        ):
            await self.tick(1)
        self.assertEqual(0, self.send.call_count)
        await self.tick(3)
        self.assertEqual(0, self.send.call_count)
        self.assertEqual(
            "approval_required", (await self.records(CronOccurrence))[0].state
        )

    async def test_api_saves_cron_and_reports_timer_state(self):
        from app.api.os import runtime_schedules, subscriptions
        from app.db.database import get_db

        app = FastAPI()
        app.include_router(subscriptions.router)
        app.include_router(runtime_schedules.router)

        async def session():
            async with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = session
        self.stack.enter_context(
            patch.object(settings, "access_token", "operator-fixture")
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/agents/agent/subscriptions",
                json={
                    "source_pattern": "cron/*",
                    "event_types": ["cron.tick"],
                    "cron_expression": "* * * * *",
                },
            )
            self.assertEqual(201, response.status_code, response.text)
            body = response.json()
            self.assertEqual("* * * * *", body["cron_expression"])
            self.assertEqual("buffer_one", body["schedule"]["overlap"])
            response = await client.put(
                f"/agents/agent/subscriptions/{body['id']}",
                json={"cron_expression": "nonsense"},
            )
            self.assertEqual(422, response.status_code)
            self.assertEqual(401, (await client.get("/runtime/schedules")).status_code)
            response = await client.get(
                "/runtime/schedules",
                headers={"authorization": "Bearer operator-fixture"},
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("no-store", response.headers["cache-control"])

    async def test_calendar_validation(self):
        for expression in ("* * * * *", "0 0 29 2 *", "0 9 * MAR THU"):
            self.assertEqual(expression, cron.validate_expression(expression))
        for expression in ("0 0 31 2 *", "@reboot", "R * * * *", "* * * * * *"):
            with self.assertRaises(ValueError):
                cron.validate_expression(expression)

    async def test_history_pages_are_chronological_and_scope_bound(self):
        await self.setup_schedule()
        await self.tick(1)
        await self.tick(4)
        async with self.sessions() as db:
            page = await schedule_control.history(db, "timer", limit=1)
            self.assertEqual("buffered", page["items"][0]["state"])
            previous = await schedule_control.history(db, "timer", limit=1, before=page["next_cursor"])
            self.assertEqual("running", previous["items"][0]["state"])
            with self.assertRaisesRegex(RuntimeErrorCode, "INVALID_CURSOR"):
                await schedule_control.history(db, "other", before=page["next_cursor"])

    async def test_resolution_rejects_corrupt_audit_and_active_original(self):
        await self.setup_schedule()
        await self.tick(1)
        item = (await self.records(CronOccurrence))[0]
        with self.assertRaisesRegex(RuntimeErrorCode, "SCHEDULE_NOT_DEAD_LETTER"):
            await schedule_control.resolve(item.id, action="discard", actor="operator", reason="test", idempotency_key="d1")
        async with self.sessions() as db:
            task = await db.scalar(select(A2ATask))
            task.status = "failed"
            log = await db.scalar(select(EventLog))
            log.audit_chain = [{"invalid": True}]
            await db.commit()
        await self.tick(2)
        with self.assertRaisesRegex(RuntimeErrorCode, "SCHEDULE_AUDIT_INVALID"):
            await schedule_control.resolve(item.id, action="discard", actor="operator", reason="test", idempotency_key="d2")

    async def test_missing_approval_evidence_exhausts_retries_without_reviving_dead_letter(self):
        await self.setup_schedule()
        with patch("app.services.event_dispatcher._send_event_message", side_effect=asyncio.CancelledError), self.assertRaises(asyncio.CancelledError):
            await self.tick(1)
        async with self.sessions() as db:
            log = await db.scalar(select(EventLog))
            log.status = "approval_approved"
            await db.commit()
        for minute in (1, 3, 7, 11, 15):
            await self.tick(minute)
        item = next(x for x in await self.records(CronOccurrence) if x.state == "dead_letter")
        self.assertEqual(3, item.attempts)
        self.assertEqual(0, self.send.call_count)

    async def test_recovery_rechecks_e2ag_and_records_denial(self):
        await self.setup_schedule()
        from app.services import event_dispatcher

        with (
            patch.object(
                event_dispatcher,
                "_send_event_message",
                side_effect=asyncio.CancelledError,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.tick(1)
        async with self.sessions() as db:
            agent = await db.get(Agent, "agent")
            agent.capabilities = {"governance": {"allowed_event_types": ["git.push"]}}
            await db.commit()
        await self.tick(1)
        self.assertEqual("denied", (await self.records(EventLog))[0].status)
        self.assertEqual(0, self.send.call_count)

    async def test_recovery_repairs_approval_creation_and_resumes_approved_receipt(
        self,
    ):
        await self.setup_schedule()
        from app.models.tables import E2AGApproval

        approval = Decision(
            stage="policy", decision="approval_required", reason_codes=("REVIEW",)
        )
        with (
            patch(
                "app.services.event_dispatcher.evaluate_policy", return_value=approval
            ),
            patch(
                "app.services.e2ag_approval.create_approval",
                side_effect=asyncio.CancelledError,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.tick(1)
        self.assertEqual([], await self.records(E2AGApproval))
        await self.tick(1)
        self.assertEqual(1, len(await self.records(E2AGApproval)))
        from app.services.e2ag_approval import decide_approval

        with (
            patch(
                "app.services.event_dispatcher.resume_approved_event",
                side_effect=asyncio.CancelledError,
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            async with self.sessions() as db:
                row = await db.scalar(select(E2AGApproval))
                await decide_approval(db, row.id, decision="approved", actor="operator")
        await self.tick(1)
        await self.tick(1)
        self.assertEqual(1, self.send.call_count)
        self.assertEqual(1, len(await self.records(A2ATask)))

    async def test_disabled_pending_and_clock_backwards_do_not_dispatch(self):
        await self.setup_schedule()
        with (
            patch.object(cron, "deliver", side_effect=asyncio.CancelledError),
            self.assertRaises(asyncio.CancelledError),
        ):
            await self.tick(1)
        async with self.sessions() as db:
            sub = await db.get(Subscription, "timer")
            sub.enabled = False
            await db.commit()
        await self.tick(2)
        self.assertEqual(0, self.send.call_count)
        self.assertEqual("cancelled", (await self.records(CronOccurrence))[0].state)
        async with self.sessions() as db:
            sub = await db.get(Subscription, "timer")
            sub.enabled = True
            await cron.synchronize(db, sub, self.start + timedelta(minutes=2))
            await db.commit()
        await self.tick(1)
        self.assertEqual(0, self.send.call_count)
        await self.tick(3)
        self.assertEqual(1, self.send.call_count)
