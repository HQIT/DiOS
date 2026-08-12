from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.database import Base
from app.models.tables import A2ATask, EventLog
from app.services import a2a_service
from app.services.e2ag import verify_audit_chain
from app.services.event_dispatcher import dispatch_event


def event(source: str, event_type: str, data: dict | None = None) -> dict:
    return {
        "specversion": "1.0",
        "id": f"evt_{source}_{event_type}",
        "source": source,
        "type": event_type,
        "subject": "test",
        "data": data or {},
    }


class DispatcherGovernanceTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_denied_event_is_audited_without_a2a_task(self):
        async with self.sessions() as db:
            log, error = await dispatch_event(
                event("webhook/attacker", "git.push"), [], db
            )
            self.assertEqual("denied", log.status)
            self.assertTrue(log.trace_id)
            self.assertEqual("deny", log.contract_decision["decision"])
            self.assertIn("CONTRACT_SOURCE_TYPE_UNBOUND", error)
            self.assertTrue(verify_audit_chain(log.audit_chain))
            self.assertEqual(0, len((await db.execute(A2ATask.__table__.select())).all()))

    async def test_approval_event_is_held_without_a2a_task(self):
        async with self.sessions() as db:
            log, error = await dispatch_event(
                event(
                    "github/acme/repo",
                    "git.push",
                    {"requested_action": "secret.read", "environment": "production"},
                ),
                [],
                db,
            )
            self.assertEqual("approval_required", log.status)
            self.assertEqual("approval_required", log.policy_decision["effective_decision"])
            self.assertIn("POLICY_PRODUCTION_SENSITIVE_ACTION", error)
            self.assertEqual(0, len((await db.execute(A2ATask.__table__.select())).all()))

    async def test_valid_unmatched_event_is_audited_as_dispatched(self):
        async with self.sessions() as db:
            log, error = await dispatch_event(event("manual/test", "manual.trigger"), [], db)
            self.assertEqual("dispatched", log.status)
            self.assertIsNone(error)
            stored = await db.get(EventLog, log.id)
            self.assertEqual(log.trace_id, stored.trace_id)
            self.assertEqual("allow", stored.contract_decision["decision"])

    async def test_ablation_modes_change_effective_enforcement(self):
        spoof = event("webhook/attacker", "git.push")
        async with self.sessions() as db:
            settings.e2ag_mode = "off"
            off_log, _ = await dispatch_event(spoof, [], db)
            self.assertEqual("dispatched", off_log.status)
            self.assertEqual("E2AG_DISABLED", off_log.contract_decision["reason_codes"][0])

            settings.e2ag_mode = "contract"
            contract_log, _ = await dispatch_event(spoof, [], db)
            self.assertEqual("denied", contract_log.status)

    async def test_trace_id_is_persisted_on_a2a_task(self):
        async with self.sessions() as db:
            task = await a2a_service.create_task(
                db,
                agent_id="agent-test",
                message={"role": "user", "parts": []},
                context_id="event-test",
                trace_id="trace-test",
            )
            self.assertEqual("trace-test", task.trace_id)
            self.assertEqual("trace-test", a2a_service.task_to_a2a_dict(task)["_e2ag"]["trace_id"])


if __name__ == "__main__":
    unittest.main()
