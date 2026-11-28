"""Opt-in A04/A05: real isolated JSONL and ACP processes; no external model or messages."""
import asyncio
import copy
import hashlib
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import unittest

import test_workitems as fixtures
from test_registry import sample
from test_interventions import API
from dios_workforce.compensations import Compensations
from dios_workforce.registry import RegistryError
from app.runtime.worker import Worker,read_profile
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.store import digest
from app.runtime.tool_catalog import FileToolCatalog,FileApprovers
from app.runtime.tool_invocations import ToolInvocations
from dios_workforce.interventions import Interventions

class ResourceTransport:
    def __init__(self,path):self.path=path;self.calls=[]
    @asynccontextmanager
    async def open(self,registration,headers):yield self
    async def call(self,name,arguments,key):
        assert arguments=={'resource':'fixture-reservation'}
        value=json.loads(self.path.read_text()) if self.path.exists() else dict(state='empty',reserve_count=0,release_count=0)
        if name=='reserve' and value['state']=='empty':value.update(state='reserved',reserve_count=value['reserve_count']+1)
        if name=='release' and value['state']=='reserved':value.update(state='released',release_count=value['release_count']+1)
        self.path.write_text(json.dumps(value));self.calls.append(dict(name=name,key=key))
        return {'content':[{'type':'text','text':json.dumps(value)}],'isError':False}

OBSERVATIONS=[]
ROOT=Path(__file__).resolve().parents[3]


