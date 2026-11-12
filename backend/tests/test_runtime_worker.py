import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from uuid import uuid4
from sqlalchemy import select
from app.runtime.adapter import LaunchSpec, Observation, LogSnapshot, Mount
from app.runtime.artifacts import LocalArtifacts
from app.runtime.bridge import JsonlBridge
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.lifecycle import Lifecycle
from app.runtime.schema import artifacts, attempts
from app.runtime.store import RuntimeStore
from app.runtime.worker import Worker, read_profile

ROOT = Path(__file__).resolve().parents[2]
OBSERVED_CONCURRENCY = []


def protocol(*, complete=True, evidence=True):
    lines = [{'type':'session','session_id':'fixture-session'}, {'type':'output','text':'中文🙂'}]
    if complete:
        lines.append({'type':'result','session_id':'fixture-session','result':{
            'summary':'合成结果','evidence':['fixture:source'] if evidence else []}})
    return b''.join((json.dumps(line,ensure_ascii=False)+'\n').encode() for line in lines)


class FakeAdapter:
    name = 'test-lifecycle'
    def __init__(self, data=None, exit_code=0, failure=False):
        self.data=protocol() if data is None else data
        self.exit_code, self.failure=exit_code, failure
        self.running, self.started, self.removed, self.stopped = False,0,False,False
    def observation(self, handle):
        return Observation(handle,'running' if self.running else 'exited',self.running,
            123 if self.running else None,'instance-1',None if self.running else self.exit_code,'not_configured','fixture-host')
    async def start(self,handle,spec):
        self.started+=1;self.running=True
        return self.observation(handle)
    async def observe(self,handle):
        if self.failure: raise TimeoutError('observer unavailable')
        self.running=False
        return self.observation(handle)
    async def logs(self,handle,max_bytes): return LogSnapshot(self.data,b'raw error detail')
    async def stop(self,handle,grace_seconds=5):
        self.running=False;self.stopped=True;return self.observation(handle)
    async def remove(self,handle): self.removed=True


class BridgeTests(unittest.TestCase):
    def test_utf8_cumulative_bytes_never_decode_partial_characters(self):
        data=protocol();b=JsonlBridge()
        for n in range(1,len(data)+1):b.feed(data[:n])
        b.feed(data,final=True)
        self.assertEqual(b.output_count,1);self.assertEqual(b.result['summary'],'合成结果')
    def test_session_switch_truncation_and_missing_evidence_rejected(self):
        for data,code in [(protocol(complete=False),'RESULT_INCOMPLETE'),
            (protocol(evidence=False),'EVIDENCE_INSUFFICIENT'),
            (protocol().replace(b'"type": "result", "session_id": "fixture-session"',b'"type": "result", "session_id": "other"'),'SESSION_CHANGED')]:
            with self.assertRaisesRegex(RuntimeErrorCode,code):JsonlBridge().feed(data,final=True)
    def test_rotated_output_rejected_instead_of_replaying_events(self):
        b=JsonlBridge();b.feed(protocol())
        with self.assertRaisesRegex(RuntimeErrorCode,'OUTPUT_CURSOR_LOST'):b.feed(b'{}\n')


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.url='sqlite+aiosqlite:///'+str(self.root/'runtime.sqlite')
        self.db=Database(self.url);await self.db.migrate()
        self.scope='worker-test-'+str(uuid4());self.store=RuntimeStore(self.db,self.scope)
        self.provider=LocalArtifacts(self.root/'artifacts')
        self.spec=LaunchSpec('test-image',('test-command',))
    async def asyncTearDown(self):await self.db.close();self.temp.cleanup()
    async def submit(self,key='one',**payload):
        return await self.store.submit(key,{'runtime_profile':'test-profile',
            'profile_digest':self.spec.fingerprint(),'payload':payload})
    def worker(self,adapter):return Worker(self.store,adapter,'test-profile',self.spec,self.provider,poll_interval=.05)
    async def test_success_links_real_observations_raw_and_validated_artifacts(self):
        r=await self.submit();adapter=FakeAdapter();result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'succeeded');self.assertTrue(adapter.removed)
        a=result['attempts'][0]
        self.assertEqual(a['session_id'],'fixture-session');self.assertEqual(a['process_id'],123)
        self.assertEqual(a['remote']['runtime']['alive'],False)
        async with self.db.read(self.scope) as c:
            rows=(await c.execute(select(artifacts).where(artifacts.c.run_id==r['id']))).mappings().all()
        self.assertEqual(len(rows),3)
        self.assertTrue(all(self.provider.verify(x['storage_key']) for x in rows))
    async def test_incomplete_and_insufficient_never_succeed_and_keep_raw_output(self):
        for n,data in enumerate([protocol(complete=False),protocol(evidence=False)]):
            await self.submit(str(n));a=FakeAdapter(data);r=await self.worker(a).once()
            self.assertEqual(r['state'],'failed');self.assertTrue(a.removed)
            self.assertIn(r['attempts'][0]['error']['code'],{'RESULT_INCOMPLETE','EVIDENCE_INSUFFICIENT'})
    async def test_exit_failure_not_overridden_by_valid_result(self):
        await self.submit();r=await self.worker(FakeAdapter(exit_code=7)).once()
        self.assertEqual(r['state'],'failed');self.assertEqual(r['attempts'][0]['error']['code'],'PROCESS_EXITED')
    async def test_observer_timeout_keeps_process_and_fences_without_requeue(self):
        r=await self.submit();a=FakeAdapter(failure=True)
        with self.assertRaises(TimeoutError):await self.worker(a).once()
        saved=await self.store.get(r['id'])
        self.assertEqual(saved['state'],'reconciliation_required');self.assertTrue(a.running)
        self.assertFalse(a.stopped);self.assertFalse(a.removed)
        self.assertEqual((await self.worker(FakeAdapter()).once())['state'],'idle')
        status=await self.store.runtime_status()
        self.assertIsNone(status['actual_concurrency']);self.assertEqual(status['unknown_attempts'],1)
    async def test_profile_mismatch_cannot_launch_and_other_profiles_not_claimed(self):
        await self.store.submit('other',{'runtime_profile':'other'})
        r=await self.store.submit('mismatch',{'runtime_profile':'test-profile','profile_digest':'changed'})
        a=FakeAdapter();result=await self.worker(a).once()
        self.assertEqual(result['id'],r['id']);self.assertEqual(result['state'],'failed');self.assertEqual(a.started,0)
        self.assertEqual((await self.worker(a).once())['state'],'idle')
    async def test_scope_drain_blocks_new_launch(self):
        await self.submit();await Lifecycle(self.db,self.scope).begin_drain()
        a=FakeAdapter();self.assertEqual((await self.worker(a).once())['state'],'idle');self.assertEqual(a.started,0)
    async def test_failed_requires_confirmed_exit_and_stale_owner_cannot_observe(self):
        await self.submit();w=self.worker(FakeAdapter())
        await self.store.register_worker(w.worker_id,w.instance,'host',os.getpid())
        a=await self.store.claim(w.worker_id,w.instance);binding=(a['run_id'],a['id'],w.worker_id,w.instance,a['epoch'])
        with self.assertRaisesRegex(RuntimeErrorCode,'PROCESS_EXIT_NOT_CONFIRMED'):await self.store.finish_failed(binding,'TEST')
        async with self.db.transaction(self.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(lease_until=0))
        await self.store.expire()
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):
            await self.store.observe_process(binding,FakeAdapter().observation(a['id']),adapter='test',boot_id='boot')


