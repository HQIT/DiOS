"""Restricted wrapper-side client. Identity and policy stay in the Worker."""
import asyncio
import json
import time
from .database import RuntimeErrorCode
from .store import encoded


class ToolChannelClient:
    def __init__(self,path):self.path=path

    async def request(self,method,params):
        raw=encoded({'method':method,'params':params})+b'\n'
        if len(raw)>65536:raise RuntimeErrorCode('TOOL_CHANNEL_FRAME_INVALID')
        reader,writer=await asyncio.open_unix_connection(self.path,limit=2*1024*1024+1)
        try:
            async with asyncio.timeout(95):
                writer.write(raw);await writer.drain();raw=await reader.readline()
            if not raw.endswith(b'\n') or len(raw)>2*1024*1024:raise RuntimeErrorCode('TOOL_CHANNEL_FRAME_INVALID')
            response=json.loads(raw.decode('utf-8'))
            if response.get('ok') is not True:raise RuntimeErrorCode(response.get('error','TOOL_CHANNEL_UNAVAILABLE'))
            return response['value']
        finally:
            writer.close()
            try:await writer.wait_closed()
            except (OSError,ConnectionError):pass

    async def ready(self,session_id):
        deadline=time.monotonic()+15
        while True:
            try:return await self.request('ready',{'session_id':session_id})
            except RuntimeErrorCode as exc:
                if exc.code!='TOOL_SESSION_NOT_READY' or time.monotonic()>=deadline:raise
                await asyncio.sleep(.2)

    async def call(self,tool,arguments,key,timeout=600):
        # A transport error never creates a new key or a second logical invocation.
        async with asyncio.timeout(timeout):
            item=await self.request('request',{'tool':tool,'arguments':arguments,'key':key})
            while item['state'] in {'prepared','awaiting_approval','dispatched'}:
                if item['expired'] and item['state']!='dispatched':raise RuntimeErrorCode('TOOL_AUTHORIZATION_EXPIRED')
                await asyncio.sleep(.5)
                item=await self.request('advance',{'invocation_id':item['id']})
            if item['state'] in {'succeeded','failed'}:
                if 'result' not in item:item=await self.request('advance',{'invocation_id':item['id']})
                return item['result']
            raise RuntimeErrorCode('TOOL_APPROVAL_DENIED' if item['state']=='denied' else 'TOOL_RECONCILIATION_REQUIRED')
