"""Dispatch completion must not overwrite evidence from another DB session."""
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app.models.tables import E2AGApproval
from app.services.e2ag import verify_audit_chain
from app.services.e2ag_approval import decide_approval
from app.services.e2ag_tool_gateway import _append_event_audit
from app.services.event_dispatcher import dispatch_event
import test_e2ag_failed_task_evidence as fixture_tests
import test_e2ag_dispatcher as base_tests


class CrossSessionAuditTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture_tests.FailedTaskEvidenceTests.asyncSetUp
    asyncTearDown = fixture_tests.FailedTaskEvidenceTests.asyncTearDown
    prepare = fixture_tests.FailedTaskEvidenceTests.prepare

    async def independently_recorded_execution(self, db, agent, task):
        async with self.sessions() as writer:
            await _append_event_audit(writer, task.context_id, trace_id=task.trace_id,
                stage="qualification_probe", outcome="observed", evidence={"task_id": task.id, "writer": "other_session"})
            await writer.commit()
        task.status = "completed"
        await db.commit()

    def check_evidence(self, log):
        probes = [e for e in log.audit_chain if e["stage"] == "qualification_probe"]
        self.assertEqual(1, len(probes), "Dispatch overwrote another session's committed evidence")
        self.assertEqual("other_session", probes[0]["evidence"]["writer"])
        self.assertTrue(verify_audit_chain(log.audit_chain))
        self.assertEqual(1, sum(e["stage"] == "a2a_task" for e in log.audit_chain))

    async def test_dispatch_preserves_other_session_evidence(self):
        async with self.sessions() as db:
            await self.prepare(db)
            with patch("app.services.a2a_service._send_to_task_agent", self.independently_recorded_execution):
                log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push",
                    {"requested_action": "report.create"}), ["failed-agent"], db)
            self.check_evidence(log)

    async def test_approval_resume_preserves_other_session_evidence(self):
        async with self.sessions() as db:
            await self.prepare(db)
            log, _ = await dispatch_event(base_tests.event("github/acme/repo", "git.push",
                {"requested_action": "secret.read", "environment": "production"}), ["failed-agent"], db)
            approval = (await db.execute(select(E2AGApproval))).scalar_one()
            with patch("app.services.a2a_service._send_to_task_agent", self.independently_recorded_execution):
                _, log = await decide_approval(db, approval.id, decision="approved", actor="qualification")
            self.check_evidence(log)
