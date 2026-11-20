"""Deferred Worker channel tests. In-memory transport; no model or Docker launch."""
import asyncio
import json
import os
import socket
import unittest
from app.runtime.database import RuntimeErrorCode
from app.runtime.worker_tool_channel import WorkerToolChannel, POLICY_ENV
from app.runtime.tool_channel_client import ToolChannelClient
from app.runtime.worker_mcp_bridge import WorkerMcpBridge
from app.runtime.worker import read_profile
import test_runtime_tool_invocations as tool_fixtures


class WorkerToolChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=tool_fixtures.ToolInvocationTests();await self.fixture.asyncSetUp()
        f=self.fixture
        self.channel=WorkerToolChannel(f.gate,f.binding,{'tools':{'search':'search'},'acp_names':{'mcp__dios__search':'search'}},os.getuid() if hasattr(os,'getuid') else 1)
        # Populate exactly the registrations that open() would resolve, without opening a socket.
        self.channel.registered={'search':f.gate.catalog.registration('search')}
        await self.channel.bind_session('session-one')
    async def asyncTearDown(self):
        await self.channel.close();await self.fixture.asyncTearDown()

    async def request(self):
        return await self.channel.handle({'method':'request','params':{'tool':'search','arguments':{'query':'evidence'},'key':'stable-call'}})

    async def test_channel_has_no_identity_override_or_approval_method(self):
        for message in [
            {'method':'approve','params':{}},
            {'method':'request','params':{'tool':'search','arguments':{},'key':'one','run_id':'other'}},
            {'method':'bind_session','params':{'session_id':'other'}}]:
            with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_CHANNEL_METHOD_DENIED'):await self.channel.handle(message)

    async def test_approval_wait_automatic_dispatch_and_same_key_replay(self):
        f=self.fixture;item=await self.request()
        self.assertEqual(item['state'],'awaiting_approval')
        waiting=await self.channel.handle({'method':'advance','params':{'invocation_id':item['id']}})
        self.assertEqual(waiting['state'],'awaiting_approval');self.assertEqual(f.transport.calls,[])
        await f.gate.decide(f.run['id'],item['id'],item['version'],'approved','fixture-approval')
        result=await self.channel.handle({'method':'advance','params':{'invocation_id':item['id']}})
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['result']['content'][0]['text'],'中文结果🙂')
        replay=await self.request();self.assertEqual(replay['id'],item['id']);self.assertEqual(len(f.transport.calls),1)
        proof=await self.channel.handle({'method':'verify_activity','params':{'activity':[{'name':'mcp__dios__search','arguments':{'query':'evidence'}}]}})
        self.assertTrue(proof['verified'])
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_TOOL_ACTIVITY_UNVERIFIED'):
            await self.channel.handle({'method':'verify_activity','params':{'activity':[{'name':'mcp__dios__search','arguments':{'query':'changed'}}]}})

    async def test_proxy_permission_does_not_approve_or_execute_action(self):
        result=await self.channel.handle({'method':'permit_proxy','params':{'name':'mcp__dios__search','arguments':{'query':'evidence'}}})
        self.assertTrue(result['forward_to_gate']);self.assertEqual(self.fixture.transport.calls,[])
        item=await self.request();self.assertEqual(item['state'],'awaiting_approval')
        for name,args in [('terminal',{}),('mcp__dios__search',{'unexpected':True})]:
            with self.assertRaises(Exception):
                await self.channel.handle({'method':'permit_proxy','params':{'name':name,'arguments':args}})
        self.assertEqual(self.fixture.transport.calls,[])

    async def test_required_evidence_checks_success_session_and_exact_arguments(self):
        requirement={'method':'verify_required','params':{'requirements':[{'tool':'search','arguments':{'query':'evidence'}}]}}
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):await self.channel.handle(requirement)
        item=await self.request()
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):await self.channel.handle(requirement)
        f=self.fixture
        await f.gate.decide(f.run['id'],item['id'],item['version'],'approved','fixture-approval')
        await self.channel.handle({'method':'advance','params':{'invocation_id':item['id']}})
        self.assertTrue((await self.channel.handle(requirement))['verified'])
        requirement['params']['requirements'][0]['arguments']['query']='other'
        with self.assertRaisesRegex(RuntimeErrorCode,'EVIDENCE_INSUFFICIENT'):await self.channel.handle(requirement)

    async def test_socket_binds_peer_uid_and_closes_with_attempt(self):
        if not hasattr(socket,'SO_PEERCRED'):self.skipTest('Linux credentialled Unix sockets')
        self.channel.session_id=None
        mount=await self.channel.open();await self.channel.bind_session('session-one')
        client=ToolChannelClient(str(self.channel.directory/'channel.sock'))
        value=await client.request('describe',{});self.assertEqual(value['tools'][0]['name'],'search')
        self.assertTrue(mount.read_only)
        self.channel.uid=-1
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_CHANNEL_PEER_DENIED'):await client.request('describe',{})
        await self.channel.close()

    async def test_revocation_blocks_channel_and_operator_invocations_cannot_be_adopted(self):
        f=self.fixture
        operator_item=await f.request()
        with self.assertRaisesRegex(RuntimeErrorCode,'TOOL_BINDING_CHANGED'):
            await self.channel.handle({'method':'advance','params':{'invocation_id':operator_item['id']}})
        run=await f.store.get(f.run['id']);await f.store.revoke(run['id'],run['version'])
        with self.assertRaisesRegex(RuntimeErrorCode,'AUTH_REVOKED'):await self.request()

    async def test_profile_channel_policy_changes_fingerprint(self):
        f=self.fixture;path=f.root/'profile.json'
        value={'id':'profile','bridge':'jsonl-v1','governance':{'mode':'e2ag'},
            'launch':{'image':'fixture','command':['fixture']},'tool_channel':{'tools':{'search':'search'},'acp_names':{}}}
        path.write_text(json.dumps(value));_,first=read_profile(path)
        self.assertIn(POLICY_ENV,first.environment)
        value['tool_channel']['acp_names']={'vendor.search':'search'}
        path.write_text(json.dumps(value));_,second=read_profile(path)
        self.assertNotEqual(first.fingerprint(),second.fingerprint())


class McpFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_negotiation_and_uninitialized_tool_call(self):
        bridge=WorkerMcpBridge(None,'fixture')
        with self.assertRaisesRegex(RuntimeErrorCode,'MCP_INITIALIZE_REQUIRED'):
            await bridge.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'search'}})
        result=await bridge.handle({'jsonrpc':'2.0','id':2,'method':'initialize','params':{'protocolVersion':'future-version'}})
        self.assertEqual(result['protocolVersion'],'2025-06-18')
