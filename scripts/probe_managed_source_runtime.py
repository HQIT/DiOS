"""Real KV/IdP/MCP gate qualification; isolates its own synthetic KV key and role."""
import argparse,asyncio,json,os,subprocess,sys,tempfile,threading,time,unittest,uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'backend'),str(ROOT/'backend/tests')]
from app.runtime.openbao_secrets import OpenBaoSecrets
from app.runtime.database import RuntimeErrorCode
from app.runtime.mcp_tool_transport import McpHttpTransport
from app.runtime.work_authorizations import view
import test_runtime_oauth_source as fixtures

class ManagedIntegration(unittest.IsolatedAsyncioTestCase):
    def configure(self,root):
        self.config=EXTERNAL['provider'];self.reference=EXTERNAL['reference'];self.expiry=self.reference['expires_at'];self.transport=None
        self.access=SOURCE_VALUE
    async def asyncSetUp(self):
        await fixtures.OAuthToolTests.asyncSetUp(self);self.calls=[];self.delete_on_initialize=False;owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if request['method']=='notifications/initialized':self.send_response(202);self.end_headers();return
                if request['method']=='initialize':
                    if owner.delete_on_initialize:ADMIN('POST',f'{MOUNT}/delete/{KEY}',{'versions':[1]})
                    result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'managed-qualification','version':'1'}}
                else:owner.calls.append(request);result={'content':[{'type':'text','text':'Managed source evidence'}],'isError':False}
                raw=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.server=ThreadingHTTPServer(('127.0.0.1',19999),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.gate.transport=McpHttpTransport();self.pulse=asyncio.create_task(self.keep_lease())
    async def keep_lease(self):
        while True:await self.store.heartbeat(*self.binding);await asyncio.sleep(5)
    async def asyncTearDown(self):
        self.pulse.cancel()
        try:await self.pulse
        except asyncio.CancelledError:pass
        await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.thread.join(2);await fixtures.OAuthToolTests.asyncTearDown(self)
    approved=fixtures.OAuthToolTests.approved
    async def test_managed_source_faults_at_actual_mcp_boundary(self):
        item=await self.approved();old=SINK.read_text();await asyncio.to_thread(ADMIN,'POST','auth/token/revoke',{'token':old})
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.calls,[]);self.assertEqual((await self.gate.get(self.run['id'],item['id']))['approval_state'],'approved')
        await asyncio.to_thread(ISSUE);result=await self.gate.dispatch(self.run['id'],item['id'],item['version']);self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.calls),1)
        item=await self.gate.request(self.run['id'],self.attempt['id'],'session-one','search',{'query':'second'},'second');item=await self.gate.decide(self.run['id'],item['id'],item['version'],'approved','fixture-approval')
        self.delete_on_initialize=True
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(len(self.calls),1);self.assertEqual((await view(self.store,self.run['id']))['state'],'revoked')
        await asyncio.to_thread(ADMIN,'POST',f'{MOUNT}/undelete/{KEY}',{'versions':[1]})
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(len(self.calls),1);self.assertNotIn(self.access,json.dumps(await self.store.get(self.run['id'])))
        Path(OUTPUT).write_text(json.dumps({'checks':['real-service-token-revocation-retains-approval','new-service-token-same-invocation-resumes','real-MCP-tools-call-once','real-KV-delete-during-MCP-initialize-blocks-tools-call','work-revocation-persisted','KV-undelete-cannot-revive-old-work','source-token-absent-from-run'],'actual_tools_calls':len(self.calls)},indent=2)+'\n')

def main():
    global EXTERNAL,ADMIN,ISSUE,SINK,MOUNT,KEY,OUTPUT,SOURCE_VALUE
    p=argparse.ArgumentParser();p.add_argument('--fixture-directory',required=True);p.add_argument('--output',required=True);args=p.parse_args();OUTPUT=args.output
    d=Path(args.fixture_directory);containers=json.loads((d/'containers.json').read_text());info=json.loads(subprocess.check_output(['docker','inspect',containers['server']],text=True))[0]
    assert info['Config']['Labels'].get('dios.purpose')=='development-secrets'
    EXTERNAL=json.loads((d/'upstream.json').read_text());base=EXTERNAL['provider']['secret_store']['address'];assert base=='http://127.0.0.1:13188';assert EXTERNAL['reference']['expires_at']>time.time()+120
    root_token=json.loads((d/'fixture.json').read_text())['root_token'];MOUNT=EXTERNAL['reference']['secret_ref']['mount'];KEY='qualification-'+uuid.uuid4().hex;tokens=[]
    def admin(method,path,body=None):
        with httpx.Client(trust_env=False,timeout=5) as client:
            response=client.request(method,base+'/v1/'+path,json=body,headers={'X-Vault-Token':root_token});assert response.is_success,(path,response.status_code);return response.json() if response.content else {}
    ADMIN=admin;value=OpenBaoSecrets(EXTERNAL['provider']['secret_store']).read(EXTERNAL['reference']['secret_ref'])
    SOURCE_VALUE=value
    with tempfile.TemporaryDirectory(prefix='dios-managed-') as temp:
        SINK=Path(temp)/'token'
        def issue():
            token=ADMIN('POST','auth/token/create',{'policies':[KEY],'no_default_policy':True,'ttl':'5m'})['auth']['client_token'];tokens.append(token);SINK.write_text(token);SINK.chmod(0o600)
        ISSUE=issue
        try:
            ADMIN('POST',f'{MOUNT}/data/{KEY}',{'options':{'cas':0},'data':{'value':value}})
            client=EXTERNAL['provider']['client_secret_ref'];ADMIN('PUT','sys/policies/acl/'+KEY,{'policy':f'path "{MOUNT}/data/{KEY}" {{ capabilities = ["read"] }}\npath "{client["mount"]}/data/{client["path"]}" {{ capabilities = ["read"] }}'})
            ISSUE();EXTERNAL['provider']['secret_store']['token_file']=str(SINK);EXTERNAL['reference']['secret_ref'].update(path=KEY,version=1)
            result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ManagedIntegration))
            if not result.wasSuccessful():raise SystemExit(1)
        finally:
            for token in tokens:ADMIN('POST','auth/token/revoke',{'token':token})
            ADMIN('DELETE',f'{MOUNT}/metadata/{KEY}');ADMIN('DELETE','sys/policies/acl/'+KEY)
if __name__=='__main__':main()
