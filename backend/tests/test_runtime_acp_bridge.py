"""Protocol fixtures only. Real Cursor/model validation is scheduled separately."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from app.runtime.acp_bridge import AcpClient
from app.runtime.database import RuntimeErrorCode


class Writer:
    def __init__(self):self.messages=[]
    def write(self,data):self.messages.append(json.loads(data))
    async def drain(self):pass


class AcpBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.process=SimpleNamespace(stdin=Writer(),stdout=asyncio.StreamReader(),stderr=asyncio.StreamReader())
        self.events=[];self.peer=AcpClient(self.process,emit_event=self.events.append)
        self.peer.session_id='session'

    async def test_permission_rejected_and_no_client_filesystem(self):
        await self.peer.incoming({'jsonrpc':'2.0','id':2,'method':'session/request_permission',
            'params':{'sessionId':'session','options':[{'kind':'allow_once','optionId':'yes'},{'kind':'reject_once','optionId':'no'}]}})
        self.assertEqual(self.process.stdin.messages[-1]['result']['outcome']['optionId'],'no')
        await self.peer.incoming({'jsonrpc':'2.0','id':3,'method':'fs/write_text_file','params':{}})
        self.assertEqual(self.process.stdin.messages[-1]['error']['code'],-32601)

    async def test_utf8_output_and_session_binding(self):
        await self.peer.incoming({'method':'session/update','params':{'sessionId':'session','update':{
            'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'中文🙂'}}}})
        self.assertEqual(self.events,[{'type':'output','text':'中文🙂'}])
        with self.assertRaisesRegex(RuntimeErrorCode,'SESSION_CHANGED'):
            await self.peer.incoming({'method':'session/update','params':{'sessionId':'other','update':{}}})

    async def test_child_eof_unblocks_pending_rpc(self):
        self.peer.reader=asyncio.create_task(self.peer.read())
        call=asyncio.create_task(self.peer.call('session/prompt',{},timeout=20))
        await asyncio.sleep(0);self.process.stdout.feed_eof()
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_PROCESS_EXITED'):await asyncio.wait_for(call,1)
        await self.peer.reader

    async def test_timeout_is_explicit_and_partial_output_kept(self):
        self.peer.output='partial'
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_QUERY_TIMEOUT'):await self.peer.call('session/prompt',{},timeout=.01)
        self.assertEqual(self.peer.output,'partial');self.assertFalse(self.peer.pending)

    async def test_native_tool_notification_is_unverified(self):
        await self.peer.incoming({'method':'session/update','params':{'sessionId':'session','update':{'sessionUpdate':'tool_call'}}})
        self.assertTrue(self.peer.native_tool_seen)


class ControlledClientOperationsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcpBridgeTests.asyncSetUp
    async def test_read_text_file_uses_gate_and_write_remains_denied(self):
        class Gate:
            def __init__(self):self.calls=[];self.denials=[]
            async def call(self,*args):
                self.calls.append(args);return {'content':[{'type':'text','text':'read-only 中文'}]}
            async def request(self,method,params):self.denials.append((method,params));return {}
        gate=Gate();self.peer.tool_client=gate;self.peer.client_read_tool='read_source'
        await self.peer.incoming({'jsonrpc':'2.0','id':4,'method':'fs/read_text_file',
            'params':{'sessionId':'session','path':'/repo/main.py','line':2,'limit':10}})
        self.assertEqual(gate.calls[0],('read_source',{'path':'/repo/main.py','line':2,'limit':10},'acp-read:4'))
        self.assertEqual(self.process.stdin.messages[-1]['result']['content'],'read-only 中文')
        await self.peer.incoming({'jsonrpc':'2.0','id':5,'method':'fs/write_text_file','params':{'sessionId':'session'}})
        self.assertEqual(len(gate.calls),1);self.assertEqual(gate.denials[-1][1]['method'],'fs/write_text_file')

    async def test_tool_notification_arguments_cannot_change(self):
        self.peer.tool_client=object()
        def notification(query):return {'method':'session/update','params':{'sessionId':'session','update':{
            'sessionUpdate':'tool_call','toolCallId':'call','name':'search','rawInput':{'query':query}}}}
        await self.peer.incoming(notification('one'))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_TOOL_ACTIVITY_CHANGED'):await self.peer.incoming(notification('two'))
