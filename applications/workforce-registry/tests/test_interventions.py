import hashlib
import json
import time
import unittest
from dataclasses import replace

import test_workitems as fixtures
from dios_workforce.interventions import Interventions
from dios_workforce.registry import Registry, RegistryError
from dios_workforce.workitems import WorkItems


class API(fixtures.API):
    async def cancel(self, run_id, *, expected_version):
        return await self.request('POST','runs/'+run_id+'/cancel',json={'expected_version':expected_version})
    async def tools(self, run_id):return await self.request('GET','runs/'+run_id+'/tools')
    async def tool(self, run_id, invocation_id):return await self.request('GET','runs/'+run_id+'/tools/'+invocation_id)


class InterventionTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.WorkItemTests.setUp
    tearDown=fixtures.WorkItemTests.tearDown
    prepare=fixtures.WorkItemTests.prepare

    async def test_lost_cancel_response_restart_and_handback_fence(self):
        await self.prepare();row=await self.work.dispatch('request',self.client,expected_version=2)
        control=Interventions(self.work);run=dict(id=row['run_id'],state='running',version=3,cancel_requested=False)
        async def get(_):return dict(run)
        async def cancel(*args,**kwargs):run['cancel_requested']=True;raise TimeoutError()
        self.reviewer.get=get;self.client.cancel=cancel
        with self.assertRaises(TimeoutError):await control.takeover('request',self.reviewer,self.client,'人工检查','take-one',expected_version=3)
        self.assertEqual(self.work.get('request')['state'],'takeover_pending')
        self.registry.close();self.registry=Registry(self.path);self.work=WorkItems(self.registry);control=Interventions(self.work)
        row=await control.takeover('request',self.reviewer,self.client,'人工检查','take-one',expected_version=3)
        self.assertEqual(row['state'],'takeover_pending')
        with self.assertRaisesRegex(RegistryError,'TAKEOVER_NOT_SETTLED'):
            await control.handback('request',self.reviewer,self.client,'new','fixed','back',expected_version=4)
        with self.assertRaisesRegex(RegistryError,'NOT_DISPATCHABLE'):
            await self.work.dispatch('request',self.client,expected_version=4)
        with self.assertRaisesRegex(RegistryError,'TAKEOVER_CONFLICT'):
            await control.takeover('request',self.reviewer,self.client,'different','take-one',expected_version=3)
        run['state']='cancelled'
        row=await control.reconcile('request',self.reviewer,self.client,expected_version=4)
        self.assertEqual(row['state'],'human_owned');self.assertIsNone(row['decision'])
        row=await control.handback('request',self.reviewer,self.client,'new','已排查','back',expected_version=5)
        self.assertEqual(row['round_number'],2)
        self.assertEqual(await control.handback('request',self.reviewer,self.client,'new','已排查','back',expected_version=5),row)
        self.assertEqual(len(self.work.history('request')['rounds']),2)
        self.assertEqual(self.work.history('request')['takeovers'][0]['status'],'handed_back')

    async def test_approval_sync_failure_partial_and_stale_data_preserve_history(self):
        await self.prepare();row=await self.work.dispatch('request',self.client,expected_version=2);control=Interventions(self.work)
        item=dict(id='call-1',run_id=row['run_id'],attempt_id='attempt',session_id='session',tool_id='search',
            arguments_digest='digest',state='awaiting_approval',version=2,approval_state='pending',approver=None,
            approval_reason='',expires_at=time.time()+100,expired=False,reason=None)
        async def page(_):return {'items':[dict(item)],'at_limit':True}
        self.reviewer.tools=page
        first=await control.sync_approvals('request',self.reviewer,expected_version=3)
        self.assertTrue(first['approval_observations'][0]['partial'])
        await control.sync_approvals('request',self.reviewer,expected_version=3)
        self.assertEqual(sum(e['kind']=='approval_observed' for e in self.work.history('request')['events']),1)
        item['version']=1
        with self.assertRaisesRegex(RegistryError,'STALE_APPROVAL_OBSERVATION'):
            await control.sync_approvals('request',self.reviewer,expected_version=3)
        history=control.history('request');self.assertEqual(history['approvals'][0]['version'],2)
        self.assertEqual(history['approval_observations'][0]['state'],'unavailable')
        item['version']=3;item['run_id']='another-run'
        with self.assertRaisesRegex(RegistryError,'APPROVAL_RUN_MISMATCH'):
            await control.sync_approvals('request',self.reviewer,expected_version=3)
        self.assertEqual(self.work.get('request'),row)


class InterventionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    save=fixtures.WorkItemIntegrationTests.save
    headers_for=fixtures.WorkItemIntegrationTests.headers_for
    finish=fixtures.WorkItemIntegrationTests.finish
    asyncTearDown=fixtures.WorkItemIntegrationTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.WorkItemIntegrationTests.asyncSetUp(self)
        self.reviewer=API(self,'c');self.control=Interventions(self.work)

    async def dispatch(self, actions=('run.read','artifact.read','tool.read','tool.approve')):
        row=await self.work.dispatch('work-001',API(self,'a'),expected_version=2)
        self.directory['principals']['c']['objects'][row['run_id']]=list(actions);self.save()
        return row

    async def test_takeover_requires_cancel_permission_and_new_round_current_authority(self):
        row=await self.dispatch(('run.read',))
        self.directory['principals']['a']['profiles']['review']['actions'].remove('run.cancel');self.save()
        with self.assertRaises(RegistryError):
            await self.control.takeover('work-001',self.reviewer,API(self,'a'),'需人工排查','take',expected_version=3)
        self.assertEqual(self.work.get('work-001')['state'],'takeover_pending')
        self.assertFalse((await self.console.store.get(row['run_id']))['cancel_requested'])
        self.directory['principals']['a']['profiles']['review']['actions'].append('run.cancel');self.save()
        owned=await self.control.takeover('work-001',self.reviewer,API(self,'a'),'需人工排查','take',expected_version=3)
        self.assertEqual(owned['state'],'human_owned')
        self.assertEqual((await self.console.store.get(row['run_id']))['state'],'cancelled')
        new=await self.control.handback('work-001',self.reviewer,API(self,'a'),'修订目标','处理已完成','back',expected_version=owned['version'])
        permissions=self.directory['principals']['a']['profiles']['review']['permissions']
        self.directory['principals']['a']['profiles']['review']['permissions']=[];self.save()
        with self.assertRaises(RegistryError):await self.work.dispatch('work-001',API(self,'a'),expected_version=new['version'])
        self.assertIsNone(self.work.get('work-001')['run_id'])
        self.assertEqual(self.work.history('work-001')['rounds'][0]['run_id'],row['run_id'])
        self.directory['principals']['a']['profiles']['review']['permissions']=permissions;self.save()
        resumed=await self.work.dispatch('work-001',API(self,'a'),expected_version=new['version'])
        self.assertNotEqual(resumed['run_id'],row['run_id'])
        self.directory['principals']['c']['objects'][resumed['run_id']]=['run.read','artifact.read'];self.save()
        await self.finish()
        observed=await self.work.observe('work-001',self.reviewer,expected_version=resumed['version'])
        accepted=await self.work.confirm('work-001',self.reviewer,'accept','接管后重新核对',expected_version=observed['version'])
        self.assertEqual(accepted['state'],'confirmed');self.assertEqual(accepted['round_number'],2)


    async def test_running_takeover_waits_for_worker_confirmation_and_fences_old_authority(self):
        from app.runtime.authorization import WorkAuthorizationContext
        from app.runtime.database import RuntimeErrorCode
        row=await self.dispatch();store=self.console.store
        await store.register_worker('take-worker','instance','host',123)
        attempt=await store.claim('take-worker','instance');raw=await store.get(row['run_id'])
        binding=(row['run_id'],attempt['id'],'take-worker','instance',attempt['epoch'])
        ctx=WorkAuthorizationContext('one',*binding[:2],'take-worker','instance',attempt['epoch'],
            attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
        await self.verifier(ctx,'execute')
        pending=await self.control.takeover('work-001',self.reviewer,API(self,'a'),'人工排查','take',expected_version=3)
        self.assertEqual(pending['state'],'takeover_pending')
        with self.assertRaises(RuntimeErrorCode):await self.verifier(ctx,'accept_result')
        await store.confirm_cancel(*binding,'process-exit-evidence')
        owned=await self.control.reconcile('work-001',self.reviewer,API(self,'a'),expected_version=pending['version'])
        self.assertEqual(owned['state'],'human_owned')
        self.assertIsNone(owned['decision'])

    async def test_real_approval_and_revocation_are_linked_without_business_completion(self):
        from app.runtime.store import digest
        from app.runtime.tool_catalog import FileApprovers,FileToolCatalog
        from app.runtime.tool_invocations import ToolInvocations
        from app.runtime.authorization import WorkAuthorizationContext
        from app.runtime.database import RuntimeErrorCode
        from test_runtime_tool_invocations import RecordingTransport
        registration=dict(enabled=True,transport='mcp-http',url='http://127.0.0.1:19999/mcp',name='search',
            approval_required=True,input_schema={'type':'object'},result_schema={'type':'object'})
        permission=dict(action='repository.search',resource_kind='mcp_tool',resource_id='review/search',
            resource_version=digest({k:v for k,v in registration.items() if k!='enabled'}),constraint_revision='test-policy')
        registration['permission']=permission
        config=json.loads(self.policy.path.read_text())
        for values in config['profiles']['review']['permissions'].values():values.append(permission)
        self.policy.path.write_text(json.dumps(config))
        self.directory['principals']['a']['profiles']['review']['permissions'].append(permission);self.save()
        # Publish the same employee binding against the current permission snapshot.
        definition=self.registry.revision('employee-a',1)
        fingerprint=(await API(self,'a').profiles())[0]['profile_digest']
        self.assertEqual(definition['bindings']['review']['profile_digest'],fingerprint)
        catalog=self.root/'tools.json';catalog.write_text(json.dumps({'version':1,'tools':{'search':registration}}))
        approvers=self.root/'approvers.json';approved_token='fixture-approval-only'
        config_approvers={'version':1,'approvers':[dict(enabled=True,subject='subject:c',token_sha256=hashlib.sha256(approved_token.encode()).hexdigest(),scopes=['one'],tools=['search'])]}
        approvers.write_text(json.dumps(config_approvers))
        transport=RecordingTransport();gate=ToolInvocations(self.console.store,self.policy,FileToolCatalog(catalog),self.console.provider,transport,FileApprovers(approvers))
        self.console.tools=gate;row=await self.dispatch();store=self.console.store
        await store.register_worker('approval-worker','instance','host',123);attempt=await store.claim('approval-worker','instance')
        binding=(row['run_id'],attempt['id'],'approval-worker','instance',attempt['epoch']);raw=await store.get(row['run_id'])
        ctx=WorkAuthorizationContext('one',*binding[:2],'approval-worker','instance',attempt['epoch'],attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
        await self.verifier(ctx,'execute');await store.report(*binding,1,'preparing');await store.report(*binding,2,'creating_session')
        await self.verifier(replace(ctx,session_id='tool-session'),'execute');await store.report(*binding,3,'waiting_model',session_id='tool-session')
        call=await gate.request(row['run_id'],attempt['id'],'tool-session','search',{'query':'evidence'},'call')
        history=await self.control.sync_approvals('work-001',self.reviewer,expected_version=3)
        self.assertEqual(history['approvals'][0]['approval_state'],'pending')
        url='/api/os/runtime/runs/'+row['run_id']+'/tools/'+call['id']+'/approval'
        response=await self.client.post(url,headers=dict(self.headers_for('c'),**{'x-dios-approver-token':approved_token}),
            json=dict(expected_version=call['version'],decision='approved',reason='核准只读检索'))
        self.assertEqual(response.status_code,200,response.text);call=response.json()
        history=await self.control.sync_approvals('work-001',self.reviewer,expected_version=3)
        self.assertEqual(history['approvals'][0]['approval_state'],'approved')
        config_approvers['approvers'][0]['enabled']=False;approvers.write_text(json.dumps(config_approvers))
        with self.assertRaises(RuntimeErrorCode):await gate.dispatch(row['run_id'],call['id'],call['version'])
        history=await self.control.sync_approvals('work-001',self.reviewer,expected_version=3,invocation_id=call['id'])
        self.assertEqual(history['approvals'][0]['approval_state'],'invalidated');self.assertEqual(transport.calls,[])
        self.assertEqual(self.work.get('work-001')['state'],'running');self.assertIsNone(self.work.get('work-001')['decision'])
        self.directory['principals']['c']['objects'][row['run_id']]=['run.read'];self.save()
        with self.assertRaises(RegistryError):await self.control.sync_approvals('work-001',self.reviewer,expected_version=3)
        self.assertEqual(self.control.history('work-001')['approval_observations'][0]['state'],'unavailable')
