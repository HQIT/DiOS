"""Remote control semantics, fault recovery and actual TLS/Docker delivery."""
import asyncio
import base64
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import select
import test_runtime_worker as fixtures
from work_auth_fixtures import policy_fixture
from app.runtime.database import Database,RuntimeErrorCode
from app.runtime.store import RuntimeStore
from app.runtime.remote_control import RemoteControl
from app.runtime.remote_runtime import RemoteRuntimeStore
from app.runtime.worker_identity import WorkerIdentities,tls_context
from app.runtime.worker import Worker,read_profile
from app.runtime.artifacts import LocalArtifacts
from app.runtime.schema import attempts,artifacts
from app.runtime.remote_schema import uploads,chunks,receipts
from app.runtime.lifecycle import Lifecycle
from app.runtime.recovery import backup_sqlite,verify_backup


class RemoteControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.WorkerTests();await self.f.asyncSetUp();f=self.f
        self.addAsyncCleanup(self.f.asyncTearDown)
        self.profile=f.root/'profile.json'
        value=json.loads((fixtures.ROOT/'examples/runtime/fixture-profile.json').read_text())
        value.update(id='test-profile',governance={'mode':'e2ag'})
        self.profile.write_text(json.dumps(value));_,f.spec=read_profile(self.profile)
        self.policy,self.descriptor=policy_fixture(f.root,'test-profile',f.spec)
        self.cert='b'*64
        self.machine=dict(enabled=True,issuance_enabled=True,revision=1,subject='worker:test',worker_id='remote',
            certificate_sha256=self.cert,not_before=time.time()-1,expires_at=time.time()+3600,
            bindings=[{'scope':f.scope,'profile':'test-profile'}])
        self.identities_file=f.root/'machines.json';self.write_machine()
        self.profiles=f.root/'profiles.json';self.profiles.write_text(json.dumps({'version':1,'profiles':{'test-profile':{'enabled':True,'path':str(self.profile)}}}))
        self.server=RemoteControl(f.store,WorkerIdentities(self.identities_file,'test'),self.profiles,self.policy,f.provider)
        self.client_provider=LocalArtifacts(f.root/'worker-local')
        self.remote=self.client();self.adapter=fixtures.FakeAdapter();self.adapter.name='local-docker-v1'
        self.worker=Worker(self.remote,self.adapter,'test-profile',f.spec,self.client_provider,poll_interval=.05,authorization_verifier=self.remote.verifier)
        self.run=await self.submit('one')
    def write_machine(self):self.identities_file.write_text(json.dumps({'version':1,'audience':'test','machines':[self.machine]}))
    def client(self):
        async def exchange(envelope):return await self.server.handle(self.cert,time.time()+3600,envelope)
        transport=SimpleNamespace(worker_id='remote:'+uuid4().hex,config={'audience':'test'},profile='test-profile',spec=self.f.spec,exchange=exchange)
        return RemoteRuntimeStore(self.f.scope,transport)
    async def submit(self,key):
        return await self.f.store.submit(key,{'runtime_profile':'test-profile','profile_digest':self.f.spec.fingerprint(),
            'secret_refs':[],'payload':{}},authorization=self.descriptor)
    async def claim(self):
        await self.remote.register_worker(self.worker.worker_id,self.worker.instance,'host',os.getpid())
        a=await self.remote.claim(self.worker.worker_id,self.worker.instance,profile_id='test-profile')
        return a,self.remote.bindings[a['run_id']]
    async def test_worker_completes_with_separate_artifacts_and_no_database(self):
        result=await self.worker.once()
        self.assertEqual(result['state'],'succeeded');self.assertFalse(hasattr(self.remote,'db'))
        self.assertNotEqual(self.f.provider.root,self.client_provider.root)
        async with self.f.db.read(self.f.scope) as c:
            linked=(await c.execute(select(artifacts).where(artifacts.c.run_id==self.run['id']))).mappings().all()
        self.assertEqual(len(linked),3)
        for item in linked:self.f.provider.verify(item['storage_key'])
        self.assertEqual(self.adapter.started,1)
    async def test_lost_claim_response_and_duplicate_triggers_do_not_duplicate_attempt(self):
        await self.remote.register_worker(self.worker.worker_id,self.worker.instance,'host',os.getpid())
        original=self.remote.transport.exchange;lost=True
        async def exchange(e):
            nonlocal lost
            value=await original(e)
            if e['message']['method']=='runtime.claim' and lost:lost=False;raise ConnectionError('lost response')
            return value
        self.remote.transport.exchange=exchange
        with self.assertRaises(ConnectionError):await self.remote.claim(self.worker.worker_id,self.worker.instance,profile_id='test-profile')
        a=await self.remote.claim(self.worker.worker_id,self.worker.instance,profile_id='test-profile')
        other=await self.submit('two')
        values=await asyncio.gather(*(self.remote.call('claim',{},uuid4().hex) for _ in range(3)))
        self.assertEqual(values,[None]*3)
        run=await self.f.store.get(self.run['id']);self.assertEqual(len(run['attempts']),1);self.assertEqual(a['id'],run['attempt_id'])
        self.assertEqual((await self.f.store.get(other['id']))['state'],'queued')
    async def test_upload_resume_utf8_and_no_hash_based_acl_bypass(self):
        a,b=await self.claim();data=('中文🙂'*18000).encode();blob=self.client_provider.put([data])
        original=self.remote.transport.exchange;lost=True
        async def exchange(e):
            nonlocal lost
            value=await original(e)
            if e['message']['method']=='runtime.upload.chunk' and lost:lost=False;raise ConnectionError('lost chunk receipt')
            return value
        self.remote.transport.exchange=exchange
        with self.assertRaises(ConnectionError):await self.remote.upload(b,self.client_provider,blob.key)
        await self.remote.upload(b,self.client_provider,blob.key)
        self.assertEqual(b''.join(self.f.provider.read(blob.key)),data)
        received=bytearray()
        while len(received)<len(data):
            part=await self.remote.bound('artifact.read',b,key=blob.key,offset=len(received));received.extend(base64.b64decode(part['data']))
        self.assertEqual(bytes(received),data)
        unlinked=self.f.provider.put([b'private'])
        with self.assertRaisesRegex(RuntimeErrorCode,'UPLOAD_NOT_FOUND'):
            await self.remote.bound('artifact.read',b,key=unlinked.key,offset=0)
    async def test_e2ag_revocation_and_machine_revocation_reject_execution(self):
        a,b=await self.claim();run=await self.f.store.get(self.run['id']);await self.f.store.revoke(run['id'],run['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.remote.heartbeat(*b)
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.remote.bound('authorize',b,action='execute',session_id=None)
        self.machine['enabled']=False;self.write_machine()
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_EXPIRED_OR_REVOKED'):await self.remote.get(run['id'])
    async def test_fenced_epoch_never_accepts_old_report(self):
        a,b=await self.claim()
        async with self.f.db.transaction(self.f.scope) as c:await c.execute(attempts.update().where(attempts.c.id==a['id']).values(lease_until=0))
        await self.f.store.expire()
        with self.assertRaisesRegex(RuntimeErrorCode,'STALE_OWNER'):await self.remote.report(*b,1,'preparing')
        self.assertEqual((await self.f.store.get(self.run['id']))['state'],'reconciliation_required')
    async def test_wrong_instance_and_scope_cannot_control_original_attempt(self):
        a,b=await self.claim();original=self.remote.instance;self.remote.instance='changed'
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_BINDING_CHANGED'):await self.remote.get(self.run['id'])
        self.remote.instance=original;self.remote.scope='other'
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_IDENTITY_SCOPE_DENIED'):await self.remote.get(self.run['id'])
    async def test_disconnected_result_recovers_after_worker_restart_without_relaunch(self):
        original=self.remote.transport.exchange;disconnected=False
        async def exchange(e):
            nonlocal disconnected
            if e['message']['method']=='runtime.upload.begin':disconnected=True
            if disconnected:raise ConnectionError('network down')
            return await original(e)
        self.remote.transport.exchange=exchange
        with self.assertRaises(ConnectionError):await self.worker.once()
        run=await self.f.store.get(self.run['id']);a=run['attempts'][0]
        async with self.f.db.transaction(self.f.scope) as c:await c.execute(attempts.update().where(attempts.c.id==a['id']).values(lease_until=0))
        remote=self.client();worker=Worker(remote,self.adapter,'test-profile',self.f.spec,self.client_provider,
            authorization_verifier=remote.verifier,poll_interval=.05)
        result=await worker.recover()
        self.assertEqual(result[0]['state'],'succeeded');self.assertEqual(self.adapter.started,1)
        run=await self.f.store.get(self.run['id']);self.assertEqual(run['attempt_id'],a['id']);self.assertEqual(run['attempts'][0]['session_id'],'fixture-session')
    async def test_drain_allows_active_completion_and_blocks_new_claim(self):
        original=self.adapter.start
        async def start(*args):
            obs=await original(*args);await Lifecycle(self.f.db,self.f.scope).begin_drain();return obs
        self.adapter.start=start
        result=await self.worker.once();self.assertEqual(result['state'],'succeeded')
        self.assertEqual((await self.worker.once())['state'],'idle')
        self.assertTrue((await Lifecycle(self.f.db,self.f.scope).status())['ready_for_cutover'])
    async def test_backup_contains_incomplete_upload_and_resumes(self):
        if not self.f.url.startswith('sqlite'):self.skipTest('SQLite backup provider')
        a,b=await self.claim();data=b'x'*70000;key=hashlib.sha256(data).hexdigest()
        await self.remote.bound('upload.begin',b,key=key,size=len(data))
        await self.remote.bound('upload.chunk',b,key=key,offset=0,data=base64.b64encode(data[:65536]).decode())
        dest=self.f.root/'backup';await asyncio.to_thread(backup_sqlite,self.f.url,self.f.provider.root,dest)
        manifest=await asyncio.to_thread(verify_backup,dest);self.assertIn('runtime_remote_schema',manifest['schemas'])
        self.server=RemoteControl(self.f.store,self.server.identities,self.profiles,self.policy,self.f.provider)
        self.client_provider.put([data]);await self.remote.upload(b,self.client_provider,key)
        self.assertEqual(b''.join(self.f.provider.read(key)),data)
    async def test_policy_change_rolls_back_claim_and_grant(self):
        config=json.loads(self.policy.path.read_text());config['subject']='operator:changed';self.policy.path.write_text(json.dumps(config))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORK_POLICY_CHANGED'):await self.claim()
        run=await self.f.store.get(self.run['id']);self.assertEqual(run['state'],'queued');self.assertEqual(run['attempts'],[])

    async def test_completed_response_loss_is_recovered_without_duplicate_artifacts(self):
        original=self.remote.transport.exchange;captured=None
        async def exchange(e):
            nonlocal captured
            value=await original(e)
            if e['message']['method']=='runtime.complete':captured=e;raise ConnectionError('lost final response')
            return value
        self.remote.transport.exchange=exchange
        with self.assertRaises(ConnectionError):await self.worker.once()
        self.assertEqual((await self.f.store.get(self.run['id']))['state'],'succeeded')
        self.remote.transport.exchange=original
        first=await original(captured);second=await original(captured);self.assertEqual(first,second)
        async with self.f.db.read(self.f.scope) as c:
            rows=(await c.execute(select(artifacts.c.id).where(artifacts.c.run_id==self.run['id']))).all()
        self.assertEqual(len(rows),3);self.assertEqual(self.adapter.started,1)

    async def test_denial_evidence_survives_operation_rollback(self):
        from app.runtime.work_auth_schema import decisions
        a,b=await self.claim();run=await self.f.store.get(self.run['id']);await self.f.store.revoke(run['id'],run['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.remote.bound('authorize',b,action='execute',session_id=None)
        async with self.f.db.read(self.f.scope) as c:
            rows=(await c.execute(select(decisions.c.decision).where(decisions.c.run_id==run['id'],decisions.c.action=='remote-control'))).scalars().all()
        self.assertEqual(rows[-1]['decision'],'deny')

    async def test_gateway_fences_expired_lease_without_worker_reconnection(self):
        from app.runtime.remote_tool_gateway import ToolGateway
        a,b=await self.claim()
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(lease_until=0))
        gateway=ToolGateway(SimpleNamespace(store=self.f.store),self.server.identities,self.profiles)
        gateway.runtime_control=self.server
        task=asyncio.create_task(gateway.maintain())
        try:
            async with asyncio.timeout(3):
                while (await self.f.store.get(self.run['id']))['state']!='reconciliation_required':await asyncio.sleep(.02)
        finally:
            gateway.stopping=True;task.cancel();await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(len((await self.f.store.get(self.run['id']))['attempts']),1)

    async def actual_tls(self,adapter):
        from app.runtime.remote_tool_gateway import ToolGateway
        from app.runtime.remote_tool_channel import RemoteToolProvider
        root=self.f.root/'tls';root.mkdir()
        def certs():
            def cmd(*args):subprocess.run(['openssl',*args],cwd=root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            cmd('req','-x509','-newkey','rsa:2048','-nodes','-keyout','ca.key','-out','ca.pem','-subj','/CN=Test CA','-days','1')
            for name,usage in [('server','serverAuth'),('client','clientAuth')]:
                cmd('req','-newkey','rsa:2048','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
                (root/(name+'.ext')).write_text('extendedKeyUsage='+usage+'\nsubjectAltName=DNS:localhost\n')
                cmd('x509','-req','-in',name+'.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial','-out',name+'.pem','-days','1','-extfile',name+'.ext')
        await asyncio.to_thread(certs)
        self.cert=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'client.pem').read_text())).hexdigest()
        self.machine['certificate_sha256']=self.cert;self.write_machine()
        gate=ToolGateway(SimpleNamespace(store=self.f.store),self.server.identities,self.profiles);gate.runtime_control=self.server
        stop=asyncio.Event();task=asyncio.create_task(gate.serve('127.0.0.1',0,tls_context(root/'ca.pem',root/'server.pem',root/'server.key',server=True),stop))
        try:
            async with asyncio.timeout(5):
                while gate.server is None:await asyncio.sleep(.01)
            config=dict(version=1,enabled=True,worker_id='remote',audience='test',host='127.0.0.1',server_name='localhost',
                port=gate.server.sockets[0].getsockname()[1],ca_file=str(root/'ca.pem'),certificate_file=str(root/'client.pem'),key_file=str(root/'client.key'),credential_mode='automatic')
            path=root/'client.json';path.write_text(json.dumps(config))
            transport=RemoteToolProvider(path,SimpleNamespace(scope=self.f.scope),'test-profile',self.f.spec)
            remote=RemoteRuntimeStore(self.f.scope,transport)
            worker=Worker(remote,adapter,'test-profile',self.f.spec,self.client_provider,poll_interval=.1,authorization_verifier=remote.verifier)
            result=await worker.serve(once=True);self.assertEqual(result['state'],'succeeded')
            return result
        finally:stop.set();await asyncio.wait_for(task,15)
    async def test_actual_tls_control_roundtrip(self):await self.actual_tls(self.adapter)

    async def test_remote_retry_uses_server_profile_and_same_work_identity(self):
        from app.runtime.fault_schema import circuits,plans
        original=await self.f.store.get(self.run['id']);await self.f.store.cancel(original['id'],original['version'])
        value=json.loads(self.profile.read_text());value['recovery']=dict(resource='shared-account',replay_safe=True,
            max_attempts=3,base_delay=10,max_delay=30,quota_delay=60)
        self.profile.write_text(json.dumps(value));_,self.f.spec=read_profile(self.profile)
        self.policy,self.descriptor=policy_fixture(self.f.root,'test-profile',self.f.spec);self.server.policy=self.policy
        remote=self.client();self.run=await self.submit('retry')
        adapter=fixtures.FakeAdapter(b'{"type":"error","code":"PROVIDER_RATE_LIMITED"}\n');adapter.name='local-docker-v1'
        worker=Worker(remote,adapter,'test-profile',self.f.spec,self.client_provider,authorization_verifier=remote.verifier,poll_interval=.05)
        first=await worker.once();self.assertEqual(first['state'],'retry_wait')
        async with self.f.db.transaction(self.f.scope) as c:
            await c.execute(circuits.update().where(circuits.c.scope==self.f.scope).values(retry_at=0))
            await c.execute(plans.update().where(plans.c.scope==self.f.scope).values(retry_at=0))
        adapter.data=fixtures.protocol();result=await worker.once()
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['id'],self.run['id'])
        self.assertEqual(len((await self.f.store.get(self.run['id']))['attempts']),2)
        self.assertEqual(result['recovery']['circuit']['state'],'closed')
    @unittest.skipUnless(os.environ.get('DIOS_TEST_DOCKER')=='1','isolated Docker not enabled')
    async def test_actual_tls_docker_worker_with_isolated_storage(self):
        adapter=fixtures.DockerRuntimeAdapter(self.f.scope)
        try:await self.actual_tls(adapter)
        finally:
            for container in await asyncio.to_thread(adapter.client.containers.list,all=True,filters={'label':'dios.runtime.scope='+adapter.scope}):
                await asyncio.to_thread(container.remove,force=True)
            adapter.client.close()


@unittest.skipUnless(os.environ.get('DIOS_TEST_POSTGRES_URL'),'isolated PostgreSQL not enabled')
class PostgresRemoteControlTests(RemoteControlTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.f.db.close();self.f.url=os.environ['DIOS_TEST_POSTGRES_URL'];self.f.db=Database(self.f.url);await self.f.db.migrate()
        self.f.store=RuntimeStore(self.f.db,self.f.scope);self.server.store=self.f.store;self.run=await self.submit('one')
