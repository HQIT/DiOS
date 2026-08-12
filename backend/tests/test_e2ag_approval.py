from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import Base
from app.models.tables import A2ATask, Agent, E2AGApproval
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_approval import decide_approval
from app.services.event_dispatcher import dispatch_event


def sensitive_event() -> dict:
    return {
        "specversion": "1.0",
        "id": "evt-approval",
        "source": "github/acme/repo",
        "type": "git.push",
        "subject": "refs/heads/main",
        "data": {
            "requested_action": "secret.read",
            "environment": "production",
        },
    }


class E2AGApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.old_mode = settings.e2ag_mode
        self.old_dedup = settings.event_dedup_enabled
        settings.e2ag_mode = "enforce"
        settings.event_dedup_enabled = False

    async def asyncTearDown(self):
        settings.e2ag_mode = self.old_mode
        settings.event_dedup_enabled = self.old_dedup
        await self.engine.dispose()

    async def _pending(self, db, with_agent: bool = False):
        agent_ids: list[str] = []
        if with_agent:
            agent = Agent(
                id="approval-agent",
                name="Approval Agent",
                mode="task",
                capabilities={"governance": {
                    "allowed_event_sources": ["github/acme/*"],
                    "allowed_event_types": ["git.push"],
                    "allowed_actions": ["secret.read"],
                }},
            )
            db.add(agent)
            await db.commit()
            agent_ids = [agent.id]
        log, _ = await dispatch_event(sensitive_event(), agent_ids, db)
        approval = (await db.execute(select(E2AGApproval))).scalar_one()
        return log, approval

    async def test_pending_approval_is_created_and_rejection_is_final(self):
        async with self.sessions() as db:
            log, approval = await self._pending(db)
            self.assertEqual("pending", approval.status)
            self.assertEqual(log.trace_id, approval.trace_id)
            self.assertTrue(verify_audit_chain(log.audit_chain))
            decided, event_log = await decide_approval(
                db, approval.id, decision="rejected", actor="security@example.com",
                reason="unexpected production access",
            )
            self.assertEqual("rejected", decided.status)
            self.assertEqual("approval_rejected", event_log.status)
            self.assertEqual(0, len((await db.execute(A2ATask.__table__.select())).all()))
            with self.assertRaisesRegex(ValueError, "APPROVAL_ALREADY_DECIDED"):
                await decide_approval(db, approval.id, decision="approved", actor="other")

    async def test_expired_approval_cannot_resume(self):
        async with self.sessions() as db:
            log, approval = await self._pending(db)
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
            with self.assertRaisesRegex(ValueError, "APPROVAL_EXPIRED"):
                await decide_approval(db, approval.id, decision="approved", actor="security")
            await db.refresh(log)
            await db.refresh(approval)
            self.assertEqual("expired", approval.status)
            self.assertEqual("approval_expired", log.status)
            self.assertTrue(verify_audit_chain(log.audit_chain))

    async def test_approval_resumes_same_trace_exactly_once(self):
        async with self.sessions() as db:
            log, approval = await self._pending(db, with_agent=True)

            async def fake_send_message(*, db, agent_id, message, context_id, trace_id):
                return await __import__(
                    "app.services.a2a_service", fromlist=["create_task"]
                ).create_task(
                    db, agent_id=agent_id, message=message,
                    context_id=context_id, trace_id=trace_id,
                )

            with patch(
                "app.services.a2a_service.send_message",
                AsyncMock(side_effect=fake_send_message),
            ) as mocked:
                decided, event_log = await decide_approval(
                    db, approval.id, decision="approved", actor="security",
                )
            tasks = (await db.execute(select(A2ATask))).scalars().all()
            self.assertEqual("approved", decided.status)
            self.assertEqual("dispatched", event_log.status)
            self.assertEqual(1, mocked.await_count)
            self.assertEqual(1, len(tasks))
            self.assertEqual(log.trace_id, tasks[0].trace_id)
            self.assertEqual(log.id, tasks[0].context_id)
            self.assertTrue(verify_audit_chain(event_log.audit_chain))


if __name__ == "__main__":
    unittest.main()
