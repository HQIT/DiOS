"""Deferred gateway regressions: fake tool transport; no model or Docker execution."""
import hashlib
import asyncio
import subprocess
import ssl
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
from sqlalchemy import select
import test_runtime_tool_invocations as fixtures
from app.runtime.database import RuntimeErrorCode
from app.runtime.worker_identity import WorkerIdentities, PROTOCOL, peer_identity
from app.runtime.remote_tool_gateway import ToolGateway
from app.runtime.worker_tool_channel import POLICY_ENV
from app.runtime.tool_schema import evidence


class RemoteToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.ToolInvocationTests();await self.f.asyncSetUp();f=self.f
        self.token='a'*43;self.cert='b'*64;now=time.time()
        self.row=dict(certificate_sha256=self.cert,token_sha256=hashlib.sha256(self.token.encode()).hexdigest(),
            subject='worker:fixture',worker_id='worker',enabled=True,not_before=now-1,expires_at=now+600,
            bindings=[{'scope':f.scope,'profile':'test-profile'}])
        self.identities_file=f.root/'identities.json';self.write_identity()
        self.identities=WorkerIdentities(self.identities_file,'tools:test')
        self.profiles_file=f.root/'gateway-profiles.json'
        self.entry={'enabled':True,'path':'/fixture/profile.json'}
        self.profiles_file.write_text(json.dumps({'version':1,'profiles':{'test-profile':self.entry}}))
        policy={'tools':{'search':'search'}}
        self.spec=replace(f.spec,environment={POLICY_ENV:json.dumps(policy)})
        # Only the public profile fingerprint is stubbed; ownership and E2AG remain real fixtures.
        self.profile_patch=patch('app.runtime.worker.read_profile',return_value=('test-profile',self.spec));self.profile_patch.start()
        self.fingerprint_patch=patch.object(type(self.spec),'fingerprint',return_value=f.run['input']['profile_digest']);self.fingerprint_patch.start()
        self.gateway=ToolGateway(f.gate,self.identities,self.profiles_file)

    async def asyncTearDown(self):
        self.fingerprint_patch.stop();self.profile_patch.stop();await self.f.asyncTearDown()

    def write_identity(self):
        self.identities_file.write_text(json.dumps({'version':1,'audience':'tools:test','workers':[self.row]}))

    def envelope(self,method='describe',params=None):
        f=self.f
        return dict(protocol=PROTOCOL,audience='tools:test',scope=f.scope,profile='test-profile',
            profile_digest=f.run['input']['profile_digest'],binding=list(f.binding),
            access_token=self.token,message={'method':method,'params':params or {}})

    async def call(self,method='describe',params=None):
        return await self.gateway.handle(self.cert,time.time()+3600,self.envelope(method,params))

    async def test_certificate_token_audience_and_scope_all_required(self):
        for key,value in [('access_token','z'*43),('audience','other'),('scope','other')]:
            envelope=self.envelope();envelope[key]=value
            with self.assertRaises(RuntimeErrorCode):await self.gateway.handle(self.cert,time.time()+3600,envelope)
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_DENIED'):
            await self.gateway.handle('c'*64,time.time()+3600,self.envelope())
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CERTIFICATE_EXPIRED'):
            await self.gateway.handle(self.cert,time.time()-1,self.envelope())
        self.assertEqual(self.f.transport.calls,[])

    async def test_instance_and_epoch_are_bound_to_original_attempt(self):
        for index,value in [(3,'another-instance'),(4,999)]:
            envelope=self.envelope();envelope['binding'][index]=value
            with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):
                await self.gateway.handle(self.cert,time.time()+3600,envelope)

    async def test_no_approval_api_and_no_caller_tool_allowlist(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_CHANNEL_METHOD_DENIED'):await self.call('approve',{})
        envelope=self.envelope();envelope['tools']={'evil':'evil'}
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_GATEWAY_FRAME_INVALID'):
            await self.gateway.handle(self.cert,time.time()+3600,envelope)

    async def test_registered_request_approval_dispatch_and_replay_keep_same_identity(self):
        f=self.f;params={'tool':'search','arguments':{'query':'evidence'},'key':'one'}
        item=await self.call('request',params)
        self.assertEqual(item['state'],'awaiting_approval')
        await f.approve(item)
        result=await self.call('advance',{'invocation_id':item['id']})
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['result']['content'][0]['text'],'中文结果🙂')
        self.assertEqual((await self.call('request',params))['id'],item['id'])
        self.assertEqual(len(f.transport.calls),1)
        async with f.db.read(f.scope) as c:
            decision=(await c.execute(select(evidence.c.decision).where(evidence.c.invocation_id==item['id'],evidence.c.stage=='dispatch'))).scalar_one()
        self.assertEqual(decision['evidence']['sender']['certificate_sha256'],self.cert)
        self.assertNotIn(self.token,json.dumps(decision))

    async def test_token_revocation_during_mcp_initialization_blocks_actual_call(self):
        f=self.f;item=await self.call('request',{'tool':'search','arguments':{'query':'evidence'},'key':'one'})
        await f.approve(item)
        async def revoke():self.row['enabled']=False;self.write_identity()
        f.transport.hook=revoke
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_EXPIRED_OR_REVOKED'):
            await self.call('advance',{'invocation_id':item['id']})
        self.assertEqual(f.transport.calls,[])

    async def test_profile_disable_during_initialization_blocks_actual_call(self):
        f=self.f;item=await self.call('request',{'tool':'search','arguments':{'query':'evidence'},'key':'one'})
        await f.approve(item)
        async def disable():
            self.profiles_file.write_text(json.dumps({'version':1,'profiles':{'test-profile':{**self.entry,'enabled':False}}}))
        f.transport.hook=disable
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_PROFILE_CHANGED'):
            await self.call('advance',{'invocation_id':item['id']})
        self.assertEqual(f.transport.calls,[])

    async def test_lost_response_reuses_receipt_without_new_call(self):
        f=self.f;item=await self.call('request',{'tool':'search','arguments':{'query':'evidence'},'key':'one'})
        await f.approve(item)
        await self.call('advance',{'invocation_id':item['id']})  # discard delivered value
        result=await self.call('advance',{'invocation_id':item['id']})
        self.assertEqual(result['state'],'succeeded');self.assertEqual(len(f.transport.calls),1)
        self.assertTrue(f.gate.receipts.available(item['id']))

    async def test_short_token_and_process_identity_rotation(self):
        args=(self.cert,self.f.scope,'test-profile','worker:'+('d'*32),self.token)
        self.assertEqual(self.identities.authorize(*args)['subject'],'worker:fixture')
        self.row['expires_at']=self.row['not_before']+901;self.write_identity()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_TOKEN_LIFETIME_INVALID'):self.identities.authorize(*args)

    async def test_busy_rejects_before_creating_calls_and_old_connection_is_not_tls_identity(self):
        self.gateway.active['worker']='other'
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_GATEWAY_BUSY'):await self.call()
        class PlainWriter:
            def get_extra_info(self,name):return None
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_TLS_REQUIRED'):peer_identity(PlainWriter())

    async def test_secret_partition_is_pinned_in_profile_fingerprint(self):
        from app.runtime.worker_tool_channel import profile_policy
        from app.runtime.adapter import LaunchSpec
        plain={'tools':{'search':'search'}}
        changed={**plain,'worker_secret_refs':['model/test']}
        self.assertEqual(profile_policy(changed)['worker_secret_refs'],['model/test'])
        # Compute the actual dataclass fingerprint without the gateway fixture patch.
        self.fingerprint_patch.stop()
        a=LaunchSpec('test',('test',),environment={POLICY_ENV:json.dumps(plain)})
        b=replace(a,environment={POLICY_ENV:json.dumps(changed)})
        self.assertNotEqual(a.fingerprint(),b.fingerprint())
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_CHANNEL_PROFILE_INVALID'):
            profile_policy({**plain,'worker_secret_refs':['same','same']})

    async def test_actual_mutual_tls_roundtrip_and_hostname_rejection(self):
        from app.runtime.worker_identity import tls_context
        from app.runtime.remote_tool_channel import RemoteToolProvider
        root=self.f.root/'tls';root.mkdir()
        def certs():
            def command(*args):subprocess.run(['openssl',*args],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            command('req','-x509','-newkey','rsa:2048','-nodes','-keyout','ca.key','-out','ca.pem','-subj','/CN=DiOS Test CA','-days','1')
            for name,usage in [('server','serverAuth'),('client','clientAuth')]:
                command('req','-newkey','rsa:2048','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
                (root/(name+'.ext')).write_text('extendedKeyUsage='+usage+'\nsubjectAltName=DNS:localhost\n')
                command('x509','-req','-in',name+'.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial',
                    '-out',name+'.pem','-days','1','-extfile',name+'.ext')
        await asyncio.to_thread(certs)
        self.cert=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'client.pem').read_text())).hexdigest()
        self.row['certificate_sha256']=self.cert;self.write_identity()
        context=tls_context(str(root/'ca.pem'),str(root/'server.pem'),str(root/'server.key'),server=True)
        stop=asyncio.Event();server=asyncio.create_task(self.gateway.serve('127.0.0.1',0,context,stop))
        try:
            async with asyncio.timeout(5):
                while self.gateway.server is None:await asyncio.sleep(.01)
            port=self.gateway.server.sockets[0].getsockname()[1]
            token=root/'token';token.write_text(self.token)
            config=dict(version=1,enabled=True,worker_id='worker',audience='tools:test',host='127.0.0.1',
                server_name='localhost',port=port,ca_file=str(root/'ca.pem'),certificate_file=str(root/'client.pem'),
                key_file=str(root/'client.key'),token_file=str(token))
            path=root/'client.json';path.write_text(json.dumps(config))
            provider=RemoteToolProvider(path,self.f.store,'test-profile',self.spec)
            value=await provider.request(self.f.binding,{'method':'describe','params':{}})
            self.assertEqual(value['tools'][0]['name'],'search')
            item=await provider.request(self.f.binding,{'method':'request','params':{'tool':'search','arguments':{'query':'evidence'},'key':'tls-call'}})
            await self.f.approve(item)
            result=await provider.request(self.f.binding,{'method':'advance','params':{'invocation_id':item['id']}})
            self.assertEqual(result['state'],'succeeded');self.assertEqual(len(self.f.transport.calls),1)
            provider.config['server_name']='wrong-host.invalid'
            with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_GATEWAY_OBSERVATION_UNAVAILABLE'):
                await provider.request(self.f.binding,{'method':'describe','params':{}})
            provider.config['server_name']='localhost'
            token.write_text('z'*43)
            with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_DENIED'):
                await provider.request(self.f.binding,{'method':'describe','params':{}})
            # The same real TLS endpoint also issues and rotates automatic credentials.
            from app.runtime.worker_credentials import WorkerCredentials
            import secrets
            machine={k:v for k,v in self.row.items() if k!='token_sha256'}
            machine.update(issuance_enabled=True,revision=1,expires_at=time.time()+3600)
            self.identities_file.write_text(json.dumps({'version':1,'audience':'tools:test','machines':[machine]}))
            issuer_key=root/'issuer.key';issuer_key.write_bytes(secrets.token_bytes(32));issuer_key.chmod(0o600)
            self.gateway.credentials=WorkerCredentials(self.f.store,self.identities,self.f.policy,issuer_key,ttl=60)
            config.pop('token_file');config['credential_mode']='automatic';path.write_text(json.dumps(config))
            automatic=RemoteToolProvider(path,self.f.store,'test-profile',self.spec)
            await automatic.request(self.f.binding,{'method':'describe','params':{}})
            cache=automatic.credentials[tuple(self.f.binding)];self.assertEqual(cache['value']['version'],1)
            cache['refresh_at']=0
            await automatic.request(self.f.binding,{'method':'describe','params':{}})
            self.assertEqual(cache['value']['version'],2)
            await self.gateway.credentials.revoke(cache['value']['credential_id'],2)
            with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CREDENTIAL_REVOKED'):
                await automatic.request(self.f.binding,{'method':'describe','params':{}})
        finally:
            stop.set();await asyncio.wait_for(server,15)
