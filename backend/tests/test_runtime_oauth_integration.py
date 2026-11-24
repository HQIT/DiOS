"""Opt-in destructive source-token qualification against an isolated Keycloak fixture."""
import asyncio,json,os,threading,subprocess,unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
from app.runtime.database import RuntimeErrorCode
from app.runtime.mcp_tool_transport import McpHttpTransport
import test_runtime_oauth_source as fixtures

@unittest.skipUnless(os.environ.get('DIOS_TEST_OAUTH_SOURCE_CONFIG'),'requires dedicated revocable Keycloak source fixture')
class OAuthRuntimeIntegration(unittest.IsolatedAsyncioTestCase):
    def configure(self,root):
        self.external=json.loads(Path(os.environ['DIOS_TEST_OAUTH_SOURCE_CONFIG']).read_text())
        self.config=self.external['provider'];self.reference=self.external['reference'];self.expiry=self.reference['expires_at'];self.access=Path(self.reference['path']).read_text();self.transport=None
    async def asyncSetUp(self):
        await fixtures.OAuthToolTests.asyncSetUp(self);self.calls=[];self.revoke_on_initialize=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if request['method']=='notifications/initialized':self.send_response(202);self.end_headers();return
                if request['method']=='initialize':
                    if owner.revoke_on_initialize:
                        with httpx.Client(trust_env=False,timeout=5) as client:
                            response=client.post(owner.external['revocation_endpoint'],data={'client_id':owner.reference['client_id'],'token':owner.access,'token_type_hint':'access_token'})
                            if response.status_code!=200:self.send_error(502);return
                    result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'source-qualification','version':'1'}}
                else:owner.calls.append(request);result={'content':[{'type':'text','text':'Verified source-bound result'}],'isError':False}
                raw=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',19999),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.gate.transport=McpHttpTransport();self.pulse=asyncio.create_task(self.keep_lease())
    async def keep_lease(self):
        while True:await self.store.heartbeat(*self.binding);await asyncio.sleep(5)
    async def asyncTearDown(self):
        self.pulse.cancel()
        try:await self.pulse
        except asyncio.CancelledError:pass
        await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.thread.join(2)
        await fixtures.OAuthToolTests.asyncTearDown(self)
    approved=fixtures.OAuthToolTests.approved
    async def test_real_identity_outage_resume_and_revoke_before_mcp_call(self):
        item=await self.approved();cid=self.external['container']
        info=json.loads(await asyncio.to_thread(subprocess.check_output,['docker','inspect',cid],text=True))[0]
        self.assertEqual(info['Config']['Labels'].get('dios.purpose'),'development-identity')
        await asyncio.to_thread(subprocess.run,['docker','stop',cid],check=True,stdout=subprocess.DEVNULL)
        try:
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
            self.assertEqual(self.calls,[]);self.assertEqual((await self.gate.get(self.run['id'],item['id']))['approval_state'],'approved')
        finally:await asyncio.to_thread(subprocess.run,['docker','start',cid],check=True,stdout=subprocess.DEVNULL)
        async with httpx.AsyncClient(trust_env=False,timeout=1) as client:
            for _ in range(180):
                try:
                    if (await client.get(self.config['issuer']+'/.well-known/openid-configuration')).status_code==200:break
                except httpx.HTTPError:pass
                await asyncio.sleep(.5)
            else:self.fail('Identity service failed to restart')
        result=await self.gate.dispatch(self.run['id'],item['id'],item['version']);self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.calls),1)
        item=await self.gate.request(self.run['id'],self.attempt['id'],'session-one','search',{'query':'second'},'second')
        item=await self.gate.decide(self.run['id'],item['id'],item['version'],'approved','fixture-approval');self.revoke_on_initialize=True
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(len(self.calls),1)
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(self.context,'execute')
        from app.runtime.work_authorizations import view
        state=await view(self.store,self.run['id']);self.assertEqual(state['state'],'revoked')
        self.assertNotIn(self.access,json.dumps(await self.store.get(self.run['id'])))
        output=os.environ.get('DIOS_TEST_OAUTH_EVIDENCE')
        if output:Path(output).write_text(json.dumps({'checks':['real-IdP-approval','provider-stop-retains-approval','provider-restart-same-call-resume','real-MCP-success-once','source-revoked-during-MCP-initialize-no-tools-call','work-revocation-persisted','token-not-in-run-record'],'actual_tools_calls':len(self.calls),'source_work_state':state['state']},indent=2))
