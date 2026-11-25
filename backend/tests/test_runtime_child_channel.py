import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from app.runtime.database import RuntimeErrorCode
from app.runtime.store import digest
from app.runtime.worker_tool_channel import WorkerToolChannel
from app.runtime.tool_channel_client import ToolChannelClient
from app.runtime.worker import Worker
from app.runtime.worker_mcp_bridge import WorkerMcpBridge
from test_runtime_worker import FakeAdapter
import test_runtime_children as fixtures


class ChildChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.ChildRunTests();await self.f.asyncSetUp()
        f=self.f;profiles=f.root/'profiles';profiles.mkdir()
        source={'id':'one','name':'Child source'};source['sha256']=digest(source)
        catalog=f.root/'inputs.json';catalog.write_text(json.dumps({'version':1,'inputs':[source]}))
        self.path=profiles/'child.json'
        self.path.write_text(json.dumps(dict(id='test-profile',bridge='jsonl-v1',governance={'mode':'e2ag'},
            launch={'image':'test-image','command':['test-command']},console=dict(enabled=True,input_kind='generic',input_catalog=str(catalog)))))
        self.policy={'tools':{},'acp_names':{'mcp__dios__dios_task_submit':'dios_task_submit','mcp__dios__dios_task_get':'dios_task_get'},'children':dict(max_children=3,max_depth=3,
            profiles={'review':dict(path=str(self.path),profile_id='test-profile',profile_digest=f.spec.fingerprint())})}
        self.gate=SimpleNamespace(store=f.store,policy=f.policy,provider=f.provider,identity_guard=None)
        self.channel=WorkerToolChannel(self.gate,f.binding,self.policy,os.getuid())
        await self.channel.open();await self.channel.bind_session(f.context.session_id)
        self.client=ToolChannelClient(str(self.channel.directory/'channel.sock'))

    async def asyncTearDown(self):
        await self.channel.close();await self.f.asyncTearDown()

    async def submit(self,key='one'):
        return await self.client.submit_child('review','受控子任务',key=key,sample_id='one')

    async def test_socket_admission_replay_independent_worker_and_result(self):
        info=await self.client.request('describe',{})
        self.assertEqual(info['child_tasks'],['review'])
        values=await asyncio.gather(self.submit(),self.submit())
        self.assertEqual(values[0]['id'],values[1]['id'])
        f=self.f;adapter=FakeAdapter()
        worker=Worker(f.store,adapter,'test-profile',f.spec,f.provider,poll_interval=.05,authorization_verifier=f.verifier)
        result=await worker.once()
        self.assertEqual(result['id'],values[0]['id']);self.assertEqual(result['state'],'succeeded')
        observed=await self.client.child(result['id'])
        self.assertEqual(observed['result']['summary'],'合成结果')
        self.assertTrue(observed['artifacts'])
        self.assertNotEqual(result['attempts'][0]['worker_id'],f.binding[2])
        self.assertEqual(adapter.started,1)

    async def test_unknown_alias_extra_identity_and_unrelated_run_are_denied(self):
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_PROFILE_NOT_GRANTED'):
            await self.client.submit_child('unknown','goal',key='one',sample_id='one')
        with self.assertRaisesRegex(RuntimeErrorCode,'NOT_FOUND'):
            await self.client.child(self.f.parent['id'])
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_CHANNEL_METHOD_DENIED'):
            await self.client.request('child.submit',dict(profile='review',goal='goal',key='one',sample_id='one',input_payload=None,actor='forged'))

    async def test_remote_sender_is_rechecked_inside_admission(self):
        self.gate.identity_guard=AsyncMock(side_effect=RuntimeErrorCode('WORKER_CERTIFICATE_EXPIRED'))
        with self.assertRaisesRegex(RuntimeErrorCode,'WORKER_CERTIFICATE_EXPIRED'):await self.submit()
        self.gate.identity_guard.assert_awaited_once()
        self.assertEqual(await self.f.counts(),(1,1,0))

    async def test_parent_cancel_fences_child_access_and_closes_queue(self):
        child=await self.submit();parent=await self.f.store.get(self.f.parent['id'])
        await self.f.store.cancel(parent['id'],parent['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.client.child(child['id'])
        self.assertEqual((await self.f.store.get(child['id']))['state'],'cancelled')

    async def test_target_profile_upgrade_and_key_change_do_not_reuse_authority(self):
        child=await self.submit()
        with self.assertRaisesRegex(RuntimeErrorCode,'IDEMPOTENCY_CONFLICT'):
            await self.client.submit_child('review','changed',key='one',sample_id='one')
        raw=json.loads(self.path.read_text());raw['launch']['command']=['different'];self.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(RuntimeErrorCode,'PROFILE_CHANGED'):await self.submit('two')
        self.assertEqual((await self.f.store.get(child['id']))['state'],'queued')

    async def test_no_child_profile_requires_explicit_activation(self):
        self.channel.policy.pop('children')
        with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_TASKS_NOT_GRANTED'):await self.submit()

    async def test_bounded_wait_preserves_task_and_rechecks_parent(self):
        child=await self.submit()
        observed=await self.client.child(child['id'],wait_seconds=1)
        self.assertTrue(observed['wait_exhausted'])
        self.assertEqual(observed['state'],'queued')
        self.assertEqual(await self.f.counts(),(2,2,1))
        pending=asyncio.create_task(self.client.child(child['id'],wait_seconds=30))
        await asyncio.sleep(.05)
        parent=await self.f.store.get(self.f.parent['id'])
        await self.f.store.cancel(parent['id'],parent['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):
            await asyncio.wait_for(pending,3)

    async def test_wait_duration_is_bounded(self):
        child=await self.submit()
        for value in [-1,31,True,'1']:
            with self.assertRaisesRegex(RuntimeErrorCode,'CHILD_WAIT_INVALID'):
                await self.client.child(child['id'],wait_seconds=value)

    async def test_mcp_tools_expose_only_aliases_and_link_native_activity(self):
        bridge=WorkerMcpBridge(self.client,'test-bridge')
        await bridge.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        await bridge.handle({'jsonrpc':'2.0','method':'notifications/initialized','params':{}})
        listed=await bridge.handle({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        self.assertEqual({x['name'] for x in listed['tools']},{'dios_task_submit','dios_task_get'})
        self.assertNotIn(str(self.path),json.dumps(listed))
        arguments=dict(profile='review',goal='child',sample_id='one')
        self.assertTrue((await self.channel.handle({'method':'permit_proxy','params':{
            'name':'mcp__dios__dios_task_submit','arguments':arguments}}))['forward_to_gate'])
        result=await bridge.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'dios_task_submit','arguments':arguments}})
        self.assertFalse(result['isError'])
        child=json.loads(result['content'][0]['text'])
        observed=await bridge.handle({'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'dios_task_get','arguments':{'run_id':child['id'],'wait_seconds':0}}})
        self.assertEqual(json.loads(observed['content'][0]['text'])['state'],'queued')
        activity=[{'name':'mcp__dios__dios_task_submit','arguments':arguments},
            {'name':'mcp__dios__dios_task_get','arguments':{'run_id':child['id'],'wait_seconds':0}}]
        self.assertTrue((await self.channel.verify_activity(activity))['verified'])
        activity[0]['arguments']['goal']='invented'
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_TOOL_ACTIVITY_UNVERIFIED'):
            await self.channel.verify_activity(activity)
