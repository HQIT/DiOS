"""MCP stdio facade supplied to one ACP session; no authority-bearing token."""
import argparse
import asyncio
import json
import sys
from .database import RuntimeErrorCode
from .store import encoded, digest
from .tool_channel_client import ToolChannelClient


class WorkerMcpBridge:
    def __init__(self,client,instance):self.client,self.instance=client,instance;self.initialized=False;self.ready=False

    async def handle(self,message):
        if not isinstance(message,dict) or message.get('jsonrpc')!='2.0':raise RuntimeErrorCode('MCP_FRAME_INVALID')
        method=message.get('method');params=message.get('params',{})
        if not isinstance(params,dict):raise RuntimeErrorCode('MCP_FRAME_INVALID')
        if method=='initialize':
            if self.initialized:raise RuntimeErrorCode('MCP_ALREADY_INITIALIZED')
            if not isinstance(params.get('protocolVersion'),str):raise RuntimeErrorCode('MCP_FRAME_INVALID')
            self.initialized=True
            return {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'dios-worker-tools','version':'0.1'}}
        if not self.initialized:raise RuntimeErrorCode('MCP_INITIALIZE_REQUIRED')
        if method=='notifications/initialized':self.ready=True;return None
        if method=='ping':return {}
        if not self.ready:raise RuntimeErrorCode('MCP_INITIALIZE_REQUIRED')
        if method=='tools/list':
            value=await self.client.request('describe',{})
            return {'tools':value['tools']}
        if method=='tools/call':
            if set(params)-{'name','arguments','_meta'}:raise RuntimeErrorCode('MCP_FRAME_INVALID')
            key='mcp:'+digest([self.instance,message.get('id')])
            try:return await self.client.call(params.get('name'),params.get('arguments',{}),key)
            except RuntimeErrorCode as exc:return {'isError':True,'content':[{'type':'text','text':exc.code}]}
        raise RuntimeErrorCode('MCP_METHOD_NOT_ALLOWED')


async def serve(path,instance):
    bridge=WorkerMcpBridge(ToolChannelClient(path),instance)
    while True:
        raw=await asyncio.to_thread(sys.stdin.buffer.readline,65537)
        if not raw:return
        message=None
        try:
            if len(raw)>65536 or not raw.endswith(b'\n'):return
            message=json.loads(raw.decode('utf-8'))
            if not isinstance(message,dict):raise RuntimeErrorCode('MCP_FRAME_INVALID')
            result=await bridge.handle(message)
            if 'id' not in message:continue
            response={'jsonrpc':'2.0','id':message['id'],'result':result}
        except Exception as exc:
            if isinstance(message,dict) and 'id' not in message:continue
            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'TOOL_CHANNEL_UNAVAILABLE'
            response={'jsonrpc':'2.0','id':message.get('id') if isinstance(message,dict) else None,
                'error':{'code':-32000,'message':code}}
        sys.stdout.buffer.write(encoded(response)+b'\n');sys.stdout.buffer.flush()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--socket',required=True);parser.add_argument('--instance',required=True)
    args=parser.parse_args();asyncio.run(serve(args.socket,args.instance))

if __name__=='__main__':main()
