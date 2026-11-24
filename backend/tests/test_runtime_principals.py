"""Runtime principal, object access and live E2AG revocation regressions."""
import asyncio
from dataclasses import replace
import hashlib
import json
import secrets
import time
import unittest
from unittest.mock import patch
from app.runtime.principals import FilePrincipals
from app.runtime.worker import read_profile,Worker
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.authorization import WorkAuthorizationContext
from app.runtime.database import RuntimeErrorCode
from app.middleware.access_token import AccessTokenMiddleware
from work_auth_fixtures import policy_fixture
from test_runtime_worker import FakeAdapter
import test_runtime_task_console as fixtures

class PrincipalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.TaskConsoleTests.asyncSetUp(self)
        self.profile['governance']={'mode':'e2ag'}
        path=self.profiles/'review.json';path.write_text(json.dumps(self.profile));_,self.spec=read_profile(path)
        self.policy,descriptor=policy_fixture(self.root,'review',self.spec)
        self.identities=self.root/'principals.json';self.tokens={k:secrets.token_urlsafe(32) for k in ('a','b','c')}
        now=time.time();permissions=descriptor['permissions']['caller']
        self.directory={'version':1,'issuer':'fixture:issuer','audience':'dios-runtime','principals':{k:{
            'subject':'subject:'+k,'actor':'service:'+k,'scope':'one','enabled':True,'revision':1,'expires_at':now+86400,
            'tokens':[{'sha256':hashlib.sha256(token.encode()).hexdigest(),'enabled':True,'not_before':now-10,'expires_at':now+3600}],
            'profiles':{'review':{'actions':['run.submit','run.read','run.cancel','run.revoke','artifact.read','tool.read'],
                'permissions':permissions}},'objects':{}} for k,token in self.tokens.items()}}
        self.save();config=json.loads(self.policy.path.read_text());config['identity_catalog']=str(self.identities);self.policy.path.write_text(json.dumps(config))
        self.console.policy=self.policy;self.verifier=WorkAuthorizationVerifier(self.console.store,self.policy)
        self.app.add_middleware(AccessTokenMiddleware)
    asyncTearDown=fixtures.TaskConsoleTests.asyncTearDown
    def save(self):self.identities.write_text(json.dumps(self.directory));self.identities.chmod(0o600)
    def headers_for(self,k):return {'Authorization':'Bearer '+self.tokens[k]}
    async def submit_as(self,k,key='same'):
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for(k),json={
            'profile_id':'review','sample_id':'first-item','goal':'review','idempotency_key':key})
        self.assertEqual(response.status_code,202,response.text);return response.json()
    async def get(self,k,path):return await self.client.get('/api/os/runtime/'+path,headers=self.headers_for(k))
    async def finish(self):
        adapter=FakeAdapter();result=await Worker(self.console.store,adapter,'review',self.spec,self.console.provider,authorization_verifier=self.verifier).once()
        self.assertEqual(result['state'],'succeeded');return result

    async def test_separate_identity_keys_and_queries_with_real_middleware(self):
        a=await self.submit_as('a');b=await self.submit_as('b');self.assertNotEqual(a['id'],b['id'])
        self.assertEqual((await self.submit_as('a'))['id'],a['id'])
        self.assertEqual(a['authorization']['subject'],'subject:a')
        for k,own,other in [('a',a,b),('b',b,a)]:
            page=(await self.get(k,'runs')).json();self.assertEqual([r['id'] for r in page['items']],[own['id']])
            self.assertEqual((await self.get(k,'runs/'+other['id'])).status_code,404)
            self.assertEqual((await self.get(k,'workers')).status_code,403)
        operator=await self.client.get('/api/os/runtime/runs',headers=self.headers)
        self.assertEqual(operator.json()['items'],[])
        self.assertEqual((await self.client.get('/api/os/runtime/runs/'+a['id'],headers=self.headers)).status_code,404)
        # Non-runtime APIs retain the separate operator gate.
        self.assertEqual((await self.client.get('/api/os/agents',headers=self.headers_for('a'))).status_code,401)

    async def test_same_digest_artifacts_require_run_link_and_current_share(self):
        a=await self.submit_as('a');await self.finish();b=await self.submit_as('b');await self.finish()
        aa=(await self.get('a','runs/'+a['id'])).json()['artifacts'][0]
        bb=(await self.get('b','runs/'+b['id'])).json()['artifacts'][0]
        self.assertEqual(aa['digest'],bb['digest'])
        path='runs/'+a['id']+'/artifacts/'+aa['id']+'/download'
        self.assertEqual((await self.get('b',path)).status_code,404)
        self.directory['principals']['b']['objects'][a['id']]=['run.read','artifact.read'];self.save()
        self.assertEqual((await self.get('b',path)).status_code,200)
        wrong='runs/'+b['id']+'/artifacts/'+aa['id']+'/download'
        self.assertEqual((await self.get('b',wrong)).status_code,404)
        self.directory['principals']['b']['objects'].clear();self.save()
        self.assertEqual((await self.get('b',path)).status_code,404)
        self.assertEqual((await self.get('b','runs/'+a['id'])).status_code,404)

    async def test_rotation_revokes_old_token_without_renaming_task(self):
        a=await self.submit_as('a');old=self.tokens['a'];self.tokens['a']=secrets.token_urlsafe(32)
        self.directory['principals']['a']['tokens'][0]['sha256']=hashlib.sha256(self.tokens['a'].encode()).hexdigest();self.save()
        denied=await self.client.get('/api/os/runtime/runs',headers={'Authorization':'Bearer '+old});self.assertEqual(denied.status_code,401)
        self.assertEqual((await self.get('a','runs/'+a['id'])).status_code,200)
        await self.finish()

    async def test_queued_revocation_blocks_execution(self):
        await self.submit_as('a');self.directory['principals']['a']['enabled']=False;self.save()
        adapter=FakeAdapter();result=await Worker(self.console.store,adapter,'review',self.spec,self.console.provider,authorization_verifier=self.verifier).once()
        self.assertEqual(adapter.started,0);self.assertEqual(result['state'],'failed')
        self.assertEqual((await self.get('a','runs')).status_code,401)

    async def test_inflight_revocation_blocks_renewal_and_result(self):
        a=await self.submit_as('a');store=self.console.store
        await store.register_worker('worker','instance','host',123)
        attempt=await store.claim('worker','instance');raw=await store.get(a['id'])
        context=WorkAuthorizationContext('one',a['id'],attempt['id'],'worker','instance',attempt['epoch'],attempt['authorization_epoch'],None,raw['input']['work_authorization_ref'])
        await self.verifier(context,'execute')
        self.directory['principals']['a']['revision']+=1;self.save()
        for action in ('execute','accept_result'):
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await self.verifier(context,action)
        self.directory['principals']['a']['revision']=1;self.save()
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.verifier(context,'execute')

    async def test_scope_expiry_audience_and_permission_narrowing(self):
        self.directory['principals']['a']['scope']='different';self.save();self.assertEqual((await self.get('a','identity')).status_code,401)
        self.directory['principals']['a']['scope']='one';self.directory['principals']['a']['tokens'][0]['expires_at']=time.time()-1;self.save()
        self.assertEqual((await self.get('a','identity')).status_code,401)
        self.directory['audience']='other-service';self.save();self.assertEqual((await self.get('b','identity')).status_code,503)
        self.directory['audience']='dios-runtime';self.directory['principals']['b']['profiles']['review']['permissions']=[];self.save()
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for('b'),json={'profile_id':'review','sample_id':'first-item','goal':'review','idempotency_key':'x'})
        self.assertNotEqual(response.status_code,202)

    async def test_download_revocation_stops_following_chunks(self):
        a=await self.submit_as('a');await self.finish()
        provider=FilePrincipals(self.identities);view=self.console.for_principal(provider.authenticate(self.tokens['a'],'one'))
        blob=(await view.detail(a['id']))['artifacts'][0];record=await view.artifact(a['id'],blob['id'])
        with patch.object(view.provider,'read',return_value=iter([b'first',b'second'])):
            stream=view.stream_artifact(a['id'],record);self.assertEqual(await anext(stream),b'first')
            self.directory['principals']['a']['enabled']=False;self.save()
            with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_PRINCIPAL_REVOKED'):await anext(stream)

    async def test_cannot_forge_identity_or_cancel_shared_read_only_run(self):
        a=await self.submit_as('a')
        self.directory['principals']['b']['objects'][a['id']]=['run.read'];self.save()
        response=await self.client.post('/api/os/runtime/runs/'+a['id']+'/cancel',headers=self.headers_for('b'),json={'expected_version':a['version']})
        self.assertEqual(response.status_code,404)
        self.assertFalse((await self.console.store.get(a['id']))['cancel_requested'])
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for('b'),json={'profile_id':'review','sample_id':'first-item','goal':'x','idempotency_key':'other','principal':{'id':'a'}})
        self.assertEqual(response.status_code,422)
        self.directory['principals']['b']['profiles'].clear();self.save()
        self.assertEqual((await self.get('b','profiles')).json()['items'],[])
        self.assertEqual((await self.get('b','samples')).json()['items'],[])

    async def test_submit_requires_ability_to_observe_accepted_task(self):
        self.directory['principals']['a']['profiles']['review']['actions']=['run.submit'];self.save()
        response=await self.client.post('/api/os/runtime/runs',headers=self.headers_for('a'),json={'profile_id':'review','sample_id':'first-item','goal':'x','idempotency_key':'hidden'})
        self.assertEqual(response.status_code,403)
        self.assertEqual((await self.console.list())['items'],[])

    async def test_no_credential_or_profile_secret_in_identity_response(self):
        response=await self.get('a','identity');self.assertEqual(response.status_code,200)
        for value in [self.tokens['a'],'sha256','PRIVATE_EXAMPLE','test-command','permissions']:
            self.assertNotIn(value,response.text)
