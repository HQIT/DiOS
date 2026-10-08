"""A task returned in failed state must still have a causal audit link."""
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app.models.tables import Agent, A2ATask, E2AGApproval
from app.services.event_dispatcher import dispatch_event
from app.services.e2ag_approval import decide_approval
from app.services.e2ag import verify_audit_chain
import test_e2ag_dispatcher as base_tests


class FailedTaskEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base_tests.DispatcherGovernanceTests.asyncSetUp
    asyncTearDown = base_tests.DispatcherGovernanceTests.asyncTearDown

    async def prepare(self, db):
        db.add(Agent(id="failed-agent", name="failed-agent", mode="task", capabilities={"governance": {
            "allowed_event_sources": ["github/acme/*"], "allowed_event_types": ["git.push"],
            "allowed_actions": ["report.create", "secret.read"]}}))
        await db.commit()

    async def fail_launch(self, db, agent, task):
        raise RuntimeError("controlled launch failure")

    def check_link(self, log, task):
        records = [x for x in log.audit_chain if x["stage"] == "a2a_task"]
        self.assertEqual(1, len(records))
        self.assertEqual(task.id, records[0]["evidence"]["task_id"])
        self.assertEqual("failed", records[0]["outcome"])
        self.assertEqual(log.id, task.context_id)
        self.assertTrue(verify_audit_chain(log.audit_chain))

    async def test_failed_dispatch_task_is_linked(self):
        async with self.sessions() as db:
            await self.prepare(db)
            with patch("app.services.a2a_service._send_to_task_agent", self.fail_launch):
                log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push",
                    {"requested_action": "report.create"}), ["failed-agent"], db)
            task = (await db.execute(select(A2ATask))).scalar_one()
            self.assertEqual("failed", log.status)
            self.check_link(log, task)

    async def test_failed_task_after_approval_is_linked(self):
        async with self.sessions() as db:
            await self.prepare(db)
            log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push",
                {"requested_action": "secret.read", "environment": "production"}), ["failed-agent"], db)
            approval = (await db.execute(select(E2AGApproval))).scalar_one()
            with patch("app.services.a2a_service._send_to_task_agent", self.fail_launch):
                _, log = await decide_approval(db, approval.id, decision="approved", actor="test")
            task = (await db.execute(select(A2ATask))).scalar_one()
            self.assertEqual("failed", log.status)
            self.check_link(log, task)


if __name__ == "__main__":
    unittest.main()
