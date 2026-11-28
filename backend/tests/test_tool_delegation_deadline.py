"""Native OPA and MCP gate: delayed final delegation replies cannot renew execution."""
import os
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from sqlalchemy import select
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.policy_evaluator import OpaEvaluator
from app.runtime.tool_schema import evidence
from app.runtime.work_auth_schema import grants
import test_runtime_delegation_lifecycle as fixtures


@unittest.skipUnless(os.environ.get('DIOS_TEST_OPA') == '1', 'requires native OPA')
class ToolDelegationDeadlineTests(unittest.IsolatedAsyncioTestCase):
    observations = []

    async def asyncSetUp(self):
        self.f = fixtures.DelegationLifecycleTests()
        self.addAsyncCleanup(self.f.cleanup)
        await self.f.asyncSetUp()
        self.assertEqual(self.f.attempt['run_id'], self.f.run['id'])

    async def exercise(self, operation, expired, *, advance=True):
        f = self.f
        item = None
        if operation != 'request':
            item = await f.request()
        if operation == 'dispatch':
            item = await f.approve(item)
        async with f.db.read(f.scope) as c:
            now = await f.db.now(c)
            run = await f.store.get(f.run['id'])
            lease = run['attempts'][0]['lease_until']
            grant = (await c.execute(select(grants).where(
                grants.c.attempt_id == f.attempt['id']))).mappings().one()
        deadline = lease if expired == 'lease' else now + 15
        if expired == 'grant':
            self.assertLess(deadline, grant['expires_at'])
            async with f.db.transaction(f.scope) as c:
                await c.execute(grants.update().where(grants.c.attempt_id == f.attempt['id'])
                    .values(expires_at=deadline))
        offset = 0
        replies = 0
        policy_seen = False
        initialized = operation != 'dispatch'
        original_time = Database.now
        original_evaluate = OpaEvaluator.evaluate
        transport = f.gate.transport

        async def clock(c):
            return await original_time(c) + offset

        async def evaluate(evaluator, config, value):
            nonlocal offset, replies, policy_seen
            result = await original_evaluate(evaluator, config, value)
            if not initialized:
                return result
            if config['url'].endswith('/dios/tools/allow'):
                policy_seen = True
            if (policy_seen and config['url'].endswith('/dios/delegation/allow')
                    and value['context']['permission']['action'] == 'repository.search'):
                self.assertTrue(result['allow'])
                replies += 1
                if advance:
                    # Advance the authoritative DB clock only after the final real PDP reply.
                    # No Run/Attempt/Grant row or ownership check is replaced.
                    offset = deadline - now + 1
            return result

        class Transport:
            @asynccontextmanager
            async def open(self, *args):
                nonlocal initialized
                async with transport.open(*args) as channel:
                    initialized = True
                    yield channel

        f.gate.transport = Transport()
        error = 'STALE_OWNER' if expired == 'lease' else 'EXECUTION_GRANT_EXPIRED'
        # For the grant case, keep the independently checked lease valid longer.
        if expired == 'grant':
            from app.runtime.schema import attempts
            async with f.db.transaction(f.scope) as c:
                await c.execute(attempts.update().where(attempts.c.id == f.attempt['id'])
                    .values(lease_until=deadline + 60))
        async def invoke():
            if operation == 'request':
                return await f.request('deadline-request')
            if operation == 'approve':
                return await f.approve(item)
            return await f.gate.dispatch(f.run['id'], item['id'], item['version'])

        with patch.object(Database, 'now', staticmethod(clock)), patch.object(
                OpaEvaluator, 'evaluate', evaluate):
            if advance:
                with self.assertRaisesRegex(RuntimeErrorCode, error):
                    await invoke()
            else:
                result = await invoke()
                self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(replies, 1, 'must reach the final tool-specific delegation reply')
        self.assertEqual(len(f.calls), 0 if advance else 1)
        if item and advance:
            saved = await f.gate.get(f.run['id'], item['id'])
            self.assertEqual(saved['version'], item['version'])
            self.assertEqual(saved['approval_state'], item['approval_state'])
            async with f.db.read(f.scope) as c:
                decisions = (await c.execute(select(evidence.c.decision).where(
                    evidence.c.invocation_id == item['id'], evidence.c.stage == operation
                ))).scalars().all()
            if operation == 'dispatch':
                self.assertTrue(any(error in x.get('reason_codes', []) for x in decisions))
                self.assertFalse(any(x['decision'] == 'allow' for x in decisions))
        self.observations.append(dict(case=self._testMethodName, operation=operation,
            expired=expired if advance else None, delayed_allow_replies=replies,
            tool_calls=len(f.calls), clock='DB time offset after real OPA reply',
            run_id=f.run['id'], attempt_id=f.attempt['id']))

    async def test_expired_lease_blocks_request(self):
        await self.exercise('request', 'lease')

    async def test_expired_lease_blocks_approval(self):
        await self.exercise('approve', 'lease')

    async def test_expired_lease_blocks_final_dispatch(self):
        await self.exercise('dispatch', 'lease')

    async def test_expired_grant_blocks_final_dispatch(self):
        await self.exercise('dispatch', 'grant')

    async def test_current_authority_dispatches_once(self):
        await self.exercise('dispatch', 'lease', advance=False)
