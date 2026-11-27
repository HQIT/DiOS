"""Actual parent/child containers complete under a two-slot limit, without oversubscription."""
import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from app.runtime.adapter import LaunchSpec
from app.runtime.capacity import Capacity
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.worker import Worker, read_profile
from app.runtime.worker_tool_channel import POLICY_ENV
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_policy import permission, FileWorkPolicy
from app.runtime.store import digest
import test_runtime_capacity as fixtures
from test_runtime_worker import protocol
from work_auth_fixtures import policy_fixture

OBSERVATIONS=[]

@unittest.skipUnless(os.environ.get('DIOS_TEST_DOCKER')=='1','native Docker opt-in')
class NativeChildCapacityTests(fixtures.CapacityScenarios, unittest.IsolatedAsyncioTestCase):
    async def test_two_parents_and_child_complete_without_holding_all_slots(self):
        import docker
        from app.runtime.artifacts import LocalArtifacts
        client=docker.from_env()
        try:image=client.images.get(os.environ.get('DIOS_TEST_ACP_IMAGE','dios-backend:recovery-upgrade-20261127')).id
        finally:client.close()
        profiles=self.root/'profiles';profiles.mkdir()
        source={'id':'one','name':'Source'};source['sha256']=digest(source)
        inputs=self.root/'inputs.json';inputs.write_text(json.dumps({'version':1,'inputs':[source]}))
        child_path=profiles/'child.json'
        child_path.write_text(json.dumps(dict(id='child',bridge='jsonl-v1',governance={'mode':'e2ag'},
            launch=dict(image=image,command=['python','-u','-c','import time,sys;data='+repr(protocol())+';head,rest=data.split(bytes([10]),1);print(head.decode(),flush=True);time.sleep(1);sys.stdout.buffer.write(rest)']),
            console=dict(enabled=True,input_kind='generic',input_catalog=str(inputs)))))
        _,child_spec=read_profile(child_path)
        channel=dict(tools={},children=dict(max_children=1,max_depth=1,profiles=dict(child=dict(
            path=str(child_path),profile_id='child',profile_digest=child_spec.fingerprint()))))
        program='''import asyncio,json,os,uuid
from app.runtime.tool_channel_client import ToolChannelClient
session=str(uuid.uuid4())
print(json.dumps({'type':'session','session_id':session}),flush=True)
async def run():
 client=ToolChannelClient(os.environ['DIOS_TOOL_SOCKET'])
 for _ in range(100):
  try:
   child=await client.submit_child('child','child capacity qualification',key='only-child',sample_id='one')
   break
  except Exception as exc:
   if str(exc)!='TOOL_SESSION_NOT_READY':raise
   await asyncio.sleep(.05)
 else:raise RuntimeError('parent session was not bound')
 for _ in range(30):
  result=await client.child(child['id'],wait_seconds=1)
  if result['state']=='succeeded':
   print(json.dumps({'type':'result','session_id':session,'result':{'summary':'Parent and child complete','evidence':[child['id']]}}),flush=True)
   return
  if result['wait_blocked']:raise RuntimeError(str(result['wait_blocked']))
 raise RuntimeError('child did not finish')
asyncio.run(run())
'''
        parent_spec=LaunchSpec(image,('python','-u','-c',program),environment={POLICY_ENV:json.dumps(channel),'PYTHONPATH':'/app'})
        child_policy,child_descriptor=policy_fixture(self.root,'child',child_spec)
        child_config=json.loads(child_policy.path.read_text())['profiles']['child']
        policy,parent_descriptor=policy_fixture(self.root,'parent',parent_spec)
        config=json.loads(policy.path.read_text());config['profiles']['child']=child_config
        for layer in config['profiles']['parent']['permissions']:
            config['profiles']['parent']['permissions'][layer]+=child_config['permissions'][layer]+[permission(child_descriptor,'spawn')]
        policy.path.write_text(json.dumps(config));policy=FileWorkPolicy(policy.path)
        provider=LocalArtifacts(self.root/'objects');self.addCleanup(provider.close)
        adapter=DockerRuntimeAdapter(self.scope);self.addCleanup(adapter.client.close)
        self.addAsyncCleanup(self.cleanup_containers,adapter)
        gate=SimpleNamespace(store=self.store,policy=policy,provider=provider,identity_guard=None)
        verifier=WorkAuthorizationVerifier(self.store,policy)
        def worker(profile,spec):return Worker(self.store,adapter,profile,spec,provider,poll_interval=.05,authorization_verifier=verifier,tool_gate=gate if profile=='parent' else None)
        await Capacity(self.store).put('shared',{'kind':'scope'},2,0)
        parents=[]
        for n in range(2):
            parents.append(await self.store.submit('parent-'+str(n),dict(runtime_profile='parent',profile_digest=parent_spec.fingerprint(),secret_refs=[],payload={}),authorization=policy.admission('parent',parent_spec.fingerprint(),[])))
        maximum=0
        async def observe():
            nonlocal maximum
            while True:
                status=await self.store.runtime_status()
                if status['actual_concurrency'] is not None:
                    maximum=max(maximum,status['actual_concurrency'])
                    self.assertLessEqual(status['actual_concurrency'],2)
                await asyncio.sleep(.05)
        observer=asyncio.create_task(observe())
        async def child_loop():
            for _ in range(300):
                await worker('child',child_spec).once()
                states=[(await self.store.get(r['id']))['state'] for r in parents]
                if states==['succeeded','succeeded']:return
                self.assertFalse('failed' in states,states)
                await asyncio.sleep(.05)
            self.fail('parents did not complete')
        child_task=asyncio.create_task(child_loop())
        try:
            first=await asyncio.gather(worker('parent',parent_spec).once(),worker('parent',parent_spec).once())
            if any(r['state']=='failed' for r in first):
                for path in provider.root.iterdir():
                    if path.is_file() and len(path.name)==64:
                        print(path.read_bytes()[:3000])
            self.assertEqual(sorted(r['state'] for r in first),['idle','succeeded'],str(first)[:4000])
            second=await worker('parent',parent_spec).once()
            self.assertEqual(second['state'],'succeeded')
            await asyncio.wait_for(child_task,30)
            status=(await Capacity(self.store).status())['items'][0]
            self.assertEqual((status['reserved_attempts'],status['tree_reserved_slots']),(0,0))
            if observer.done():observer.result()
            self.assertEqual(maximum,2)
            OBSERVATIONS.append(dict(image=image,parents=[r['id'] for r in parents],max_actual_concurrency=maximum,
                competing_parent_waited=True,completed_parent_trees=2,final_capacity=status))
        finally:
            child_task.cancel();observer.cancel()
            await asyncio.gather(child_task,observer,return_exceptions=True)

    async def cleanup_containers(self,adapter):
        for container in await asyncio.to_thread(adapter.client.containers.list,all=True,filters={'label':'dios.runtime.scope='+self.scope}):
            await asyncio.to_thread(container.remove,force=True)