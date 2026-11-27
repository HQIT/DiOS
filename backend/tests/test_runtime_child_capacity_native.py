"""Actual parent/child containers complete under a two-slot limit, without oversubscription."""
import asyncio
import json
import os
import sys
import time
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
    async def fixture(self):
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
        parent_path=profiles/'parent.json'
        parent_path.write_text(json.dumps(dict(id='parent',bridge='jsonl-v1',governance={'mode':'e2ag'},
            tool_channel=channel,launch=dict(image=image,command=['python','-u','-c',program],environment={'PYTHONPATH':'/app'}))))
        _,parent_spec=read_profile(parent_path)
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
        from app.runtime.tool_invocations import ToolInvocations
        from app.runtime.tool_catalog import FileToolCatalog
        from app.runtime.mcp_tool_transport import McpHttpTransport
        tools_path=self.root/'tools.json';tools_path.write_text(json.dumps({'version':1,'tools':{}}))
        gate=ToolInvocations(self.store,policy,FileToolCatalog(tools_path),provider,McpHttpTransport())
        verifier=WorkAuthorizationVerifier(self.store,policy)
        def worker(profile,spec):return Worker(self.store,adapter,profile,spec,provider,poll_interval=.05,authorization_verifier=verifier,tool_gate=gate if profile=='parent' else None)
        await Capacity(self.store).put('shared',{'kind':'scope'},2,0)
        parents=[]
        for n in range(2):
            parents.append(await self.store.submit('parent-'+str(n),dict(runtime_profile='parent',profile_digest=parent_spec.fingerprint(),secret_refs=[],payload={}),authorization=policy.admission('parent',parent_spec.fingerprint(),[])))
        return SimpleNamespace(image=image,worker=worker,parents=parents,parent_spec=parent_spec,
            child_spec=child_spec,parent_path=parent_path,policy=policy,provider=provider,adapter=adapter)

    async def test_two_parents_and_child_complete_without_holding_all_slots(self):
        f=await self.fixture()
        image,worker,parents,parent_spec,child_spec,provider=(f.image,f.worker,f.parents,f.parent_spec,f.child_spec,f.provider)
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

    async def test_killed_parent_worker_preserves_reservation_until_reconciled(self):
        f=await self.fixture()
        tools=self.root/'tools.json';tools.write_text(json.dumps({'version':1,'tools':{}}))
        command=[sys.executable,'-m','app.runtime.cli','--database-url',self.url,'--scope',self.scope,
            'worker','--profile',str(f.parent_path),'--artifacts',str(f.provider.root),
            '--policy-file',str(f.policy.path),'--tools-file',str(tools),'--once','--poll-interval','.05']
        async def start():
            return await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        async def stop(process):
            if process.returncode is None:process.kill()
            await process.communicate()
        process=await start();self.addAsyncCleanup(stop,process)
        deadline=time.monotonic()+35
        while True:
            snapshots=[await self.store.get(r['id']) for r in f.parents]
            active=next((r for r in snapshots if r.get('family',{} ) and r['family']['children']),None)
            if active:break
            if process.returncode is not None:
                self.fail(str(await process.communicate()))
            self.assertLess(time.monotonic(),deadline,'parent did not submit child')
            await asyncio.sleep(.1)
        original=active['attempts'][0]
        child_id=active['family']['children'][0]['run_id']
        self.assertTrue(original['session_id'])
        self.assertTrue(original['process_instance'])
        self.assertIsNone(process.returncode)
        process.kill();await process.communicate()
        self.assertEqual(process.returncode,-9)
        blocked=await f.worker('parent',f.parent_spec).once()
        self.assertEqual(blocked['state'],'idle')
        held=(await Capacity(self.store).status())['items'][0]
        self.assertEqual((held['reserved_attempts'],held['tree_reserved_slots']),(1,1))
        # Use real server time and lease expiration. A query/Worker exit does not transfer ownership.
        recovery=f.worker('parent',f.parent_spec)
        deadline=time.monotonic()+85
        while True:
            await recovery.recover()
            resolved=await self.store.get(active['id'])
            if resolved['state']=='failed':break
            self.assertLess(time.monotonic(),deadline,str(resolved['state']))
            await asyncio.sleep(.5)
        self.assertEqual(len(resolved['attempts']),1)
        self.assertEqual(resolved['attempt_id'],original['id'])
        self.assertEqual(resolved['attempts'][0]['session_id'],original['session_id'])
        self.assertEqual((await self.store.get(child_id))['state'],'cancelled')
        freed=(await Capacity(self.store).status())['items'][0]
        self.assertEqual((freed['reserved_attempts'],freed['tree_reserved_slots']),(0,0))
        restarted=await start();self.addAsyncCleanup(stop,restarted)
        async def children():
            deadline=time.monotonic()+35
            while restarted.returncode is None:
                await f.worker('child',f.child_spec).once()
                self.assertLess(time.monotonic(),deadline,'restarted tree did not finish')
                await asyncio.sleep(.05)
        child_task=asyncio.create_task(children())
        try:
            stdout,stderr=await asyncio.wait_for(restarted.communicate(),40)
            await child_task
            self.assertEqual(restarted.returncode,0,stderr.decode())
            remaining=next(r for r in f.parents if r['id']!=active['id'])
            completed=await self.store.get(remaining['id'])
            self.assertEqual(completed['state'],'succeeded',stdout.decode())
            self.assertEqual(len(completed['attempts']),1)
            final=(await Capacity(self.store).status())['items'][0]
            self.assertEqual((final['reserved_attempts'],final['tree_reserved_slots']),(0,0))
            OBSERVATIONS.append(dict(case='native-parent-worker-sigkill',image=f.image,
                killed_worker_pid=process.pid,worker_exit=process.returncode,restarted_worker_pid=restarted.pid,
                parent_run=active['id'],parent_attempt=original['id'],session_id=original['session_id'],
                process_instance=original['process_instance'],child_run=child_id,
                parent_state=resolved['state'],original_attempts=len(resolved['attempts']),
                child_state='cancelled',held_capacity=held,reconciled_capacity=freed,
                next_parent=completed['id'],next_parent_state=completed['state'],final_capacity=final))
        finally:
            child_task.cancel();await asyncio.gather(child_task,return_exceptions=True)

    async def cleanup_containers(self,adapter):
        for container in await asyncio.to_thread(adapter.client.containers.list,all=True,filters={'label':'dios.runtime.scope='+self.scope}):
            await asyncio.to_thread(container.remove,force=True)