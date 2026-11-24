"""Opt-in: official OPA container, durable Runtime and a recording MCP HTTP endpoint."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
import subprocess
import threading
import socket
from contextlib import asynccontextmanager
import unittest
from uuid import uuid4
import httpx
from app.runtime.database import RuntimeErrorCode
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.tool_catalog import FileToolCatalog,FileApprovers
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.store import digest
import test_runtime_worker as fixtures
from work_auth_fixtures import policy_fixture
import test_runtime_tool_invocations as toolfixtures

IMAGE='openpolicyagent/opa:1.17.0@sha256:3c6e9e4d433b6e94df424c3385134312a95042aa991cdfc8e01944115675fb9d'

@unittest.skipUnless(os.environ.get('DIOS_TEST_OPA')=='1','requires explicit real OPA container qualification')
class OpaIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.WorkerTests.asyncSetUp(self);self.cid=None;self.mcp=None;self.calls=[]
        self.addAsyncCleanup(self.cleanup)
        self.bundle=self.root/'bundle';self.bundle.mkdir();self.bundle.chmod(0o755);self.root.chmod(0o755)
        self.write_bundle('v1')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        self.cid=await asyncio.to_thread(self.docker,'run','-d','--name','dios-opa-gate-'+uuid4().hex[:12],
            '--label','dios.purpose=opa-gate-test','-p','127.0.0.1:'+str(port)+':8181','--memory=256m','--cpus=1',
            '-v',str(self.bundle)+':/policy:ro',IMAGE,'run','--server','--addr=0.0.0.0:8181','--log-level=error','--disable-telemetry','--bundle','/policy')
        info=json.loads(await asyncio.to_thread(self.docker,'inspect',self.cid))[0]
        self.url='http://127.0.0.1:'+info['NetworkSettings']['Ports']['8181/tcp'][0]['HostPort']
        await self.ready()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if value['method']=='notifications/initialized':self.send_response(202);self.end_headers();return
                if value['method']=='initialize':
                    result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
                else:
                    owner.calls.append(value);result={'content':[{'type':'text','text':'中文 evidence'}],'isError':False}
                raw=json.dumps({'jsonrpc':'2.0','id':value['id'],'result':result}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.mcp=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.mcp.serve_forever,daemon=True);self.thread.start()
        self.policy,_=policy_fixture(self.root,'test-profile',self.spec)
        self.registration=dict(enabled=True,transport='mcp-http',url='http://127.0.0.1:'+str(self.mcp.server_port)+'/mcp',name='search',approval_required=True,
            input_schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False},
            result_schema={'type':'object','required':['content']},
            policy_evaluator={'kind':'opa','url':self.url+'/v1/data/dios/tools/allow','bundles':{'/policy':'v1'},'timeout_seconds':1})
        self.permission=dict(action='repository.search',resource_kind='mcp_tool',resource_id='review/search',
            resource_version=digest({k:v for k,v in self.registration.items() if k!='enabled'}),constraint_revision='test-policy')
        self.registration['permission']=self.permission
        config=json.loads(self.policy.path.read_text())
        for rows in config['profiles']['test-profile']['permissions'].values():rows.append(self.permission)
        self.policy.path.write_text(json.dumps(config))
        self.catalog_file=self.root/'tools.json';self.catalog_file.write_text(json.dumps({'version':1,'tools':{'search':self.registration}}))
        import hashlib
        self.approver_file=self.root/'approvers.json';self.approver_file.write_text(json.dumps({'version':1,'approvers':[
            dict(enabled=True,subject='operator:independent',token_sha256=hashlib.sha256(b'fixture-approval').hexdigest(),scopes=[self.scope],tools=['search'])]}))
        descriptor=self.policy.admission('test-profile',self.spec.fingerprint(),[])
        self.run=await self.store.submit('opa-run',dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),secret_refs=[]),authorization=descriptor)
        await self.store.register_worker('worker','instance','host',os.getpid());self.attempt=await self.store.claim('worker','instance')
        self.binding=(self.run['id'],self.attempt['id'],'worker','instance',self.attempt['epoch'])
        self.context=WorkAuthorizationContext(self.scope,*self.binding[:2],'worker','instance',self.attempt['epoch'],self.attempt['authorization_epoch'],None,self.run['input']['work_authorization_ref'])
        self.verifier=WorkAuthorizationVerifier(self.store,self.policy)
        await self.verifier(self.context,'execute');await self.store.report(*self.binding,1,'preparing');await self.store.report(*self.binding,2,'creating_session')
        await self.verifier(replace(self.context,session_id='session-one'),'execute');await self.store.report(*self.binding,3,'waiting_model',session_id='session-one')
        self.gate=ToolInvocations(self.store,self.policy,FileToolCatalog(self.catalog_file),self.provider,McpHttpTransport(),FileApprovers(self.approver_file))
    @staticmethod
    def docker(*args):return subprocess.check_output(['docker',*args],text=True,stderr=subprocess.PIPE).strip()
    def write_bundle(self,revision):
        (self.bundle/'.manifest').write_text(json.dumps({'revision':revision,'roots':['dios'],'rego_version':1}))
        (self.bundle/'tools.rego').write_text('package dios.tools\nimport rego.v1\ndefault allow := false\nallow if {\n input.action.properties.arguments.query != "forbidden"\n every layer in ["caller", "service", "workload", "resource"] {\n input.context.permission in input.context.permissions[layer]\n }\n}\n')
    async def ready(self):
        async with httpx.AsyncClient(trust_env=False,timeout=.2) as client:
            for _ in range(100):
                try:
                    response=await client.get(self.url+'/health');response.raise_for_status();return
                except httpx.HTTPError:await asyncio.sleep(.05)
        raise RuntimeError('OPA failed to become ready')
    async def cleanup(self):
        if self.mcp:
            await asyncio.to_thread(self.mcp.shutdown);self.mcp.server_close();self.thread.join(2)
        if self.cid:await asyncio.to_thread(self.docker,'rm','-f',self.cid)
        await fixtures.WorkerTests.asyncTearDown(self)
    request=toolfixtures.ToolInvocationTests.request
    approve=toolfixtures.ToolInvocationTests.approve
    async def test_real_opa_approval_http_tool_receipt_and_no_duplicate_dispatch(self):
        item=await self.approve(await self.request())
        result=await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['id'],item['id'])
        decisions=[e['decision']['evidence'] for e in result['evidence'] if e['stage']=='policy']
        self.assertTrue(decisions);self.assertTrue(all(d['bundles']=={'/policy':'v1'} for d in decisions))
        await self.gate.dispatch(self.run['id'],item['id'],item['version']);self.assertEqual(len(self.calls),1)
    async def test_real_opa_parameter_deny_produces_no_upstream_call(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_DENIED'):await self.request(arguments={'query':'forbidden'})
        self.assertEqual(self.calls,[])
    async def test_bundle_change_after_approval_blocks_dispatch(self):
        item=await self.approve(await self.request());self.write_bundle('v2')
        await asyncio.to_thread(self.docker,'restart',self.cid);await self.ready()
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_REVISION_CHANGED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[]);self.assertEqual((await self.gate.get(self.run['id'],item['id']))['approval_state'],'approved')
    async def test_live_pdp_outage_after_approval_keeps_call_unconsumed(self):
        item=await self.approve(await self.request());await asyncio.to_thread(self.docker,'stop',self.cid)
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_EVALUATOR_UNAVAILABLE'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[]);self.assertEqual((await self.gate.get(self.run['id'],item['id']))['approval_state'],'approved')

    async def test_policy_upgrade_during_mcp_initialization_blocks_final_dispatch(self):
        item=await self.approve(await self.request());original=self.gate.transport
        owner=self
        class Transport:
            @asynccontextmanager
            async def open(self,*args):
                async with original.open(*args) as channel:
                    owner.write_bundle('v2')
                    await asyncio.to_thread(owner.docker,'restart',owner.cid);await owner.ready()
                    yield channel
        self.gate.transport=Transport()
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_REVISION_CHANGED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[])

    async def test_pdp_total_timeout_does_not_consume_approval(self):
        from app.runtime.policy_evaluator import OpaEvaluator
        item=await self.approve(await self.request())
        async def slow(request):await asyncio.sleep(2);return httpx.Response(200,json={'result':True})
        self.gate.policy_evaluator=OpaEvaluator(transport=httpx.MockTransport(slow))
        with self.assertRaisesRegex(RuntimeErrorCode,'POLICY_EVALUATOR_TIMEOUT'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[]);self.assertEqual((await self.gate.get(self.run['id'],item['id']))['approval_state'],'approved')

    def change_approver(self, **changes):
        config=json.loads(self.approver_file.read_text())
        config['approvers'][0].update(changes)
        self.approver_file.write_text(json.dumps(config))

    def after_policy(self, callback, at=1):
        original=self.gate.policy_evaluator;count=0
        class Evaluator:
            async def evaluate(inner,*args):
                nonlocal count
                result=await original.evaluate(*args);count+=1
                if count==at:callback()
                return result
        self.gate.policy_evaluator=Evaluator()

    async def test_approver_revoked_during_policy_wait_cannot_approve(self):
        item=await self.request()
        self.after_policy(lambda:self.change_approver(enabled=False))
        with self.assertRaisesRegex(RuntimeErrorCode,'APPROVER_FORBIDDEN'):
            await self.approve(item)
        saved=await self.gate.get(self.run['id'],item['id'])
        self.assertEqual(saved['approval_state'],'pending');self.assertEqual(self.calls,[])

    async def test_approver_revoked_during_final_policy_wait_cannot_dispatch_or_resurrect(self):
        item=await self.approve(await self.request())
        # First query is preflight; second runs after real MCP initialization.
        self.after_policy(lambda:self.change_approver(enabled=False),at=2)
        with self.assertRaisesRegex(RuntimeErrorCode,'APPROVER_FORBIDDEN'):
            await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[])
        saved=await self.gate.get(self.run['id'],item['id'])
        self.assertEqual((saved['state'],saved['approval_state']),('denied','invalidated'))
        self.assertEqual(saved['version'],item['version']+1)
        self.change_approver(enabled=True)
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_APPROVAL_INVALIDATED'):
            await self.gate.dispatch(self.run['id'],item['id'],saved['version'])
        self.assertEqual(self.calls,[])
        fresh=await self.approve(await self.request('fresh-approval'))
        self.assertEqual((await self.gate.dispatch(self.run['id'],fresh['id'],fresh['version']))['state'],'succeeded')
        self.assertEqual(len(self.calls),1)