@unittest.skipUnless(os.environ.get('DIOS_WORKITEM_TEST_IMAGE'),'explicit local image required')
class WorkItemNativeTests(unittest.IsolatedAsyncioTestCase):
    save=fixtures.WorkItemIntegrationTests.save
    headers_for=fixtures.WorkItemIntegrationTests.headers_for

    async def asyncSetUp(self):
        await fixtures.WorkItemIntegrationTests.asyncSetUp(self)
        self.reviewer=API(self,'c');self.comp=Compensations(self.work)
        self.resource=self.root/'resource';self.resource.mkdir(mode=0o777);self.resource.chmod(0o777)
        self.adapter=DockerRuntimeAdapter('workitem-joint-'+self.root.name)
        self.specs={};config=json.loads(self.policy.path.read_text());tools={}
        for name in ('reserve','release'):
            registration=dict(enabled=True,transport='mcp-http',url='http://127.0.0.1:19999/mcp',name=name,
                approval_required=name=='release',input_schema={'type':'object','required':['resource'],'properties':{'resource':{'const':'fixture-reservation'}},'additionalProperties':False},result_schema={'type':'object'})
            registration['permission']=dict(action='fixture.'+name,resource_kind='mcp_tool',resource_id='fixture/'+name,
                resource_version=digest({k:v for k,v in registration.items() if k!='enabled'}),constraint_revision='test-policy')
            tools[name]=registration
        catalog=self.root/'tools.json';catalog.write_text(json.dumps({'version':1,'tools':tools}))
        approvers=self.root/'approvers.json';self.approver_token='fixture-approval'
        approvers.write_text(json.dumps({'version':1,'approvers':[dict(enabled=True,subject='subject:c',token_sha256=hashlib.sha256(self.approver_token.encode()).hexdigest(),scopes=['one'],tools=['release'])]}))
        self.transport=ResourceTransport(self.resource/'reservation.json')
        self.gate=ToolInvocations(self.console.store,self.policy,FileToolCatalog(catalog),self.console.provider,self.transport,FileApprovers(approvers))
        self.console.tools=self.gate
        for name,actor in [('forward','a'),('undo','b')]:
            profile=copy.deepcopy(self.profile);profile['id']=name
            profile['console']['implementation']='synthetic-jsonl' if name=='forward' else 'synthetic-acp'
            tool='reserve' if name=='forward' else 'release'
            profile['tool_channel']={'tools':{tool:tool},'acp_names':{'fixture.'+tool:tool}}
            profile['launch']=dict(image=os.environ['DIOS_WORKITEM_TEST_IMAGE'],command=['python','/qualification/workitem_native_fixture.py',name],
                environment={'PYTHONPATH':'/app','PYTHONDONTWRITEBYTECODE':'1'},cpu_count=.5,
                mounts=[dict(source=str(ROOT/'backend'),target='/app'),dict(source=str(Path(__file__).parent),target='/qualification'),
                        dict(source=str(self.resource),target='/run/workitem-fixture',read_only=True)])
            profile['recovery']=dict(resource='fixture:'+name,replay_safe=True,max_attempts=2,base_delay=1,max_delay=1,quota_delay=1)
            path=self.profiles/(name+'.json');path.write_text(json.dumps(profile));_,spec=read_profile(path);self.specs[name]=spec
            permissions=[dict(action='runtime.'+action,resource_kind='runtime_profile',resource_id=name,
                resource_version=spec.fingerprint(),constraint_revision='test-policy') for action in ('execute','accept_result')]
            permissions.append(tools[tool]['permission'])
            config['profiles'][name]=dict(profile_digest=spec.fingerprint(),secret_refs=[],work_ttl_seconds=86400,
                permissions={layer:permissions for layer in ('caller','service','workload','resource')})
            self.directory['principals'][actor]['profiles'][name]=dict(actions=['run.submit','run.read','run.cancel','artifact.read','tool.read'],permissions=permissions)
            self.policy.path.write_text(json.dumps(config));self.save()
            definition=sample(spec.fingerprint());definition['bindings']['review']['profile_id']=name
            self.registry.template(name,1,definition)
            self.registry.revise('employee-'+actor,name,1,{},expected_version=2)
            await self.registry.publish('employee-'+actor,API(self,actor),expected_version=3)
        self.work.bind('work-001','employee-a',expected_version=2)

    async def asyncTearDown(self):
        self.adapter.client.close()
        await fixtures.WorkItemIntegrationTests.asyncTearDown(self)

    def worker(self,name):
        return Worker(self.console.store,self.adapter,name,self.specs[name],self.console.provider,
            authorization_verifier=self.verifier,tool_gate=self.gate,poll_interval=.05,timeout=45)

    async def share_and_confirm(self,work_id,row):
        self.directory['principals']['c']['objects'][row['run_id']]=['run.read','artifact.read'];self.save()
        row=await self.work.observe(work_id,self.reviewer,expected_version=row['version'])
        self.assertEqual(row['state'],'awaiting_confirmation')
        return await self.work.confirm(work_id,self.reviewer,'accept','实际资源与成果已核对',expected_version=row['version'])

    async def test_duplicate_triggers_retry_two_implementations_compensation_and_human_close(self):
        source=self.work.get('work-001');spec=source['specification']
        # Event, timer and manual entrypoints use the application's same logical business ID.
        for _ in ('event','timer','manual'):
            again=await self.work.create('work-001',spec['title'],spec['kind'],spec['sample'],spec['goal'],self.reviewer)
            self.assertEqual(again,source)
        source=await self.work.dispatch('work-001',API(self,'a'),expected_version=source['version'])
        first=await self.worker('forward').once();self.assertEqual(first['state'],'retry_wait')
        self.assertFalse((self.resource/'reservation.json').exists())
        self.assertEqual((await self.work.dispatch('work-001',API(self,'a'),expected_version=source['version']))['run_id'],source['run_id'])
        # Current reviewer observation stays running while technical attempts are retried.
        self.directory['principals']['c']['objects'][source['run_id']]=['run.read','artifact.read'];self.save()
        observed=await self.work.observe('work-001',self.reviewer,expected_version=source['version']);self.assertEqual(observed['state'],'running')
        (self.resource/'network-fault-used').write_text('fault already exercised')
        async with asyncio.timeout(20):
            while True:
                result=await self.worker('forward').once()
                if result['state']!='idle':break
                await asyncio.sleep(.2)
        self.assertEqual(result['id'],source['run_id']);self.assertEqual(result['state'],'succeeded')
        self.assertEqual(len(result['attempts']),2)
        self.assertEqual(json.loads((self.resource/'reservation.json').read_text())['reserve_count'],1)
        source=await self.share_and_confirm('work-001',source)
        await self.work.create('undo-001','释放登记资源','review','first-item','释放本次登记的资源',self.reviewer)
        self.work.bind('undo-001','employee-b',expected_version=1)
        linked=await self.comp.plan('work-001','undo-001',self.reviewer,'业务决定释放原资源','compensate-1',expected_version=source['version'])
        permissions=self.directory['principals']['b']['profiles']['undo']['permissions']
        self.directory['principals']['b']['profiles']['undo']['permissions']=[];self.save()
        with self.assertRaises(RegistryError):await self.work.dispatch('undo-001',API(self,'b'),expected_version=2)
        self.assertEqual(json.loads((self.resource/'reservation.json').read_text())['release_count'],0)
        self.directory['principals']['b']['profiles']['undo']['permissions']=permissions;self.save()
        target=await self.work.dispatch('undo-001',API(self,'b'),expected_version=2)
        self.directory['principals']['c']['objects'][target['run_id']]=['run.read','artifact.read','tool.read','tool.approve'];self.save()
        task=asyncio.create_task(self.worker('undo').once())
        try:
            async with asyncio.timeout(30):
                while True:
                    page=await self.gate.list(target['run_id'])
                    if page['items']:break
                    if task.done():self.fail(str(task.result()))
                    await asyncio.sleep(.1)
            call=page['items'][0];self.assertEqual(call['state'],'awaiting_approval')
            pending=await Interventions(self.work).sync_approvals('undo-001',self.reviewer,expected_version=target['version'])
            self.assertEqual(pending['approvals'][0]['approval_state'],'pending')
            self.assertEqual(json.loads((self.resource/'reservation.json').read_text())['release_count'],0)
            response=await self.client.post('/api/os/runtime/runs/'+target['run_id']+'/tools/'+call['id']+'/approval',
                headers=dict(self.headers_for('c'),**{'x-dios-approver-token':self.approver_token}),
                json=dict(expected_version=call['version'],decision='approved',reason='核准释放登记资源'))
            self.assertEqual(response.status_code,200,response.text)
            undone=await asyncio.wait_for(task,30);self.assertEqual(undone['state'],'succeeded')
            observed=await Interventions(self.work).sync_approvals('undo-001',self.reviewer,expected_version=target['version'])
            self.assertEqual(observed['approvals'][0]['approval_state'],'consumed')
        finally:
            if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
        with self.assertRaisesRegex(RegistryError,'COMPENSATION_NOT_CONFIRMED'):
            await self.comp.confirm('work-001',self.reviewer,'补偿完成',expected_version=linked['work_item']['version'])
        target=await self.share_and_confirm('undo-001',target)
        closed=await self.comp.confirm('work-001',self.reviewer,'补偿完成',expected_version=linked['work_item']['version'])
        self.assertEqual(closed['work_item']['state'],'compensated')
        for _ in range(2):
            await self.comp.confirm('work-001',self.reviewer,'补偿完成',expected_version=linked['work_item']['version'])
        resource=json.loads((self.resource/'reservation.json').read_text())
        self.assertEqual(resource,dict(state='released',reserve_count=1,release_count=1))
        own_runs=await API(self,'a').request('GET','runs');undo_runs=await API(self,'b').request('GET','runs')
        self.assertEqual(len(own_runs['items']),1);self.assertEqual(len(undo_runs['items']),1)
        attempts=result['attempts']+undone['attempts']
        self.assertTrue(all(a['process_id'] and a['process_instance'] and a['session_id'] for a in attempts),
            [{k:a[k] for k in ('process_id','process_instance','session_id')} for a in attempts])
        self.assertEqual(len({a['process_instance'] for a in attempts}),3)
        self.assertNotEqual(result['attempts'][-1]['session_id'],undone['attempts'][0]['session_id'])
        OBSERVATIONS.append(dict(logical_work_items=2,runs=2,forward_attempts=2,compensation_attempts=1,
            resource=resource,business_state=closed['work_item']['state'],implementations=['jsonl-v1','production-acp-bridge'],
            process_instances=[a['process_instance'] for a in attempts],sessions=[a['session_id'] for a in attempts],
            tool_calls=self.transport.calls,approval_state=closed['compensations'][0]['work_item']['approvals'][0]['approval_state'],max_concurrent_workers=1))
