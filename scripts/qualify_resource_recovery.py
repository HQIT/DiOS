"""Native shared-budget recovery; --local is preparation, not physical qualification."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'backend'),str(ROOT/'backend/tests')]
from qualify_physical_worker import PhysicalWorkers, remote_command
from qualify_physical_faults import PhysicalFaults
from test_remote_recovery_qualification import RemoteRecoveryQualificationTests
from test_runtime_worker import WorkerTests
from app.runtime.capacity import Capacity
from app.runtime.consumption import Consumption
from app.runtime.database import RuntimeErrorCode
from app.runtime.objects import Objects
from app.runtime.remote_tool_channel import RemoteToolProvider
from app.runtime.remote_runtime import RemoteRuntimeStore


class ResourceScenarios:
    observations=[]
    physical=False

    def configure_launch(self,profile,value):
        # A synthetic usage frame tests identity/correlation, not provider billing.
        script="""import json,os,time,uuid
from pathlib import Path
sid=str(uuid.uuid4())
def emit(v):print(json.dumps(v),flush=True)
emit({'type':'session','session_id':sid})
emit({'type':'output','text':'synthetic resource qualification'})
emit({'type':'usage','session_id':sid,'sequence':1,'kind':'acp_prompt','quality':'reported','values':{'inputTokens':1,'outputTokens':0,'totalTokens':1}})
end=time.monotonic()+180
while not Path('/run/fixture/release').exists():
 if time.monotonic()>end:raise RuntimeError('barrier timeout')
 time.sleep(.1)
