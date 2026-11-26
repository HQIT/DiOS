"""Real Work admission and Worker preflight; optional isolated Docker mount proof."""
from dataclasses import replace
import hashlib
import json
import os
import unittest
from unittest.mock import patch

import test_runtime_worker as fixtures
from test_runtime_tool_invocations import RecordingTransport
from work_auth_fixtures import policy_fixture
from app.runtime.adapter import LaunchSpec
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.secret_provider import FileSecretMounts
from app.runtime.store import digest
from app.runtime.tool_catalog import FileToolCatalog
from app.runtime.tool_invocations import ToolInvocations
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.worker import Worker
from app.runtime.worker_tool_channel import POLICY_ENV, MOUNT_PATH


class CapturingAdapter(fixtures.FakeAdapter):
    name='local-docker-v1'

    async def start(self,handle,spec):
        self.spec=spec
        return await super().start(handle,spec)


class SecretPartitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.WorkerTests.asyncSetUp
    asyncTearDown=fixtures.WorkerTests.asyncTearDown

    async def prepare(self,worker_refs=None,*,channel=True,spec=None,version='v1'):
        refs=[{'ref':name,'version':version,'target':'/run/secrets/'+name} for name in ('model','gateway')]
        channel_policy={'tools':{'search':'search'}}
        if worker_refs is not None:channel_policy['worker_secret_refs']=worker_refs
        self.spec=spec or LaunchSpec('test-image',('test-command',),user=str(os.getuid())+':'+str(os.getgid()))
        if channel:self.spec=replace(self.spec,environment={**self.spec.environment,POLICY_ENV:json.dumps(channel_policy)})
        self.policy,_=policy_fixture(self.root,'test-profile',self.spec,refs)
        reg=dict(enabled=True,transport='mcp-http',url='http://127.0.0.1:19999/mcp',name='search',
            approval_required=True,input_schema={'type':'object'},result_schema={'type':'object'},credential_ref=refs[1])
        reg['permission']=dict(action='repository.search',resource_kind='mcp_tool',resource_id='review/search',
            resource_version=digest({k:v for k,v in reg.items() if k!='enabled'}),constraint_revision='test-policy')
        config=json.loads(self.policy.path.read_text())
        for permissions in config['profiles']['test-profile']['permissions'].values():permissions.append(reg['permission'])
        self.policy.path.write_text(json.dumps(config))
        catalog=self.root/'tools.json';catalog.write_text(json.dumps({'version':1,'tools':{'search':reg}}))
        secrets={}
        self.values={name:'synthetic-'+name+'-private-value' for name in ('model','gateway')}
        for name,value in self.values.items():
            path=self.root/name;path.write_text(value);path.chmod(0o600)
            secrets[name]=dict(state='active',scope=self.scope,profiles=['test-profile'],version=version,
                path=str(path),sha256=hashlib.sha256(value.encode()).hexdigest())
        secret_file=self.root/'secrets.json';secret_file.write_text(json.dumps({'version':1,'secrets':secrets}))
        self.resolver=FileSecretMounts(secret_file,self.scope)
        self.gate=ToolInvocations(self.store,self.policy,FileToolCatalog(catalog),self.provider,RecordingTransport(),secrets=self.resolver)
        descriptor=self.policy.admission('test-profile',self.spec.fingerprint(),refs)
        self.run=await self.store.submit('partition',dict(runtime_profile='test-profile',profile_digest=self.spec.fingerprint(),secret_refs=refs),authorization=descriptor)
        self.refs=refs

    def worker(self,adapter):
        return Worker(self.store,adapter,'test-profile',self.spec,self.provider,poll_interval=.05,
            authorization_verifier=WorkAuthorizationVerifier(self.store,self.policy),
            secret_resolver=self.resolver,secret_requests=self.refs,tool_gate=self.gate)

    async def test_omitted_allowlist_mounts_no_credentials(self):
        await self.prepare();adapter=CapturingAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual([m.target for m in adapter.spec.mounts],[MOUNT_PATH])

    async def test_explicit_model_secret_excludes_gateway_secret(self):
        await self.prepare(['model']);adapter=CapturingAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual({m.target for m in adapter.spec.mounts},{MOUNT_PATH,'/run/secrets/model'})
        self.assertTrue(all(m.read_only for m in adapter.spec.mounts))
        for value in self.values.values():self.assertNotIn(value,json.dumps(adapter.spec.environment))

    async def test_gateway_secret_cannot_be_mounted_even_when_work_authorizes_it(self):
        await self.prepare(['gateway']);adapter=CapturingAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['attempts'][0]['error']['code'],'SECRET_TARGET_CONFLICT')
        self.assertEqual(adapter.started,0)

    async def test_unknown_worker_secret_rejected_before_launch(self):
        await self.prepare(['ungranted']);adapter=CapturingAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['attempts'][0]['error']['code'],'SECRET_TARGET_CONFLICT')
        self.assertEqual(adapter.started,0)

    async def test_profile_without_tool_channel_preserves_authorized_mounts(self):
        await self.prepare(channel=False);adapter=CapturingAdapter()
        result=await self.worker(adapter).once()
        self.assertEqual(result['state'],'succeeded')
        self.assertEqual({m.target for m in adapter.spec.mounts},{r['target'] for r in self.refs})

    async def test_secret_service_recovers_original_run_without_replaying_launched_work(self):
        from app.runtime.database import RuntimeErrorCode
        from app.runtime.fault_schema import plans,circuits
        from app.runtime.faults import POLICY_ENV as RECOVERY_ENV
        recovery=dict(resource='model:fixture',replay_safe=False,max_attempts=2,base_delay=1,max_delay=2,quota_delay=2)
        spec=LaunchSpec('test-image',('test-command',),user=f'{os.getuid()}:{os.getgid()}',environment={RECOVERY_ENV:json.dumps(recovery)})
        await self.prepare(['model'],spec=spec);adapter=CapturingAdapter();worker=self.worker(adapter)
        with patch.object(self.resolver,'resolve',side_effect=RuntimeErrorCode('SECRET_PROVIDER_UNAVAILABLE')):
            first=await worker.once()
        self.assertEqual(first['state'],'retry_wait',first.get('recovery'));self.assertEqual(adapter.started,0)
        self.assertEqual(first['recovery']['category'],'secret_service')
        self.assertEqual(first['recovery']['reason'],'RETRY_UNLAUNCHED')
        async with self.db.transaction(self.scope) as connection:
            await connection.execute(plans.update().where(plans.c.scope==self.scope).values(retry_at=0))
            await connection.execute(circuits.update().where(circuits.c.scope==self.scope).values(retry_at=0))
        result=await worker.once()
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['id'],first['id'])
        self.assertEqual(len(result['attempts']),2);self.assertEqual(adapter.started,1)
        self.assertEqual(result['recovery']['circuit']['state'],'closed')

    @unittest.skipUnless(os.environ.get('DIOS_TEST_SECRET_DOCKER')=='1','explicit Docker secret isolation qualification')
    async def test_real_container_reads_only_model_secret(self):
        code='''import hashlib,json,os
from pathlib import Path
assert not Path('/run/secrets/gateway').exists()
path=Path('/run/secrets/model')
value=path.read_bytes()
try:
 path.write_bytes(b'changed')
except OSError: pass
else: raise AssertionError('secret writable')
assert not any(value.decode() in v for v in os.environ.values())
print(json.dumps({'type':'session','session_id':'partition-docker'}),flush=True)
print(json.dumps({'type':'result','session_id':'partition-docker','result':{
 'summary':'gateway absent; model read-only; environment clean',
 'evidence':['sha256:'+hashlib.sha256(value).hexdigest()]}}),flush=True)
'''
        adapter=DockerRuntimeAdapter(self.scope)
        try:
            image=adapter.client.images.get('python:3.12-slim').id
            spec=LaunchSpec(image,('python','-c',code),user=str(os.getuid())+':'+str(os.getgid()))
            await self.prepare(['model'],spec=spec)
            result=await self.worker(adapter).once()
            self.assertEqual(result['state'],'succeeded',result.get('attempts'))
            self.assertEqual(result['attempts'][0]['session_id'],'partition-docker')
            # Durable task state and artifacts contain evidence, never credential bytes.
            serialized=json.dumps(result)
            for value in self.values.values():
                self.assertNotIn(value,serialized)
                for path in (self.root/'artifacts').rglob('*'):
                    if path.is_file():self.assertNotIn(value.encode(),path.read_bytes())
        finally:
            for container in adapter.client.containers.list(all=True,filters={'label':'dios.runtime.scope='+adapter.scope}):
                container.remove(force=True)
            adapter.client.close()
