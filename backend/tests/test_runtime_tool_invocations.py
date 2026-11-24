"""Deferred #34 regressions: fake transport only, never invokes a real tool/model."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import os
import unittest
from unittest.mock import patch
from sqlalchemy import select
import test_runtime_worker as fixtures
from work_auth_fixtures import policy_fixture
from app.runtime.database import RuntimeErrorCode
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_auth_schema import grants
from app.runtime.store import digest
from app.runtime.tool_catalog import FileToolCatalog, FileApprovers
from app.runtime.tool_invocations import ToolInvocations, require_settled
from app.runtime.tool_schema import invocations, evidence
from app.runtime.recovery import backup_sqlite, verify_backup


class RecordingTransport:
    def __init__(self):self.calls=[];self.opened=0;self.failure=False;self.hook=None
    @asynccontextmanager
    async def open(self,registration,headers):
        self.opened+=1
        if self.hook:await self.hook()
        yield self
    async def call(self,name,arguments,key):
        self.calls.append((name,arguments,key))
        if self.failure:raise TimeoutError()
        return {'content':[{'type':'text','text':'中文结果🙂'}],'isError':False}


class ToolInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.WorkerTests.asyncSetUp(self)
        self.policy,_=policy_fixture(self.root,'test-profile',self.spec)
        self.registration=dict(enabled=True,transport='mcp-http',url='http://127.0.0.1:19999/mcp',name='search',
            approval_required=True,input_schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False},
            result_schema={'type':'object','required':['content'],'properties':{'content':{'type':'array'}}})
        version=digest({k:v for k,v in self.registration.items() if k!='enabled'})
        self.permission=dict(action='repository.search',resource_kind='mcp_tool',resource_id='review/search',
            resource_version=version,constraint_revision='test-policy')
        self.registration['permission']=self.permission
        config=json.loads(self.policy.path.read_text())
        for rows in config['profiles']['test-profile']['permissions'].values():rows.append(self.permission)
        self.policy.path.write_text(json.dumps(config))
        self.catalog_file=self.root/'tools.json';self.catalog_file.write_text(json.dumps({'version':1,'tools':{'search':self.registration}}))
        self.approver_file=self.root/'approvers.json'
        self.approver_file.write_text(json.dumps({'version':1,'approvers':[
            dict(enabled=True,subject='operator:independent',token_sha256=hashlib.sha256(b'fixture-approval').hexdigest(),scopes=[self.scope],tools=['search']),
            dict(enabled=True,subject='operator:test',token_sha256=hashlib.sha256(b'fixture-self').hexdigest(),scopes=[self.scope],tools=['search'])]}))
        descriptor=self.policy.admission('test-profile',self.spec.fingerprint(),[])
        self.run=await self.store.submit('run',dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),secret_refs=[]),authorization=descriptor)
        await self.store.register_worker('worker','instance','host',os.getpid())
        self.attempt=await self.store.claim('worker','instance')
        self.binding=(self.run['id'],self.attempt['id'],'worker','instance',self.attempt['epoch'])
        self.context=WorkAuthorizationContext(self.scope,*self.binding[:2],'worker','instance',self.attempt['epoch'],
            self.attempt['authorization_epoch'],None,self.run['input']['work_authorization_ref'])
        self.verifier=WorkAuthorizationVerifier(self.store,self.policy)
        await self.verifier(self.context,'execute')
        await self.store.report(*self.binding,1,'preparing')
        await self.store.report(*self.binding,2,'creating_session')
        await self.verifier(replace(self.context,session_id='session-one'),'execute')
        await self.store.report(*self.binding,3,'waiting_model',session_id='session-one')
        self.transport=RecordingTransport()
        self.gate=ToolInvocations(self.store,self.policy,FileToolCatalog(self.catalog_file),self.provider,self.transport,FileApprovers(self.approver_file))
    asyncTearDown=fixtures.WorkerTests.asyncTearDown

    async def request(self,key='call',arguments=None):
        return await self.gate.request(self.run['id'],self.attempt['id'],'session-one','search',arguments or {'query':'evidence'},key)

    async def approve(self,item):
        return await self.gate.decide(self.run['id'],item['id'],item['version'],'approved','fixture-approval')

    async def test_legacy_policy_digest_reaches_tools_only_while_file_unchanged(self):
        from app.runtime.schema import runs
        from app.runtime.work_auth_schema import work
        legacy=self.policy.admission('test-profile',self.spec.fingerprint(),[])['legacy_policy_digest']
        async with self.db.transaction(self.scope) as c:
            saved=await self.store._run(c,self.run['id'])
            payload=dict(saved['input'],work_policy_digest=legacy)
            await c.execute(runs.update().where(runs.c.id==self.run['id']).values(input=payload))
            await c.execute(work.update().where(work.c.run_id==self.run['id']).values(policy_digest=legacy))
        await self.verifier(replace(self.context,session_id='session-one'),'execute')
        item=await self.approve(await self.request())
        result=await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'succeeded')
        config=json.loads(self.policy.path.read_text())
        config['profiles']['other']={'profile_digest':'changed'}
        self.policy.path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_CHANGED'):
            await self.request('after-policy-change')

    async def test_pending_never_reaches_transport_and_self_approval_denied(self):
        item=await self.request()
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_APPROVAL_REQUIRED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.opened,0)
        with self.assertRaisesRegex(RuntimeErrorCode,'APPROVER_MUST_BE_INDEPENDENT'):
            await self.gate.decide(self.run['id'],item['id'],1,'approved','fixture-self')
        self.assertEqual(self.transport.calls,[])

    async def test_approval_dispatch_and_idempotent_replay(self):
        item=await self.request();same=await self.request()
        self.assertEqual(item['id'],same['id'])
        approved=await self.approve(item)
        result=await self.gate.dispatch(self.run['id'],item['id'],approved['version'])
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['approval_state'],'consumed')
        self.assertTrue(result['result_key'])
        again=await self.gate.dispatch(self.run['id'],item['id'],approved['version'])
        self.assertEqual(again['state'],'succeeded');self.assertEqual(len(self.transport.calls),1)
        with self.assertRaisesRegex(RuntimeErrorCode,'IDEMPOTENCY_CONFLICT'):await self.request(arguments={'query':'different'})

    async def test_concurrent_dispatch_calls_upstream_once(self):
        item=await self.approve(await self.request())
        await asyncio.gather(*(self.gate.dispatch(self.run['id'],item['id'],item['version']) for _ in range(2)),return_exceptions=True)
        self.assertEqual(len(self.transport.calls),1)

    async def test_revoke_during_initialization_blocks_actual_tool_call(self):
        item=await self.approve(await self.request())
        async def revoke():
            run=await self.store.get(self.run['id']);await self.store.revoke(run['id'],run['version'])
        self.transport.hook=revoke
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.calls,[])

    async def test_dispatch_audit_failure_rolls_back_approval_consumption(self):
        item=await self.approve(await self.request())
        with patch('app.runtime.tool_invocations.audit',side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        saved=await self.gate.get(self.run['id'],item['id'])
        self.assertEqual(saved['approval_state'],'approved');self.assertEqual(saved['state'],'prepared')
        self.assertEqual(self.transport.calls,[])

    async def test_timeout_is_unknown_no_replay_and_blocks_task_success(self):
        item=await self.approve(await self.request());self.transport.failure=True
        result=await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'result_unknown')
        await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(len(self.transport.calls),1)
        async with self.db.read(self.scope) as c:
            with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_RECONCILIATION_REQUIRED'):
                await require_settled(c,self.scope,self.run['id'])

    async def test_changed_session_parameters_or_expiry_cannot_dispatch(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'SESSION_CHANGED'):
            await self.gate.request(self.run['id'],self.attempt['id'],'wrong','search',{'query':'x'},'bad-session')
        item=await self.approve(await self.request())
        async with self.db.transaction(self.scope) as c:
            await c.execute(invocations.update().where(invocations.c.id==item['id']).values(expires_at=0))
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_AUTHORIZATION_EXPIRED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.calls,[])

    async def test_policy_or_endpoint_edit_invalidates_authorization(self):
        item=await self.approve(await self.request())
        config=json.loads(self.catalog_file.read_text());config['tools']['search']['url']='http://127.0.0.1:19998/mcp'
        self.catalog_file.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_VERSION_NOT_PINNED'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.calls,[])

    async def test_revoked_approver_cannot_dispatch_prior_approval(self):
        item=await self.approve(await self.request())
        config=json.loads(self.approver_file.read_text());config['approvers'][0]['enabled']=False
        self.approver_file.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode,'APPROVER_FORBIDDEN'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.calls,[])

    async def test_process_loss_marks_overdue_unknown_without_recall(self):
        item=await self.approve(await self.request())
        async with self.db.transaction(self.scope) as c:
            await c.execute(invocations.update().where(invocations.c.id==item['id']).values(state='dispatched',approval_state='consumed',dispatch_deadline=0))
        result=await self.gate.reconcile_overdue(self.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'result_unknown')
        await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.transport.calls,[])

    async def test_backup_includes_tool_ledger(self):
        await self.request();destination=self.root/'backup'
        await asyncio.to_thread(backup_sqlite,self.url,self.provider.root,destination)
        manifest=await asyncio.to_thread(verify_backup,destination)
        self.assertIn('runtime_tool_schema',manifest['schemas'])
