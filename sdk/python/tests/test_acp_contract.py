"""Protocol fixtures only. Real Cursor/model validation is scheduled separately."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from dios_sdk.acp import AcpClient
from dios_sdk import WorkloadError as RuntimeErrorCode


class Writer:
    def __init__(self):self.messages=[]
    def write(self,data):self.messages.append(json.loads(data))
    async def drain(self):pass
    def close(self):pass


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
        self.assertEqual(self.peer.output,'partial')
        self.assertFalse(self.peer.prompt_task.done())
        await self.peer.connection.close()
        await asyncio.gather(self.peer.prompt_task,return_exceptions=True)

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
        self.assertEqual(gate.calls[0][:2],('read_source',{'path':'/repo/main.py','line':2,'limit':10}))
        self.assertTrue(gate.calls[0][2].startswith('acp-read:'))
        self.assertEqual(self.process.stdin.messages[-1]['result']['content'],'read-only 中文')
        await self.peer.incoming({'jsonrpc':'2.0','id':5,'method':'fs/write_text_file','params':{'sessionId':'session'}})
        self.assertEqual(len(gate.calls),1);self.assertEqual(gate.denials[-1][1]['method'],'fs/write_text_file')

    async def test_tool_notification_arguments_cannot_change(self):
        self.peer.tool_client=object()
        def notification(query):return {'method':'session/update','params':{'sessionId':'session','update':{
            'sessionUpdate':'tool_call','toolCallId':'call','name':'search','rawInput':{'query':query}}}}
        await self.peer.incoming(notification('one'))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_TOOL_ACTIVITY_CHANGED'):await self.peer.incoming(notification('two'))


class CompletionRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_result_survives_nonzero_server_shutdown(self):
        import sys,tempfile
        from pathlib import Path
        from unittest.mock import patch
        from dios_sdk.acp import execute
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            server=root/'server.py'
            server.write_text("""import sys,json
for line in sys.stdin:
 m=json.loads(line);method=m.get('method')
 if 'id' not in m:continue
 if method=='initialize':r={'protocolVersion':1}
 elif method=='session/new':r={'sessionId':'persistent-session'}
 else:
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'persistent-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'{"summary":"retained","evidence":["sample"]}'}}}}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'persistent-session','update':{'sessionUpdate':'usage_update','used':200,'size':20000,'cost':{'amount':0.125,'currency':'USD'}}}}),flush=True)
  r={'stopReason':'end_turn','usage':{'inputTokens':100,'outputTokens':20,'totalTokens':120}}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)
sys.exit(1)
""",encoding='utf-8')
            config=root/'config.json';config.write_text(json.dumps({'command':[sys.executable,str(server)],'cwd':str(root)}))
            events=[]
            with patch('dios_sdk.acp.emit',events.append):
                code=await execute(config,'review',lambda value:value)
            self.assertEqual(code,2)
            self.assertEqual(events[-1]['type'],'result')
            usage=[x for x in events if x['type']=='usage']
            self.assertEqual([x['kind'] for x in usage],['acp_session','acp_prompt'])
            self.assertEqual(usage[0]['values']['cost']['amount'],'0.125')
            self.assertEqual(usage[1]['values']['inputTokens'],100)
            self.assertEqual(events[-1]['session_id'],'persistent-session')
            self.assertEqual(events[-1]['result']['summary'],'retained')




class CursorMappingTests(unittest.TestCase):
    def test_only_owned_mcp_proxy_is_forwarded(self):
        from dios_sdk.cursor import proxy_permission,tool_activity
        raw={'providerIdentifier':'dios','toolName':'repository_search','args':{'query':'sha'}}
        self.assertEqual(proxy_permission({'rawInput':raw}),{'name':'mcp__dios__repository_search','arguments':{'query':'sha'}})
        for changed in ({'command':'ls'},{**raw,'providerIdentifier':'external'}):
            with self.assertRaises(RuntimeErrorCode):proxy_permission({'rawInput':changed})
        with self.assertRaises(RuntimeErrorCode):tool_activity({'rawInput':raw,'status':'pending'})
        self.assertEqual(tool_activity({'rawInput':raw,'status':'completed'})['rawInput'],{'query':'sha'})

    def test_final_object_preserves_commentary_but_rejects_partial_answer(self):
        from dios_sdk.cursor import final_object
        self.assertEqual(final_object('Checking evidence.\n```json\n{"summary":"ok","findings":[]}\n```')['summary'],'ok')
        for text in ('{"summary":"partial"','{"summary":"ok"} trailing text'):
            with self.assertRaises(ValueError):final_object(text)


class StreamingToolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=AcpBridgeTests.asyncSetUp
    async def test_arguments_stream_until_terminal_then_freeze(self):
        self.peer.tool_client=object()
        def frame(kind,raw,status):return {'method':'session/update','params':{'sessionId':'session','update':{
            'sessionUpdate':kind,'toolCallId':'one','rawInput':raw,'status':status}}}
        await self.peer.incoming(frame('tool_call',{},'pending'))
        await self.peer.incoming(frame('tool_call_update',{'query':'sha'},'in_progress'))
        await self.peer.incoming(frame('tool_call_update',{'query':'sha'},'completed'))
        with self.assertRaisesRegex(RuntimeErrorCode,'ACP_TOOL_ACTIVITY_CHANGED'):
            await self.peer.incoming(frame('tool_call_update',{'query':'other'},'completed'))

    async def test_proxy_permission_requires_gate_and_allow_once(self):
        from dios_sdk.cursor import proxy_permission
        class Gate:
            async def request(self,method,params):
                self.method=method;return {'forward_to_gate':True}
        gate=Gate();self.peer.tool_client=gate;self.peer.permission_decoder=proxy_permission
        self.peer.tool_activity['native-one']={'rawInput':{'providerIdentifier':'dios','toolName':'search','args':{}}}
        await self.peer.incoming({'jsonrpc':'2.0','id':10,'method':'session/request_permission','params':{
            'sessionId':'session','toolCall':{'toolCallId':'native-one','title':'Search'},
            'options':[{'kind':'allow_once','optionId':'once'},{'kind':'allow_always','optionId':'always'}]}})
        self.assertEqual(gate.method,'permit_proxy')
        self.assertEqual(self.process.stdin.messages[-1]['result']['outcome']['optionId'],'once')