emit({'type':'result','session_id':sid,'result':{'summary':'synthetic resource result','evidence':['fixture:'+os.environ['DIOS_ATTEMPT_ID']]}})
"""
        value['launch']['command']=['python','-u','-c',script]

    async def launch_node(self,node):
        if self.physical and node=='worker':return await PhysicalWorkers.launch(self)
        return await RemoteRecoveryQualificationTests.launch(self)

    async def test_budget_survives_worker_loss_and_single_settlement(self):
        capacity=Capacity(self.store);money=Consumption(self.fixture.db,self.store.scope)
        await capacity.put('shared',{'kind':'scope'},2,0)
        await money.put('tokens',{'kind':'scope'},'tokens',4,2,0,key='create',actor='qualification',enforcement='reservation_only')
        runs=[await self.submit('resource-'+str(i)) for i in range(3)]
        self.assertEqual((await self.submit('resource-0'))['id'],runs[0]['id'])
        owner=await self.launch_node('controller')
        # Establish the owner that will be killed; the second competes for remaining capacity.
        one=await self.concurrency(1)
        other=await self.launch_node('worker')
        running=await self.concurrency(2)
        blocked=await self.collect(await self.launch_node('worker'))
        self.assertEqual(blocked['state'],'idle')
        state=await self.store.get(one['attempts'][0]['run_id']);first=state['attempts'][0]
        queued=[r for r in runs if (await self.store.get(r['id']))['state']=='queued']
        self.assertEqual(len(queued),1)
        self.assertEqual((await money.status())['items'][0]['reserved'],4)
        self.assertEqual((await capacity.status())['items'][0]['reserved_attempts'],2)
        owner.kill();await asyncio.wait_for(owner.communicate(),10)
        self.assertNotEqual(owner.returncode,0)
        first=(await self.store.get(state['id']))['attempts'][0]
        transport=RemoteToolProvider(self.client_file,SimpleNamespace(scope=self.store.scope),'account-a',self.profiles['account-a'][1])
        transport.worker_id=first['worker_id']
        stale=RemoteRuntimeStore(self.store.scope,transport);stale.instance=first['worker_instance']
        binding=[state['id'],first['id'],first['worker_id'],first['worker_instance'],first['epoch']]
        stale.bindings[state['id']]=binding
        # Preserve a credential that was valid before lease expiry.
        await stale.get(state['id'])
        proof=await Objects(self.fixture.db,self.store.scope).publish(self.fixture.provider,
            [json.dumps({'source':'synthetic usage frame','attempt_id':first['id'],'session_id':first['session_id'],'totalTokens':1}).encode()],
            'usage-proof','qualification')
        evidence={'ref':proof['ref'],'digest':proof['descriptor']['digest'],'basis':'operator_reconciliation'}
        with self.assertRaisesRegex(RuntimeErrorCode,'CONSUMPTION_EXECUTION_UNRESOLVED'):
            await money.settle('tokens',first['id'],1,evidence,key='early',actor='qualification')
        (self.control/'release').touch()
        finished_other=await self.collect(other)
        self.assertEqual(finished_other['state'],'succeeded')
        async with asyncio.timeout(15):
            while (await self.adapter.observe(first['id'])).alive:await asyncio.sleep(.1)
        self.assertEqual((await money.status())['items'][0]['reserved'],4)
        self.assertEqual((await self.collect(await self.launch_node('worker')))['state'],'idle')
        # Natural DB lease expiry; no timestamp patch or early ownership release.
        async with asyncio.timeout(70):
            while True:
                async with self.fixture.db.read(self.store.scope) as c:now=await self.fixture.db.now(c)
                if now>first['lease_until']:break
                await asyncio.sleep(min(1,first['lease_until']-now+.05))
        await self.store.expire()
        unknown=await self.store.get(state['id'])
        self.assertEqual(unknown['state'],'reconciliation_required')
        unknown_capacity=(await capacity.status())['items'][0]
        self.assertEqual(unknown_capacity['reserved_attempts'],1)
        self.assertEqual((await money.status())['items'][0]['reserved'],4)
        with self.assertRaises(RuntimeErrorCode) as denied:
            await stale.heartbeat(*binding)
        self.assertIn(denied.exception.code,{'STALE_OWNER','WORKER_CREDENTIAL_EXPIRED','AUTH_REVOKED'})
        await self.collect(await self.launch_node('controller'))
        restored=await self.store.get(state['id'])
        self.assertEqual(restored['state'],'succeeded')
        self.assertEqual(len(restored['attempts']),1)
        self.assertEqual(restored['attempts'][0]['id'],first['id'])
        self.assertEqual(restored['attempts'][0]['session_id'],first['session_id'])
        self.assertEqual((await capacity.status())['items'][0]['reserved_attempts'],0)
        usage=restored['model_usage']['items']
        self.assertEqual(len(usage),1);self.assertEqual(usage[0]['values']['totalTokens'],1)
        self.assertEqual(usage[0]['attempt_id'],first['id']);self.assertEqual(usage[0]['session_id'],first['session_id'])
        # Replay the same settlement concurrently; a different receipt cannot settle twice.
        receipts=await asyncio.gather(*(money.settle('tokens',first['id'],1,evidence,key='settle',actor='qualification') for _ in range(4)))
        self.assertTrue(all(r==receipts[0] for r in receipts))
        with self.assertRaisesRegex(RuntimeErrorCode,'CONSUMPTION_ALREADY_SETTLED'):
            await money.settle('tokens',first['id'],1,evidence,key='duplicate',actor='qualification')
        balance=(await money.status())['items'][0]
        self.assertEqual((balance['reserved'],balance['settled'],balance['remaining']),(2,1,1))
        self.assertEqual((await self.collect(await self.launch_node('worker')))['state'],'idle')
        self.assertEqual((await self.store.get(queued[0]['id']))['attempts'],[])
        settled_run=await self.store.get(state['id'])
        self.assertEqual(settled_run['consumption_budget']['items'][0]['actual'],1)
        self.observations.append({'provider':self.fixture.db.engine.dialect.name,'scope':self.store.scope,
            'real_concurrency':running,'killed_worker_pid':owner.pid,'original_attempt':first['id'],
            'original_session':first['session_id'],'lease_until':first['lease_until'],'expired_at':now,
            'unknown_capacity':unknown_capacity,'stale_owner_error':denied.exception.code,
            'restored_state':restored['state'],'attempt_count':len(restored['attempts']),
            'usage':usage,'settlement':receipts[0],'balance':balance,'queued_run':queued[0]['id'],
            'workload_image':self.image_id,'additional_launch_for_restored_run':0})


class LocalResources(ResourceScenarios,RemoteRecoveryQualificationTests):pass
class PhysicalResources(ResourceScenarios,PhysicalWorkers):physical=True


class PartitionResources(ResourceScenarios, PhysicalFaults):
    physical = True

    def configure_launch(self, profile, value):
        super().configure_launch(profile, value)
        value['recovery']['max_attempts'] = 1

    async def wait_terminal(self, run_id, process):
        async with asyncio.timeout(100):
            while True:
                await self.store.expire()
                value = await self.store.get(run_id)
                if value['state'] in {'succeeded', 'failed', 'cancelled'}:
                    return value
                if process.returncode is not None:
                    out, err = await process.communicate()
                    self.fail('persistent Worker exited: '+(out+err).decode()[-3000:])
                await asyncio.sleep(.5)

    async def test_partition_preserves_budget_and_revoked_work_cannot_resume(self):
        capacity = Capacity(self.store)
        money = Consumption(self.fixture.db, self.store.scope)
        await capacity.put('shared', {'kind':'scope'}, 1, 0)
        await money.put('tokens', {'kind':'scope'}, 'tokens', 3, 2, 0,
            key='create', actor='qualification', enforcement='reservation_only')
        first_run = await self.submit('partition-first')
        revoked_run = await self.submit('partition-revoked')
        process = await PhysicalWorkers.launch(self, once=False)
        running = await self.concurrency(1)
        first = (await self.store.get(first_run['id']))['attempts'][0]
        container = await self.workload()
        transport = RemoteToolProvider(self.client_file, SimpleNamespace(scope=self.store.scope),
            'account-a', self.profiles['account-a'][1])
        transport.worker_id = first['worker_id']
        stale = RemoteRuntimeStore(self.store.scope, transport)
        stale.instance = first['worker_instance']
        binding = [first_run['id'], first['id'], first['worker_id'], first['worker_instance'], first['epoch']]
        stale.bindings[first_run['id']] = binding
        await stale.get(first_run['id'])
        await self.api('POST', '/proxies/control', {'enabled':False})
        async with asyncio.timeout(35):
            while (await self.inspect(container))['State']['Running']:
                await asyncio.sleep(.25)
        blocked = await self.collect(await RemoteRecoveryQualificationTests.launch(self))
        self.assertEqual(blocked['state'], 'idle')
        self.assertEqual((await money.status())['items'][0]['reserved'], 2)
        self.assertEqual((await self.store.get(revoked_run['id']))['attempts'], [])
        # Revoke queued work while the remote Worker cannot observe the control plane.
        current = await self.store.get(revoked_run['id'])
        await self.store.revoke(current['id'], current['version'])
        (self.control/'release').touch()
        await self.api('POST', '/proxies/control', {'enabled':True})
        restored = await self.wait_terminal(first_run['id'], process)
        self.assertEqual(restored['state'], 'failed')
        self.assertEqual(len(restored['attempts']), 1)
        self.assertEqual(restored['attempts'][0]['id'], first['id'])
        self.assertIsNone(process.returncode)
        self.assertEqual((await money.status())['items'][0]['reserved'], 2)
        with self.assertRaises(RuntimeErrorCode) as denied:
            await stale.heartbeat(*binding)
        self.assertIn(denied.exception.code, {'STALE_OWNER','WORKER_CREDENTIAL_EXPIRED','AUTH_REVOKED'})
        proof = await Objects(self.fixture.db, self.store.scope).publish(self.fixture.provider,
            [json.dumps({'basis':'synthetic partition reconciliation','attempt':first['id']}).encode()],
            'usage-proof','qualification')
        evidence = {'ref':proof['ref'],'digest':proof['descriptor']['digest'],'basis':'operator_reconciliation'}
        await money.settle('tokens', first['id'], 1, evidence, key='settle', actor='qualification')
        third = await self.submit('partition-after-recovery')
        finished = await self.wait_terminal(third['id'], process)
        self.assertEqual(finished['state'], 'succeeded')
        self.assertEqual(len(finished['attempts']), 1)
        revoked = await self.store.get(revoked_run['id'])
        self.assertTrue(revoked['revoked'])
        self.assertEqual(revoked['attempts'], [])
        balance = (await money.status())['items'][0]
        self.assertEqual((balance['reserved'],balance['settled'],balance['remaining']), (2,1,0))
        self.observations.append({'case':'partition-budget-and-E2AG-revocation',
            'provider':self.fixture.db.engine.dialect.name, 'scope':self.store.scope,
            'initial_concurrency':running, 'worker_restarted':False,
            'original_attempt':first['id'], 'original_state':restored['state'],
            'first_attempt_count':len(restored['attempts']), 'stale_owner_error':denied.exception.code,
            'revoked_run':revoked_run['id'], 'revoked_attempts':len(revoked['attempts']),
            'next_run':third['id'], 'next_state':finished['state'], 'balance':balance})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--local',action='store_true');p.add_argument('--postgres',action='store_true')
    for name in ('ssh-config','host','remote-source','runtime-image'):p.add_argument('--'+name)
    p.add_argument('--toxiproxy', type=Path, help='Also qualify controlled network loss and E2AG revocation')
    p.add_argument('--remote-python', help='Native Linux manager Python; workloads remain in Docker')
    p.add_argument('--remote-docker-host', help='Existing remote Docker endpoint, including a rootless socket')
    p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    if not args.local and (not all(getattr(args,n) for n in ('ssh_config','host','remote_source')) or not (args.remote_python or args.runtime_image)):p.error('physical SSH/source/image configuration required')
    output=args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT/'workspace'):p.error('new workspace output required')
    output.mkdir(parents=True,mode=0o700)
    cls=LocalResources if args.local else PhysicalResources
    if args.toxiproxy and args.local:p.error('network qualification requires two physical hosts')
    cls.options=args;cls.__unittest_skip__=False
    PartitionResources.options=args;PartitionResources.__unittest_skip__=False
    if args.postgres:
        url=os.environ['DIOS_TEST_POSTGRES_URL']
        cls.fixture_type=type('PostgresFixture',(WorkerTests,),{'database_url':url})
        PartitionResources.fixture_type=cls.fixture_type
    boots={'controller':hashlib.sha256(Path('/proc/sys/kernel/random/boot_id').read_bytes()).hexdigest()}
    if not args.local:
        raw=subprocess.check_output(['ssh','-F',args.ssh_config,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',args.host,'cat /proc/sys/kernel/random/boot_id'],timeout=20)
        boots['worker']=hashlib.sha256(raw).hexdigest()
        if boots['controller']==boots['worker']:raise ValueError('distinct physical host required')
        # Validate the actual mounted implementation, not merely the selected image label.
        source_root=ROOT/'backend'
        manifest={str(f.relative_to(source_root)):hashlib.sha256(f.read_bytes()).hexdigest() for f in (source_root/'app').rglob('*.py')}
        code="import json,hashlib,sys;from pathlib import Path;r=Path(sys.argv[1]);print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (r/'app').rglob('*.py')}))"
        ssh=['ssh','-F',args.ssh_config,'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',args.host]
        observed=json.loads(subprocess.check_output(ssh+[shlex.join(['python3','-c',code,args.remote_source])],timeout=20))
        if observed!=manifest:raise ValueError('remote backend source differs; stage the current commit before qualification')
        remote_image=subprocess.check_output(ssh+[remote_command(shlex.join(['docker','image','inspect','dios-worker-fixture:local','--format','{{.Id}}']), args)],text=True,timeout=20).strip()
        local_image=subprocess.check_output(['docker','image','inspect','dios-worker-fixture:local','--format','{{.Id}}'],text=True).strip()
        if remote_image!=local_image:raise ValueError('workload image mismatch')
    start=time.monotonic()
    cases=[cls('test_budget_survives_worker_loss_and_single_settlement')]
    if args.toxiproxy:cases.append(PartitionResources('test_partition_preserves_budget_and_revoked_work_cannot_resume'))
    with (output/'tests.log').open('w') as log:
        result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite(cases))
    passed=result.wasSuccessful() and not result.skipped and len(cls.observations)==len(cases)
    files=['scripts/qualify_resource_recovery.py','scripts/qualify_physical_worker.py','scripts/qualify_physical_faults.py','backend/tests/test_remote_recovery_qualification.py','backend/app/runtime/capacity.py','backend/app/runtime/consumption.py','backend/app/runtime/remote_control.py','backend/app/runtime/remote_runtime.py','backend/app/runtime/worker.py']
    report={'work_item':'K04-2.04','passed':passed,'complete':False,'mode':'local-independent-process' if args.local else 'physical-two-host',
        'implementation_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'tests':result.testsRun,'seconds':round(time.monotonic()-start,3),'host_boot_sha256':boots,'observations':cls.observations,
        'failures':[{'test':test.id(),'traceback':trace} for test,trace in result.failures+result.errors],
        'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files},
        'limits':['Synthetic reported usage exercises accounting correlation; no provider invoice or hard cost ceiling is qualified.',
                  'Worker SIGKILL and optional Toxiproxy partition affect only qualification resources; old control-plane primary and physical-host loss require separate evidence.',
                  'Local mode does not qualify two physical hosts; complete remains false until the full WBS matrix is accepted.']}
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('passed','complete','tests','seconds')}))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())