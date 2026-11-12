import asyncio
from dataclasses import replace
import json
import os
import unittest
from unittest.mock import patch
from sqlalchemy import select
from app.runtime.adapter import Observation
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.schema import attempts, artifacts
from app.runtime.reporting import relay_reports
from app.runtime.reconciliation import reconcile_run, ExitReceipts
from app.runtime.worker import Worker
import test_runtime_worker as fixtures
from test_runtime_worker import FakeAdapter, protocol


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.WorkerTests.asyncSetUp
    asyncTearDown = fixtures.WorkerTests.asyncTearDown
    submit = fixtures.WorkerTests.submit
    worker = fixtures.WorkerTests.worker

    async def interrupted(self, adapter=None):
        r = await self.submit()
        adapter = adapter or FakeAdapter()
        with patch.object(self.store, 'complete', side_effect=ConnectionError('controller unavailable')):
            with self.assertRaises(ConnectionError): await self.worker(adapter).once()
        r = await self.store.get(r['id'])
        self.assertEqual(r['state'], 'reconciliation_required')
        return r, adapter

    async def recover(self, r, adapter, **kwargs):
        return await reconcile_run(self.store,adapter,self.provider,r['id'],**kwargs)

    async def test_complete_output_recovers_same_attempt_and_session_without_launch(self):
        r, adapter = await self.interrupted()
        a = r['attempts'][0]
        result = await self.recover(r, adapter)
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['attempt_id'],a['id'])
        self.assertEqual(result['attempts'][0]['session_id'],a['session_id'])
        self.assertEqual(adapter.started,1)
        async with self.db.read(self.scope) as c:
            blobs = (await c.execute(select(artifacts).where(artifacts.c.run_id==r['id']))).all()
        self.assertEqual(len(blobs),3)
        version=result['version']
        self.assertEqual((await self.recover(r,adapter))['version'],version)
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):
            await self.store.heartbeat(r['id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch'])

    async def test_receipt_survives_restart_and_missing_container(self):
        r, adapter=await self.interrupted()
        await self.db.close()
        self.db=Database(self.url);self.store=RuntimeStore(self.db,self.scope)
        async def absent(handle):return Observation(handle,'absent',False,None,None,None,'unknown','unknown')
        adapter.observe=absent
        result=await self.recover(r,adapter)
        self.assertEqual(result['state'],'succeeded');self.assertEqual(adapter.started,1)

    async def test_alive_or_absent_without_receipt_remains_unknown(self):
        r=await self.submit();adapter=FakeAdapter(failure=True)
        with self.assertRaises(TimeoutError):await self.worker(adapter).once()
        async def alive(handle):return adapter.observation(handle)
        adapter.observe=alive
        result=await self.recover(r,adapter)
        self.assertEqual(result['state'],'reconciliation_required')
        self.assertFalse(adapter.stopped)
        async def absent(handle):return Observation(handle,'absent',False,None,None,None,'unknown','unknown')
        adapter.observe=absent
        self.assertEqual((await self.recover(r,adapter))['state'],'reconciliation_required')
        self.assertEqual(adapter.started,1)

    async def test_replaced_process_and_changed_session_rejected(self):
        r,adapter=await self.interrupted()
        original=adapter.observe
        async def replaced(handle):return replace(await original(handle),process_instance='replacement')
        adapter.observe=replaced
        with self.assertRaisesRegex(RuntimeErrorCode,'PROCESS_CHANGED'):await self.recover(r,adapter)
        adapter.observe=original
        adapter.data=protocol().replace(b'fixture-session',b'changed-session')
        with self.assertRaisesRegex(RuntimeErrorCode,'EXIT_RECEIPT_CONFLICT'):await self.recover(r,adapter)
        self.assertEqual((await self.store.get(r['id']))['state'],'reconciliation_required')

    async def test_incomplete_or_nonzero_exit_retains_evidence_and_fails(self):
        for n,adapter in enumerate([FakeAdapter(protocol(complete=False)),FakeAdapter(exit_code=7)]):
            r=await self.submit(str(n))
            with patch.object(self.store,'finish_failed',side_effect=ConnectionError()):
                with self.assertRaises(ConnectionError):await self.worker(adapter).once()
            result=await self.recover(r,adapter)
            self.assertEqual(result['state'],'failed')
            self.assertIn(result['attempts'][0]['error']['code'],{'RESULT_INCOMPLETE','PROCESS_EXITED'})

    async def test_revocation_and_cancellation_cannot_accept_result(self):
        r,adapter=await self.interrupted()
        await self.store.revoke(r['id'],r['version'])
        result=await self.recover(r,adapter)
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['attempts'][0]['error']['code'],'AUTH_REVOKED')

    async def test_cancel_after_worker_loss_is_confirmed_only_after_exit(self):
        r,adapter=await self.interrupted()
        await self.store.cancel(r['id'],r['version'])
        self.assertEqual((await self.recover(r,adapter))['state'],'cancelled')

    async def test_authorization_hook_uses_original_binding_and_cas_catches_revoke(self):
        r,adapter=await self.interrupted()
        contexts=[]
        async def verifier(context,action):
            contexts.append((context,action))
            current=await self.store.get(r['id'])
            await self.store.revoke(r['id'],current['version'])
            return True
        with self.assertRaisesRegex(RuntimeErrorCode,'VERSION_CONFLICT'):
            await self.recover(r,adapter,verifier=verifier)
        context,action=contexts[0]
        self.assertEqual(action,'accept_result');self.assertEqual(context.attempt_id,r['attempt_id'])
        self.assertEqual(context.lease_epoch,r['attempts'][0]['epoch'])
        self.assertEqual(context.authorization_epoch,r['attempts'][0]['authorization_epoch'])
        self.assertEqual(context.session_id,'fixture-session')
        self.assertEqual((await self.recover(r,adapter))['state'],'failed')

    async def test_governed_work_requires_verifier_before_launch(self):
        await self.store.submit('governed',{'runtime_profile':'test-profile',
            'profile_digest':self.spec.fingerprint(),'work_authorization_ref':'grant-ref'})
        adapter=FakeAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(adapter.started,0)
        self.assertEqual(result['attempts'][0]['error']['code'],'WORK_AUTHORIZATION_VERIFIER_REQUIRED')

    async def test_governed_recovery_requires_acceptance_verifier(self):
        r=await self.store.submit('governed',{'runtime_profile':'test-profile',
            'profile_digest':self.spec.fingerprint(),'work_authorization_ref':'grant-ref'})
        async def allow(context,action):return True
        adapter=FakeAdapter()
        worker=Worker(self.store,adapter,'test-profile',self.spec,self.provider,authorization_verifier=allow)
        with patch.object(self.store,'complete',side_effect=ConnectionError()):
            with self.assertRaises(ConnectionError):await worker.once()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_AUTHORIZATION_VERIFIER_REQUIRED'):
            await self.recover(r,adapter)
        self.assertEqual((await self.recover(r,adapter,verifier=allow))['state'],'succeeded')

    async def test_parallel_reconcilers_commit_once(self):
        r,adapter=await self.interrupted()
        results=await asyncio.gather(self.recover(r,adapter),self.recover(r,adapter))
        self.assertTrue(all(x['state']=='succeeded' for x in results))
        events=await self.store.pending_events('audit')
        self.assertEqual(sum(e['type']=='attempt.exit_reconciled' for e in events),1)

    async def test_worker_phase_delivery_outage_replays_with_original_time(self):
        await self.submit();adapter=FakeAdapter();worker=self.worker(adapter)
        original=self.store.report;failed=False;captured=[]
        async def report(*args,**kwargs):
            nonlocal failed
            if kwargs.get('phase')=='waiting_model' and not failed:
                failed=True;raise ConnectionError('temporary outage')
            if kwargs.get('output'):captured.append(kwargs['output_observed_at'])
            return await original(*args,**kwargs)
        # Advance only local spool time to exercise retry without slowing the model.
        now=worker.spool.clock()
        worker.spool.clock=lambda:now
        original_logs=adapter.logs
        calls=0
        async def logs(handle,limit):
            nonlocal now,calls
            calls+=1
            if calls>1:now+=2
            return await original_logs(handle,limit)
        adapter.logs=logs
        # Keep the process live for two polls so delivery recovers before completion.
        original_observe=adapter.observe;polls=0
        async def observe(handle):
            nonlocal polls,now
            polls+=1
            if polls<3:return adapter.observation(handle)
            now+=2
            return await original_observe(handle)
        adapter.observe=observe
        with patch.object(self.store,'report',side_effect=report):result=await worker.once()
        self.assertTrue(failed);self.assertEqual(result['state'],'succeeded')
        self.assertEqual(worker.spool.status()['retained'],0)
        self.assertEqual(result['attempts'][0]['last_output_at'],captured[-1])

    async def test_stale_spool_never_renews_lease_and_result_recovers_separately(self):
        await self.submit();adapter=FakeAdapter();worker=self.worker(adapter)
        original=self.store.report
        async def disconnected(*args,**kwargs):
            if kwargs.get('phase') in {'waiting_model','streaming','validating'}:raise ConnectionError()
            return await original(*args,**kwargs)
        with patch.object(self.store,'report',side_effect=disconnected):
            with self.assertRaisesRegex(RuntimeErrorCode,'REPORT_DELIVERY_PENDING'):await worker.once()
        status=await self.store.runtime_status();r=await self.store.get(status['attempts'][0]['run_id'])
        self.assertGreater(worker.spool.status()['retained'],0)
        worker.spool.clock=lambda:10**12
        await relay_reports(self.store,worker.spool)
        self.assertEqual(worker.spool.status()['errors']['STALE_OWNER'],1)
        self.assertEqual((await self.recover(r,adapter))['state'],'succeeded')
        self.assertGreater(worker.spool.status()['retained'],0)

    async def test_bounded_recovery_visits_later_unknown_runs(self):
        run_ids=set()
        adapter=FakeAdapter(failure=True)
        for i in range(3):
            r=await self.submit(str(i));run_ids.add(r['id'])
            with self.assertRaises(TimeoutError):await self.worker(adapter).once()
        async def alive(handle):return adapter.observation(handle)
        adapter.observe=alive
        worker=self.worker(adapter)
        visited=set()
        for _ in range(3):
            batch=await worker.recover(limit=1)
            self.assertEqual(len(batch),1);visited.add(batch[0]['run_id'])
        self.assertEqual(visited,run_ids)
        self.assertFalse(adapter.stopped)

    async def test_prelaunch_offline_report_never_starts_workload(self):
        r=await self.submit();adapter=FakeAdapter();worker=self.worker(adapter)
        with patch.object(self.store,'report',side_effect=ConnectionError()):
            with self.assertRaisesRegex(RuntimeErrorCode,'REPORT_DELIVERY_PENDING'):await worker.once()
        self.assertEqual(adapter.started,0)
        self.assertEqual(worker.spool.status()['retained'],2)
        self.assertEqual((await self.store.get(r['id']))['state'],'reconciliation_required')


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL not enabled')
class PostgresRecoveryTests(RecoveryTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.db.close()
        self.url=os.environ['DIOS_TEST_POSTGRES_URL']
        self.db=Database(self.url);await self.db.migrate()
        self.store=RuntimeStore(self.db,self.scope)


@unittest.skipUnless(os.environ.get('DIOS_TEST_DOCKER')=='1','isolated Docker not enabled')
class DockerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    # Reuse setup/helpers without inheriting the other test cases.
    async def asyncSetUp(self):
        await fixtures.WorkerTests.asyncSetUp(self)
        from app.runtime.worker import read_profile
        from app.runtime.docker_adapter import DockerRuntimeAdapter
        from test_runtime_worker import ROOT
        _,self.spec=read_profile(ROOT/'examples/runtime/fixture-profile.json')
        self.adapter=DockerRuntimeAdapter(self.scope)
        self.children=[]
    async def asyncTearDown(self):
        for child in self.children:
            if child.returncode is None:child.kill()
            await child.communicate()
        containers=await asyncio.to_thread(self.adapter.client.containers.list,all=True,
            filters={'label':'dios.runtime.scope='+self.adapter.scope})
        for container in containers:await asyncio.to_thread(container.remove,force=True)
        self.adapter.client.close()
        await fixtures.WorkerTests.asyncTearDown(self)
    submit=fixtures.WorkerTests.submit
    child=fixtures.DockerWorkerTests.child
    wait_state=fixtures.DockerWorkerTests.wait_state

    async def test_killed_worker_recovers_original_container_result_after_restart(self):
        r=await self.submit(delay=3)
        child=await self.child()
        status=await self.wait_state(lambda s:s['actual_concurrency']==1)
        identity=status['attempts'][0]
        child.kill();await child.communicate()
        async with self.db.transaction(self.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==identity['attempt_id']).values(lease_until=0))
        live=await reconcile_run(self.store,self.adapter,self.provider,r['id'])
        self.assertEqual(live['state'],'reconciliation_required')
        for _ in range(100):
            if not (await self.adapter.observe(identity['attempt_id'])).alive:break
            await asyncio.sleep(.1)
        restarted=await self.child()
        _,error=await asyncio.wait_for(restarted.communicate(),20)
        self.assertEqual(restarted.returncode,0,error)
        result=await self.store.get(r['id'])
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(result['attempts']),1)
        self.assertEqual(result['attempts'][0]['session_id'],identity['session_id'])
        self.assertEqual(result['attempts'][0]['process_instance'],identity['process_instance'])
