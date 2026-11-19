"""Deferred transport boundary cases; in-memory HTTP only."""
import json
import unittest
import httpx
from app.runtime.mcp_tool_transport import McpChannel, ToolTransportError


class BytesStream(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data
    async def __aiter__(self):
        for byte in self.data:yield bytes([byte])


class McpToolTransportTests(unittest.IsolatedAsyncioTestCase):
    async def channel(self,payload,content_type='application/json'):
        async def handler(request):return httpx.Response(200,headers={'content-type':content_type},stream=BytesStream(payload))
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_utf8_sse_one_byte_at_a_time(self):
        value={'jsonrpc':'2.0','id':'call','result':{'content':[{'type':'text','text':'中文🙂'}]}}
        data=('event: message\r\ndata: '+json.dumps(value,ensure_ascii=False)+'\r\n\r\n').encode()
        async with await self.channel(data,'text/event-stream') as client:
            result=await McpChannel(client,'http://fixture/mcp',{}).call('search',{},'call')
        self.assertEqual(result['content'][0]['text'],'中文🙂')

    async def test_wrong_id_cannot_be_success(self):
        data=json.dumps({'jsonrpc':'2.0','id':'other','result':{'content':[]}}).encode()
        async with await self.channel(data) as client:
            with self.assertRaisesRegex(ToolTransportError,'MCP_RESPONSE_ID_MISMATCH'):
                await McpChannel(client,'http://fixture/mcp',{}).call('search',{},'call')

    async def test_truncated_utf8_retains_original_partial_bytes(self):
        data=b'data: '+bytes([0xe4,0xb8])
        async with await self.channel(data,'text/event-stream') as client:
            with self.assertRaises(ToolTransportError) as error:
                await McpChannel(client,'http://fixture/mcp',{}).call('search',{},'call')
        self.assertEqual(error.exception.partial,data)

    async def test_server_reverse_request_never_executes_client_operation(self):
        data=b'data: {"jsonrpc":"2.0","id":"reverse","method":"sampling/createMessage"}\n\n'
        async with await self.channel(data,'text/event-stream') as client:
            with self.assertRaisesRegex(ToolTransportError,'MCP_SERVER_REQUEST_UNSUPPORTED'):
                await McpChannel(client,'http://fixture/mcp',{}).call('search',{},'call')
