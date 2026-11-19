"""Bounded MCP Streamable HTTP client. One tool call; no automatic resend."""
import asyncio
import codecs
from contextlib import asynccontextmanager
import json
import httpx
from .database import RuntimeErrorCode

VERSION='2025-06-18'


class ToolTransportError(RuntimeErrorCode):
    def __init__(self,code,partial=b''):
        super().__init__(code);self.partial=partial


class McpChannel:
    def __init__(self,client,url,headers):
        self.client,self.url,self.headers=client,url,dict(headers)

    async def rpc(self,payload,*,notification=False):
        raw=bytearray()
        try:
            async with self.client.stream('POST',self.url,json=payload,headers=self.headers) as response:
                if response.status_code>=300:raise RuntimeErrorCode('MCP_UPSTREAM_HTTP_ERROR')
                session=response.headers.get('mcp-session-id')
                if session:
                    if len(session)>512 or any(ord(c)<33 or ord(c)>126 for c in session):raise RuntimeErrorCode('MCP_SESSION_INVALID')
                    old=self.headers.get('Mcp-Session-Id')
                    if old and old!=session:raise RuntimeErrorCode('MCP_SESSION_CHANGED')
                    self.headers['Mcp-Session-Id']=session
                if notification:
                    if response.status_code!=202:raise RuntimeErrorCode('MCP_NOTIFICATION_REJECTED')
                    return None
                kind=response.headers.get('content-type','').split(';')[0].strip().lower()
                if kind not in {'application/json','text/event-stream'}:raise RuntimeErrorCode('MCP_CONTENT_TYPE_UNSUPPORTED')
                decoder=codecs.getincrementaldecoder('utf-8')('strict');buffer='';data=[]
                def result(message):
                    if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise RuntimeErrorCode('MCP_RESPONSE_INVALID')
                    if 'method' in message:
                        if 'id' in message:raise RuntimeErrorCode('MCP_SERVER_REQUEST_UNSUPPORTED')
                        return None
                    if message.get('id')!=payload['id']:raise RuntimeErrorCode('MCP_RESPONSE_ID_MISMATCH')
                    if 'error' in message:raise RuntimeErrorCode('MCP_RPC_ERROR')
                    value=message.get('result')
                    if not isinstance(value,dict):raise RuntimeErrorCode('MCP_RESPONSE_INVALID')
                    return value
                async for chunk in response.aiter_bytes():
                    if len(raw)+len(chunk)>1048576:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
                    raw.extend(chunk)
                    if kind=='text/event-stream':
                        buffer+=decoder.decode(chunk)
                        while '\n' in buffer:
                            line,buffer=buffer.split('\n',1);line=line.removesuffix('\r')
                            if line.startswith('data:'):data.append(line[5:].removeprefix(' '))
                            elif not line and data:
                                value=result(json.loads('\n'.join(data)));data=[]
                                if value is not None:return value
                if kind=='application/json':
                    value=result(json.loads(bytes(raw).decode('utf-8')))
                    if value is not None:return value
                else:decoder.decode(b'',final=True)
                raise RuntimeErrorCode('MCP_RESPONSE_INCOMPLETE')
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'MCP_TRANSPORT_FAILED'
            raise ToolTransportError(code,bytes(raw)) from None

    async def call(self,name,arguments,invocation_id):
        return await self.rpc({'jsonrpc':'2.0','id':invocation_id,'method':'tools/call',
            'params':{'name':name,'arguments':arguments}})


class McpHttpTransport:
    @asynccontextmanager
    async def open(self,registration,headers):
        headers={**headers,'Accept':'application/json, text/event-stream','Content-Type':'application/json'}
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=30) as client:
            channel=McpChannel(client,registration['url'],headers)
            async with asyncio.timeout(30):
                result=await channel.rpc({'jsonrpc':'2.0','id':'initialize','method':'initialize','params':{
                    'protocolVersion':VERSION,'capabilities':{},'clientInfo':{'name':'dios-tool-gate','version':'0.1'}}})
                if result.get('protocolVersion')!=VERSION:raise RuntimeErrorCode('MCP_VERSION_UNSUPPORTED')
                capabilities=result.get('capabilities',{})
                if not isinstance(capabilities,dict) or not isinstance(capabilities.get('tools'),dict):raise RuntimeErrorCode('MCP_TOOLS_UNSUPPORTED')
                channel.headers['MCP-Protocol-Version']=VERSION
                await channel.rpc({'jsonrpc':'2.0','method':'notifications/initialized'},notification=True)
            yield channel