@unittest.skipUnless(os.environ.get('DIOS_TEST_DOCKER')=='1','explicit isolated Docker regression not enabled')
class DockerWorkerTests(WorkerTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _, self.spec=read_profile(ROOT/'examples/runtime/fixture-profile.json')
        self.adapter=DockerRuntimeAdapter(self.scope)
        self.handles=[];self.children=[]
    async def asyncTearDown(self):
        for p in self.children:
            if p.returncode is None:p.kill()
            await p.communicate()
        # Only objects labelled with this disposable test scope are cleaned up.
        containers=await asyncio.to_thread(self.adapter.client.containers.list,all=True,
            filters={'label':'dios.runtime.scope='+self.adapter.scope})
        for c in containers:await asyncio.to_thread(c.remove,force=True)
        self.adapter.client.close()
        await super().asyncTearDown()
    async def child(self,once=True):
        profile=self.root/'profile.json'
        value=json.loads((ROOT/'examples/runtime/fixture-profile.json').read_text());value['id']='test-profile'
        profile.write_text(json.dumps(value))
        args=[sys.executable,'-m','app.runtime.cli','--database-url',self.url,'--scope',self.scope,
            'worker','--profile',str(profile),'--artifacts',str(self.provider.root),'--poll-interval','.1']
        if once:args.append('--once')
        p=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        self.children.append(p);return p
    async def wait_state(self,predicate,timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            status=await self.store.runtime_status()
            if predicate(status):return status
            await asyncio.sleep(.1)
        self.fail('runtime observation deadline exceeded')
    async def test_docker_three_independent_workers_sessions_and_natural_drain(self):
        runs=[await self.submit(str(n),delay=4) for n in range(3)]
        children=[await self.child() for _ in range(3)]
        status=await self.wait_state(lambda s:s['actual_concurrency']==3)
        self.assertEqual(len({w['pid'] for w in status['workers']}),3)
        self.assertEqual(len({a['process_id'] for a in status['attempts']}),3)
        self.assertEqual(len({a['session_id'] for a in status['attempts']}),3)
        self.assertEqual(status['registered_slots'],3)
        OBSERVED_CONCURRENCY.append({
            'configured_slots':status['registered_slots'],'actual_concurrency':status['actual_concurrency'],
            'workers':[{k:w[k] for k in ('id','pid','instance')} for w in status['workers']],
            'attempts':[{k:a[k] for k in ('run_id','attempt_id','worker_id','session_id','process_id','process_instance','freshness')}
                        for a in status['attempts']]})
        await self.submit('queued-after-active',delay=.1)
        await Lifecycle(self.db,self.scope).begin_drain()
        extra=await self.child();stdout,stderr=await asyncio.wait_for(extra.communicate(),20)
        self.assertEqual(extra.returncode,0,stderr);self.assertEqual(json.loads(stdout)['state'],'idle')
        for p in children:
            _,err=await asyncio.wait_for(p.communicate(),20);self.assertEqual(p.returncode,0,err)
        for r in runs:self.assertEqual((await self.store.get(r['id']))['state'],'succeeded')
        final=await Lifecycle(self.db,self.scope).wait_drained(3)
        self.assertTrue(final['ready_for_cutover']);self.assertEqual(final['queued_runs'],1)
        self.assertEqual((await self.store.runtime_status())['actual_concurrency'],0)
    async def test_docker_cancel_confirms_exit_and_retains_logs(self):
        r=await self.submit(delay=30);p=await self.child()
        await self.wait_state(lambda s:s['actual_concurrency']==1)
        current=await self.store.get(r['id']);await self.store.cancel(r['id'],current['version'])
        _,err=await asyncio.wait_for(p.communicate(),20);self.assertEqual(p.returncode,0,err)
        final=await self.store.get(r['id']);self.assertEqual(final['state'],'cancelled')
        self.assertEqual(final['attempts'][0]['remote']['runtime']['alive'],False)
        self.assertEqual((await self.adapter.observe(final['attempt_id'])).state,'absent')
    async def test_docker_crash_incomplete_and_evidence_failure(self):
        for mode,code in [('crash','PROCESS_EXITED'),('incomplete','RESULT_INCOMPLETE'),('insufficient','EVIDENCE_INSUFFICIENT')]:
            await self.submit(mode,mode=mode)
            result=await self.worker(self.adapter).once()
            self.assertEqual(result['state'],'failed')
            self.assertEqual(result['attempts'][0]['error']['code'],code)
    async def test_docker_worker_loss_retains_live_container_and_never_double_dispatches(self):
        r=await self.submit(delay=30);p=await self.child()
        status=await self.wait_state(lambda s:s['actual_concurrency']==1)
        handle=status['attempts'][0]['attempt_id']
        p.kill();await p.communicate()
        self.assertTrue((await self.adapter.observe(handle)).alive)
        other=await self.child();out,err=await asyncio.wait_for(other.communicate(),20)
        self.assertEqual(other.returncode,0,err);self.assertEqual(json.loads(out)['state'],'idle')
        async with self.db.transaction(self.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==handle).values(lease_until=0))
        await self.store.expire()
        saved=await self.store.get(r['id'])
        self.assertEqual(saved['state'],'reconciliation_required');self.assertEqual(len(saved['attempts']),1)
        self.assertIsNone((await self.store.runtime_status())['actual_concurrency'])
    async def test_docker_sigterm_drains_worker_and_keeps_next_run_queued(self):
        r=await self.submit('active',delay=2);await self.submit('pending')
        p=await self.child(once=False);await self.wait_state(lambda s:s['actual_concurrency']==1)
        p.send_signal(signal.SIGTERM)
        _,err=await asyncio.wait_for(p.communicate(),20);self.assertEqual(p.returncode,0,err)
        self.assertEqual((await self.store.get(r['id']))['state'],'succeeded')
        self.assertEqual((await Lifecycle(self.db,self.scope).status())['queued_runs'],1)
    async def test_docker_service_identity_resources_logs_stop_cleanup_and_scope(self):
        handle=str(uuid4());spec=replace(self.spec,kind='service',command=('python','-u','-c',
            "import time;print('service ready',flush=True);time.sleep(30)"))
        first=await self.adapter.start(handle,spec);second=await self.adapter.start(handle,spec)
        self.assertEqual(first.process_instance,second.process_instance);self.assertTrue(first.alive)
        c=await asyncio.to_thread(self.adapter.client.containers.get,'dios-worker-'+handle)
        self.assertEqual(c.attrs['HostConfig']['Memory'],spec.memory_bytes)
        self.assertEqual(c.attrs['HostConfig']['PidsLimit'],spec.pids_limit)
        self.assertTrue(c.attrs['HostConfig']['ReadonlyRootfs'])
        with self.assertRaisesRegex(RuntimeErrorCode,'RUNTIME_SPEC_CONFLICT'):
            await self.adapter.start(handle,replace(spec,cpu_count=2))
        with self.assertRaisesRegex(RuntimeErrorCode,'RUNTIME_SCOPE_MISMATCH'):
            await DockerRuntimeAdapter('other',self.adapter.client).observe(handle)
        with self.assertRaisesRegex(RuntimeErrorCode,'RUNTIME_STILL_RUNNING'):await self.adapter.remove(handle)
        for _ in range(30):
            logs=await self.adapter.logs(handle,1024)
            if logs.stdout:break
            await asyncio.sleep(.1)
        self.assertIn(b'service ready',logs.stdout)
        self.assertFalse((await self.adapter.stop(handle,0)).alive)
        self.assertFalse((await self.adapter.start(handle,spec)).alive)
        await self.adapter.remove(handle);self.assertEqual((await self.adapter.observe(handle)).state,'absent')


if __name__=='__main__':unittest.main()
