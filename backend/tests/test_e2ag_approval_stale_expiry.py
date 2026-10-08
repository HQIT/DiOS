"""A stale expiry decision must not replace a committed terminal decision."""
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch
from sqlalchemy import select
from app.models.tables import E2AGApproval
from app.services.e2ag_approval import decide_approval
import test_e2ag_approval as base_tests


class ApprovalStaleExpiryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base_tests.E2AGApprovalTests.asyncSetUp
    asyncTearDown = base_tests.E2AGApprovalTests.asyncTearDown
    _pending = base_tests.E2AGApprovalTests._pending

    async def check_terminal(self, terminal):
        async with self.sessions() as stale:
            _, held = await self._pending(stale)
            identity = held.id
            expiry = held.expires_at
            expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
            async with self.sessions() as writer:
                await decide_approval(writer, identity, decision=terminal, actor="first-reviewer")
            # Models an earlier-read pending snapshot reaching the expiry check
            # after another session has committed, not a sleep/throughput test.
            with patch("app.services.e2ag_approval._now", return_value=expiry + timedelta(seconds=1)):
                with self.assertRaisesRegex(ValueError, "APPROVAL_ALREADY_DECIDED"):
                    await decide_approval(stale, identity, decision="approved", actor="stale-reviewer")
        async with self.sessions() as reader:
            actual = (await reader.execute(select(E2AGApproval))).scalar_one()
            self.assertEqual(terminal, actual.status)
            self.assertEqual("first-reviewer", actual.actor)

    async def test_stale_expiry_cannot_replace_approval(self):
        await self.check_terminal("approved")

    async def test_stale_expiry_cannot_replace_rejection(self):
        await self.check_terminal("rejected")
