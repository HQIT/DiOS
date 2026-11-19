"""Fault injection: persistent shared cooldowns, single probes and original E2AG work."""
import asyncio,json,os,unittest
from dataclasses import replace
from uuid import uuid4
from sqlalchemy import select
import test_runtime_worker as f
from work_auth_fixtures import policy_fixture
from app.runtime.database import Database,RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.worker import Worker
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.faults import POLICY_ENV,category,policy
from app.runtime.fault_schema import plans,circuits,bindings
from app.runtime.schema import attempts
from app.runtime.work_auth_schema import work,grants
from app.runtime.lifecycle import Lifecycle
from app.runtime.recovery import backup_sqlite,verify_backup

def failure(code):return (json.dumps({'type':'error','code':code})+'\n').encode()

class FaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=f.WorkerTests();await self.f.asyncSetUp();self.addAsyncCleanup(self.f.asyncTearDown)
        self.store=self.f.store;self.db=self.f.db
        self.p=dict(resource='model:account-a',replay_safe=True,max_attempts=3,base_delay=10,max_delay=60,quota_delay=120,accept_complete_on_error=True)
        self.spec=replace(self.f.spec,environment={POLICY_ENV:json.dumps(self.p)})
        self.policy,self.descriptor=policy_fixture(self.f.root,'test-profile',self.spec)
    async def submit(self,key='one'):
        return await self.store.submit(key,dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),secret_refs=[],payload={}),authorization=self.descriptor)
    def worker(self,adapter=None):
        return Worker(self.store,adapter or f.FakeAdapter(),'test-profile',self.spec,self.f.provider,
            poll_interval=.05,authorization_verifier=WorkAuthorizationVerifier(self.store,self.policy))
    async def due(self):
        async with self.db.transaction(self.store.scope) as c:
            await c.execute(plans.update().where(plans.c.scope==self.store.scope,plans.c.state=='waiting').values(retry_at=0))
            await c.execute(circuits.update().where(circuits.c.scope==self.store.scope).values(retry_at=0))
    async def claim(self,w):
        await self.store.register_worker(w.worker_id,w.instance,'test',os.getpid())
        return await self.store.claim(w.worker_id,w.instance,profile_id='test-profile',recovery_policy=self.p)
    async def test_error_classes_are_explicit_and_unknown_text_cannot_trigger_retry(self):
        expected={'PROVIDER_NETWORK_ERROR':'network','PROVIDER_RATE_LIMITED':'rate_limit','PROVIDER_QUOTA_EXHAUSTED':'quota',
            'SESSION_CREATE_FAILED':'session','PROCESS_EXITED':'process','RESULT_FORMAT_INVALID':'format','EVIDENCE_INSUFFICIENT':'evidence'}
        for code,kind in expected.items():self.assertEqual(category(code),kind)
        self.assertEqual(category('stderr says HTTP 429'),'unknown')
        with self.assertRaises(RuntimeErrorCode):policy(dict(self.p,max_attempts=100))
        for invalid in ([],{},None,429):
            with self.assertRaisesRegex(RuntimeErrorCode,'RESULT_FORMAT_INVALID'):
                f.JsonlBridge().feed((json.dumps({'type':'error','code':invalid})+'\n').encode())
    async def test_backoff_retains_attempt_then_new_attempt_gets_new_grant(self):
        run=await self.submit();bad=self.worker(f.FakeAdapter(failure('PROVIDER_NETWORK_ERROR')))
        first=await bad.once();self.assertEqual(first['state'],'retry_wait')
        self.assertEqual(first['recovery']['category'],'network')
        self.assertEqual((await self.worker().once())['state'],'idle')
        await self.due();second=await self.worker().once()
        self.assertEqual(second['state'],'succeeded');self.assertEqual(second['id'],run['id'])
        self.assertEqual(len(second['attempts']),2);self.assertGreater(second['attempts'][1]['epoch'],second['attempts'][0]['epoch'])
        async with self.db.read(self.store.scope) as c:
            gs=(await c.execute(select(grants).where(grants.c.run_id==run['id']))).mappings().all()
        self.assertEqual(len(gs),2);self.assertNotEqual(gs[0]['attempt_id'],gs[1]['attempt_id'])
        self.assertEqual(second['recovery']['circuit']['state'],'closed')
    async def test_shared_limit_allows_only_one_half_open_probe_and_then_recovers(self):
        await self.submit();await self.worker(f.FakeAdapter(failure('PROVIDER_RATE_LIMITED'))).once()
        await self.submit('two');await self.submit('three')
        await self.due();workers=[self.worker() for _ in range(3)]
        claimed=await asyncio.gather(*(self.claim(w) for w in workers))
        active=[a for a in claimed if a];self.assertEqual(len(active),1)
        state=(await self.store.get(active[0]['run_id']))['recovery']['circuit'];self.assertEqual(state['probe_attempt'],active[0]['id'])
        async with self.db.transaction(self.store.scope) as c:
            from app.runtime.faults import outcome
            await outcome(c,self.store.scope,active[0]['id'],True,await self.db.now(c))
        self.assertIsNotNone(await self.claim(self.worker()))
    async def test_quota_uses_longer_cooldown_and_survives_service_restart(self):
        run=await self.submit();first=await self.worker(f.FakeAdapter(failure('PROVIDER_QUOTA_EXHAUSTED'))).once()
        self.assertGreaterEqual(first['recovery']['retry_at']-first['updated_at'],119)
        await self.db.close();self.db=Database(self.f.url);self.f.db=self.db;self.store=RuntimeStore(self.db,self.f.scope)
        self.assertEqual((await self.store.get(run['id']))['recovery']['circuit']['state'],'open')
        self.assertEqual((await self.worker().once())['state'],'idle')
        await self.due();self.assertEqual((await self.worker().once())['state'],'succeeded')
    async def test_repeated_probe_failure_is_bounded_by_attempt_budget(self):
        await self.submit();worker=self.worker(f.FakeAdapter(failure('SESSION_CREATE_FAILED')))
        for i in range(3):
            result=await worker.once();self.assertEqual(len(result['attempts']),i+1);await self.due()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['recovery']['reason'],'RETRY_BUDGET_EXHAUSTED')
        self.assertEqual((await self.worker().once())['state'],'idle')
    async def test_revocation_or_expiry_during_cooldown_prevents_new_execution(self):
        for index in range(2):
            run=await self.submit(str(index));await self.worker(f.FakeAdapter(failure('PROVIDER_NETWORK_ERROR'))).once()
            current=await self.store.get(run['id'])
            if index==0:await self.store.revoke(run['id'],current['version'])
            else:
                async with self.db.transaction(self.store.scope) as c:await c.execute(work.update().where(work.c.run_id==run['id']).values(expires_at=0))
            await self.due();self.assertEqual((await self.worker().once())['state'],'idle')
            result=await self.store.get(run['id']);self.assertEqual(result['state'],'failed');self.assertEqual(len(result['attempts']),1)
    async def test_cancel_and_drain_during_cooldown_do_not_relaunch(self):
        run=await self.submit();result=await self.worker(f.FakeAdapter(failure('PROVIDER_RATE_LIMITED'))).once()
        await Lifecycle(self.db,self.store.scope).begin_drain();await self.due()
        self.assertEqual((await self.worker().once())['state'],'idle')
        await self.store.cancel(run['id'],result['version']);self.assertEqual((await self.store.get(run['id']))['state'],'cancelled')
    async def test_format_and_evidence_failure_do_not_retry(self):
        for i,data in enumerate([b'not-json\n',f.protocol(evidence=False)]):
            await self.submit(str(i));result=await self.worker(f.FakeAdapter(data)).once()
            self.assertEqual(result['state'],'failed');self.assertEqual(result['recovery']['reason'],'NON_RETRYABLE')
    async def test_complete_result_is_validated_before_retrying_teardown_failure(self):
        await self.submit();result=await self.worker(f.FakeAdapter(exit_code=7)).once()
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(result['attempts']),1)
    async def test_complete_result_exit_conflict_requires_review_without_model_replay(self):
        self.p['accept_complete_on_error']=False
        self.spec=replace(self.spec,environment={POLICY_ENV:json.dumps(self.p)})
        self.policy,self.descriptor=policy_fixture(self.f.root,'test-profile',self.spec)
        await self.submit();result=await self.worker(f.FakeAdapter(exit_code=7)).once()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['recovery']['code'],'RESULT_EXIT_CONFLICT')
        self.assertEqual(result['recovery']['reason'],'NON_RETRYABLE')
        self.assertEqual((await self.worker().once())['state'],'idle')
    async def test_probe_query_timeout_keeps_probe_reserved_until_reconciliation(self):
        await self.submit();await self.worker(f.FakeAdapter(failure('PROVIDER_RATE_LIMITED'))).once();await self.due()
        adapter=f.FakeAdapter(failure=True);worker=self.worker(adapter)
        with self.assertRaises(TimeoutError):await worker.once()
        await self.submit('queued');self.assertEqual((await self.worker().once())['state'],'idle')
        self.assertTrue(adapter.stopped)
        adapter.failure=False;await worker.recover()
        self.assertEqual((await self.worker().once())['state'],'succeeded')
    async def test_late_success_does_not_close_an_open_circuit(self):
        a=await self.submit();b=await self.submit('two');w1,w2=self.worker(),self.worker()
        first,second=await self.claim(w1),await self.claim(w2)
        async with self.db.transaction(self.store.scope) as c:
            from app.runtime.faults import outcome
            now=await self.db.now(c);await outcome(c,self.store.scope,second['id'],False,now,'PROVIDER_RATE_LIMITED')
            await outcome(c,self.store.scope,first['id'],True,now)
            state=(await c.execute(select(circuits).where(circuits.c.scope==self.store.scope))).mappings().one()
        self.assertEqual(state['state'],'open')
    async def test_unapproved_retry_policy_and_manual_retry_cannot_bypass_cooldown(self):
        run=await self.submit();result=await self.worker(f.FakeAdapter(failure('PROVIDER_NETWORK_ERROR'))).once()
        with self.assertRaisesRegex(RuntimeErrorCode,'RETRY_MANAGED_BY_POLICY'):await self.store.retry(run['id'],result['version'])
        self.p['replay_safe']=False;await self.due()
        self.assertIsNone(await self.claim(self.worker()))
        self.assertEqual((await self.store.get(run['id']))['recovery']['reason'],'REPLAY_DISABLED')
    async def test_backup_contains_circuit_and_retry_plan(self):
        if not self.f.url.startswith('sqlite'):self.skipTest('SQLite backup provider')
        await self.submit();await self.worker(f.FakeAdapter(failure('PROVIDER_RATE_LIMITED'))).once()
        destination=self.f.root/'backup';await asyncio.to_thread(backup_sqlite,self.f.url,self.f.provider.root,destination)
        result=await asyncio.to_thread(verify_backup,destination);self.assertIn('runtime_fault_schema',result['schemas'])

    async def test_unknown_tool_effects_prevent_automatic_retry(self):
        from unittest.mock import AsyncMock,patch
        await self.submit()
        with patch('app.runtime.tool_invocations.require_settled',new=AsyncMock(side_effect=RuntimeErrorCode('TOOL_RESULT_UNKNOWN'))):
            result=await self.worker(f.FakeAdapter(failure('PROVIDER_NETWORK_ERROR'))).once()
        self.assertEqual(result['state'],'failed');self.assertEqual(result['recovery']['reason'],'TOOL_RESULT_UNKNOWN')
    async def test_different_account_circuit_is_independent(self):
        await self.submit();await self.worker(f.FakeAdapter(failure('PROVIDER_RATE_LIMITED'))).once()
        self.p=dict(self.p,resource='model:account-b')
        self.spec=replace(self.spec,environment={POLICY_ENV:json.dumps(self.p)})
        self.policy,self.descriptor=policy_fixture(self.f.root,'other-profile',self.spec)
        await self.store.submit('other',dict(runtime_profile='other-profile',profile_digest=self.spec.fingerprint(),secret_refs=[],payload={}),authorization=self.descriptor)
        worker=Worker(self.store,f.FakeAdapter(),'other-profile',self.spec,self.f.provider,
            poll_interval=.05,authorization_verifier=WorkAuthorizationVerifier(self.store,self.policy))
        self.assertEqual((await worker.once())['state'],'succeeded')

@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL required')
class PostgresFaultTests(FaultTests):
    async def asyncSetUp(self):
        await super().asyncSetUp();await self.db.close()
        self.f.url=os.environ['DIOS_TEST_POSTGRES_URL'];self.f.db=Database(self.f.url);self.db=self.f.db
        await self.db.migrate();self.store=RuntimeStore(self.db,self.f.scope)
