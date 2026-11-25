import json
import unittest
from unittest.mock import AsyncMock, patch
from dios_sdk.mcp import WorkerMcpBridge
from dios_sdk.tools import ToolChannelClient
from dios_sdk.protocol import WorkloadError


class ChildTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_preserves_stable_key_and_opaque_input(self):
        client = ToolChannelClient('/unused.sock')
        with patch.object(client,'request',new_callable=AsyncMock) as request:
            await client.submit_child('delegate','中文目标',key='stable',input_payload={'id':'one','name':'输入'})
            self.assertEqual(request.call_args.args,('child.submit',dict(profile='delegate',goal='中文目标',key='stable',sample_id=None,input_payload={'id':'one','name':'输入'})))
            await client.child('run-one')
            request.assert_awaited_with('child.get',{'run_id':'run-one','wait_seconds':0})
            await client.children()
            request.assert_awaited_with('child.list',{'wait_seconds':0})

    async def test_discovery_mcp_defaults_and_rejects_identity_selection(self):
        client=AsyncMock()
        client.children.return_value={'items':[],'wait_exhausted':True}
        bridge=WorkerMcpBridge(client,'session')
        await bridge.handle({'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        await bridge.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        msg={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'dios_task_list','arguments':{}}}
        self.assertFalse((await bridge.handle(msg))['isError'])
        client.children.assert_awaited_once_with(wait_seconds=20)
        msg['params']['arguments']={'parent_run_id':'another'}
        self.assertTrue((await bridge.handle(msg))['isError'])
        self.assertEqual(client.children.await_count,1)

    async def test_mcp_replay_identity_and_denial(self):
        client = AsyncMock()
        client.submit_child.return_value={'id':'child','state':'queued'}
        bridge = WorkerMcpBridge(client,'attempt-session')
        await bridge.handle({'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        await bridge.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        message={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'dios_task_submit','arguments':{'profile':'one','goal':'g','sample_id':'sample'}}}
        first=await bridge.handle(message);await bridge.handle(message)
        self.assertEqual(json.loads(first['content'][0]['text'])['id'],'child')
        self.assertEqual(client.submit_child.await_args_list[0],client.submit_child.await_args_list[1])
        message['params']['arguments']['actor']='forged'
        self.assertTrue((await bridge.handle(message))['isError'])
        self.assertEqual(client.submit_child.await_count,2)
        client.child.side_effect=WorkloadError('PARENT_AUTHORIZATION_INVALID')
        denied=await bridge.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'dios_task_get','arguments':{'run_id':'child'}}})
        self.assertTrue(denied['isError'])
        self.assertEqual(denied['content'][0]['text'],'PARENT_AUTHORIZATION_INVALID')
