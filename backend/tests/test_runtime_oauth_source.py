"""Source-token validation and E2AG revocation; no real user credentials."""
import asyncio,hashlib,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from app.runtime.oauth_source import OAuthSource,validate_provider,private_value
from app.runtime.database import RuntimeErrorCode
from app.runtime.principals import FilePrincipals
from app.runtime.worker import Worker
from app.runtime.authorization import WorkAuthorizationContext
from test_runtime_worker import FakeAdapter
import test_runtime_principals as principals

class SourceFixture:
    def configure(self,root):
        self.access='synthetic-source-access-token-'+('x'*64);self.secret='synthetic-client-secret'
        self.access_file=root/'upstream.token';self.access_file.write_text(self.access);self.access_file.chmod(0o600)
        self.secret_file=root/'upstream.client';self.secret_file.write_text(self.secret);self.secret_file.chmod(0o600)
        self.expiry=int(time.time())+900
        self.config={'issuer':'https://issuer.example','audience':'dios-api','endpoint':'https://issuer.example/introspect','client_id':'dios-api','client_secret_file':str(self.secret_file),'timeout_seconds':.2}
        self.reference={'ref':'identity/alice','version':'v1','path':str(self.access_file),'sha256':hashlib.sha256(self.access.encode()).hexdigest(),
            'issuer':self.config['issuer'],'audience':'dios-api','subject':'upstream-alice','client_id':'portal','expires_at':self.expiry}
        self.response={'active':True,'iss':self.config['issuer'],'aud':['dios-api'],'sub':'upstream-alice','client_id':'portal','exp':self.expiry}
        self.status=200;self.requests=[];self.delay=0
        async def handle(request):
            self.requests.append(request);await asyncio.sleep(self.delay)
            return httpx.Response(self.status,json=self.response,headers={'Location':'https://untrusted.example'})
        self.transport=httpx.MockTransport(handle)

class OAuthSourceTests(SourceFixture,unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.configure(Path(self.tmp.name));self.source=OAuthSource(self.config,transport=self.transport)
    def test_authlib_client_auth_and_source_claims(self):
        result=self.source.current(self.reference);self.assertEqual(result['subject'],'upstream-alice')
        from urllib.parse import parse_qs
        request=self.requests[0];self.assertEqual(parse_qs(request.content.decode())['token'],[self.access]);self.assertTrue(request.headers['authorization'].startswith('Basic '))
        self.assertNotIn(self.access,json.dumps(result));self.assertNotIn(self.secret,json.dumps(result))
    def test_claim_mismatch_and_inactive_source_are_revocations(self):
        for changes in ({'active':False},{'iss':'https://other'},{'sub':'other'},{'aud':['other']},{'client_id':'other'},{'exp':self.expiry-1},{'nbf':time.time()+60}):
            original=self.response;self.response=dict(original,**changes)
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):self.source.current(self.reference)
            self.response=original
    def test_failure_malformed_redirect_and_timeout_are_unavailable(self):
        for status in (302,401,429,500):
            self.status=status
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.source.current(self.reference)
        self.status=200
        for response in ({'active':'true'},[],{'padding':'x'*65536}):
            self.response=response
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.source.current(self.reference)
        self.delay=.3
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):self.source.current(self.reference)
    def test_expired_changed_and_unprotected_reference_denied(self):
        self.reference['expires_at']=time.time()-1
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_EXPIRED'):self.source.current(self.reference)
        self.reference['expires_at']=self.expiry;self.access_file.write_text('changed')
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):self.source.current(self.reference)
        self.access_file.chmod(0o644)
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):private_value(self.access_file)
        target=self.access_file.with_suffix('.link');target.symlink_to(self.access_file)
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):private_value(target)
    def test_endpoint_constraints(self):
        for endpoint in ('http://remote.example/introspect','https://u:p@issuer.example/introspect','https://issuer.example/introspect?token=x'):
            with self.assertRaises(ValueError):validate_provider(dict(self.config,endpoint=endpoint))

class OAuthPrincipalTests(SourceFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await principals.PrincipalTests.asyncSetUp(self);self.configure(self.root)
        self.directory['oauth2']=self.config;self.directory['principals']['a']['upstream']=self.reference
        self.directory['principals']['a']['tokens']=[];self.tokens['a']=self.access;self.save()
        actual=OAuthSource;transport=self.transport
        self.override=patch('app.runtime.oauth_source.OAuthSource',side_effect=lambda config:actual(config,transport=transport));self.override.start()
    async def asyncTearDown(self):self.override.stop();await principals.PrincipalTests.asyncTearDown(self)
    save=principals.PrincipalTests.save
    headers_for=principals.PrincipalTests.headers_for
    submit_as=principals.PrincipalTests.submit_as
    get=principals.PrincipalTests.get
    finish=principals.PrincipalTests.finish
    async def context(self,run):
        store=self.console.store;await store.register_worker('worker','instance','host',123);attempt=await store.claim('worker','instance');raw=await store.get(run['id'])
        return WorkAuthorizationContext('one',run['id'],attempt['id'],'worker','instance',attempt['epoch'],attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
    async def test_external_identity_submit_worker_result_and_no_token_in_record(self):
        identity=(await self.get('a','identity')).json();self.assertEqual(identity['principal']['upstream']['subject'],'upstream-alice')
        run=await self.submit_as('a');self.assertLessEqual(run['authorization']['expires_at'],self.expiry)
        await self.finish();raw=await self.console.store.get(run['id']);serialized=json.dumps(raw)
        self.assertNotIn(self.access,serialized);self.assertNotIn(self.secret,serialized);self.assertNotIn(str(self.access_file),serialized)
    async def test_database_clock_ahead_does_not_extend_source_deadline(self):
        from unittest.mock import AsyncMock
        with patch.object(self.console.db,'now',new=AsyncMock(return_value=time.time()+120)):
            run=await self.submit_as('a')
        self.assertLessEqual(run['authorization']['expires_at'],self.expiry)
    async def test_provider_outage_retains_grant_and_recovers(self):
        run=await self.submit_as('a');context=await self.context(run);await self.verifier(context,'execute')
        self.status=503
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):await self.verifier(context,'execute')
        self.assertEqual((await self.get('a','identity')).status_code,503)
        self.status=200;await self.verifier(context,'execute');self.assertEqual((await self.get('a','identity')).status_code,200)
    async def test_revocation_persists_even_if_upstream_reappears(self):
        run=await self.submit_as('a');context=await self.context(run);await self.verifier(context,'execute');self.response['active']=False
        for action in ('execute','accept_result'):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(context,action)
        self.response['active']=True
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.verifier(context,'execute')
    async def test_cancel_rechecks_source_off_event_loop(self):
        run=await self.submit_as('a');response=await self.client.post('/api/os/runtime/runs/'+run['id']+'/cancel',headers=self.headers_for('a'),json={'expected_version':run['version']})
        self.assertEqual(response.status_code,200,response.text)
    async def test_trusted_provider_change_invalidates_old_binding(self):
        run=await self.submit_as('a');context=await self.context(run);await self.verifier(context,'execute')
        self.directory['oauth2']['endpoint']='https://changed.example/introspect';self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(context,'execute')
    async def test_version_change_invalidates_old_binding(self):
        run=await self.submit_as('a');context=await self.context(run);await self.verifier(context,'execute')
        self.directory['principals']['a']['upstream']['version']='v2';self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(context,'execute')

class OAuthToolTests(SourceFixture,unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_runtime_tool_invocations as toolfixtures
        await toolfixtures.ToolInvocationTests.asyncSetUp(self);self.configure(self.root)
        config=json.loads(self.policy.path.read_text());self.identity_file=self.root/'source-principals.json'
        directory={'version':1,'issuer':'trusted:identity','audience':'dios-runtime','oauth2':self.config,'principals':{'a':{
            'subject':'subject:a','actor':'actor:portal','scope':self.scope,'enabled':True,'revision':1,'expires_at':self.expiry,
            'tokens':[],'upstream':self.reference,'profiles':{'test-profile':{'actions':['run.submit','run.read'],
            'permissions':config['profiles']['test-profile']['permissions']['caller']}},'objects':{}}}}
        self.identity_file.write_text(json.dumps(directory));self.identity_file.chmod(0o600);config['identity_catalog']=str(self.identity_file);self.policy.path.write_text(json.dumps(config))
        actual=OAuthSource;transport=self.transport
        self.override=patch('app.runtime.oauth_source.OAuthSource',side_effect=lambda config:actual(config,transport=transport));self.override.start()
        principal=await asyncio.to_thread(FilePrincipals(self.identity_file).authenticate,self.access,self.scope)
        descriptor=await asyncio.to_thread(self.policy.admission,'test-profile',self.spec.fingerprint(),[],principal=principal)
        self.run=await self.store.submit('source-run',{'runtime_profile':'test-profile','profile_digest':self.spec.fingerprint(),'secret_refs':[],'principal':principal},authorization=descriptor)
        await self.store.register_worker('oauth-worker','oauth-instance','host',os.getpid());self.attempt=await self.store.claim('oauth-worker','oauth-instance')
        self.binding=(self.run['id'],self.attempt['id'],'oauth-worker','oauth-instance',self.attempt['epoch'])
        self.context=WorkAuthorizationContext(self.scope,*self.binding[:2],'oauth-worker','oauth-instance',self.attempt['epoch'],self.attempt['authorization_epoch'],None,self.run['input']['work_authorization_ref'])
        await self.verifier(self.context,'execute');await self.store.report(*self.binding,1,'preparing');await self.store.report(*self.binding,2,'creating_session')
        from dataclasses import replace
        self.context=replace(self.context,session_id='session-one');await self.verifier(self.context,'execute');await self.store.report(*self.binding,3,'waiting_model',session_id='session-one')
        # SourceFixture uses transport for OAuth; the action gate retains its independent recording transport.
        self.action_transport=self.gate.transport
    async def asyncTearDown(self):
        import test_runtime_tool_invocations as toolfixtures
        self.override.stop();await toolfixtures.ToolInvocationTests.asyncTearDown(self)
    async def approved(self):
        item=await self.gate.request(self.run['id'],self.attempt['id'],'session-one','search',{'query':'evidence'},'source-call')
        return await self.gate.decide(self.run['id'],item['id'],item['version'],'approved','fixture-approval')
    async def test_source_allows_approved_tool(self):
        item=await self.approved();result=await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.action_transport.calls),1)
    async def test_source_revoked_after_approval_blocks_tool(self):
        item=await self.approved();self.response['active']=False
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.action_transport.calls,[]);saved=await self.gate.get(self.run['id'],item['id']);self.assertEqual(saved['approval_state'],'approved')
        self.response['active']=True
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
    async def test_outage_preserves_approval_and_same_call_resumes(self):
        item=await self.approved();self.status=503
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PROVIDER_UNAVAILABLE'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.action_transport.calls,[]);saved=await self.gate.get(self.run['id'],item['id']);self.assertEqual(saved['approval_state'],'approved')
        self.status=200;result=await self.gate.dispatch(self.run['id'],item['id'],item['version']);self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.action_transport.calls),1)
    async def test_revocation_during_transport_initialization_blocks_tool(self):
        item=await self.approved()
        async def revoke():self.response['active']=False
        self.action_transport.hook=revoke
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.gate.dispatch(self.run['id'],item['id'],item['version'])
        self.assertEqual(self.action_transport.calls,[])
