"""Host-side TLS client behind the existing per-Attempt Unix socket."""
import asyncio
import json
import time
import secrets
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace
from .database import RuntimeErrorCode
from .store import encoded, identifier
from .worker_identity import PROTOCOL, document, tls_context
from .worker_tool_channel import WorkerToolChannel
from .remote_tool_gateway import REQUEST_LIMIT, RESPONSE_LIMIT


class RemoteToolProvider:
    def __init__(self,path,store,profile,spec):
        self.config=document(path);self.store,self.profile,self.spec=store,profile,spec
        c=self.config
        if c.get('enabled') is not True:raise RuntimeErrorCode('WORKER_GATEWAY_DISABLED')
        for key in ('worker_id','audience','server_name','host'):identifier(c.get(key))
        if type(c.get('port')) is not int or not 1<=c['port']<=65535:raise RuntimeErrorCode('WORKER_GATEWAY_CONFIG_INVALID')
        if len(c['worker_id'])>31:raise RuntimeErrorCode('WORKER_GATEWAY_CONFIG_INVALID')
        self.worker_id=c['worker_id']+':'+uuid4().hex
        self.automatic=c.get('credential_mode','file')=='automatic'
        if c.get('credential_mode','file') not in {'file','automatic'}:raise RuntimeErrorCode('WORKER_CREDENTIAL_MODE_INVALID')
        self.credentials={}
        self.context=tls_context(c['ca_file'],c['certificate_file'],c['key_file'])

    def channel(self,binding,policy,uid):return RemoteWorkerToolChannel(self,binding,policy,uid)

    async def request(self,binding,message):
        deadline=time.monotonic()+15
        while True:
            try:return await self._request(binding,message)
            except RuntimeErrorCode as exc:
                # BUSY is emitted before admitting any action; ambiguous network failures never retry.
                if exc.code!='WORKER_GATEWAY_BUSY' or time.monotonic()>=deadline:raise
                await asyncio.sleep(.2)

    async def _request(self,binding,message):
        if self.automatic:
            credential=await self.credential(binding)
            return await self._wire(binding,message,credential['access_token'])
        c=self.config
        def read_token():
            path=Path(c['token_file'])
            if not path.is_file() or path.stat().st_size>256:raise RuntimeErrorCode('WORKER_TOKEN_DENIED')
            return path.read_text(encoding='ascii').strip()
        token=await asyncio.to_thread(read_token)
        return await self._wire(binding,message,token)

    async def _wire(self,binding,message,token):
        c=self.config
        envelope=dict(protocol=PROTOCOL,audience=c['audience'],scope=self.store.scope,profile=self.profile,
            profile_digest=self.spec.fingerprint(),binding=list(binding),message=message,access_token=token)
        if self.automatic:envelope['credential_mode']='automatic'
        return await self.exchange(envelope)

    async def exchange(self,envelope):
        c=self.config
        raw=encoded(envelope)+b'\n'
        if len(raw)>REQUEST_LIMIT:raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        writer=None
        try:
            async with asyncio.timeout(95):
                reader,writer=await asyncio.open_connection(c['host'],c['port'],ssl=self.context,
                    server_hostname=c['server_name'],ssl_handshake_timeout=10,limit=RESPONSE_LIMIT+1)
                if writer.get_extra_info('ssl_object').selected_alpn_protocol()!=PROTOCOL:raise RuntimeErrorCode('WORKER_TLS_REQUIRED')
                writer.write(raw);await writer.drain();response=await reader.readline()
            if len(response)>RESPONSE_LIMIT or not response.endswith(b'\n'):raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
            value=json.loads(response.decode('utf-8'))
            if not isinstance(value,dict):raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
            if value.get('ok') is not True:raise RuntimeErrorCode(value.get('error','WORKER_GATEWAY_UNAVAILABLE'))
            return value['value']
        except (OSError,TimeoutError):raise RuntimeErrorCode('WORKER_GATEWAY_OBSERVATION_UNAVAILABLE') from None
        finally:
            if writer:
                writer.close()
                try:await asyncio.wait_for(writer.wait_closed(),3)
                except (OSError,TimeoutError):pass


    async def credential(self,binding):
        key=tuple(binding)
        state=self.credentials.setdefault(key,{'lock':asyncio.Lock(),'value':None,'pending':None,'refresh_at':0})
        async with state['lock']:
            if state['value'] and state['pending'] is None and time.monotonic()<state['refresh_at']:return state['value']
            if state['pending'] is None:
                previous=state['value'];issue=previous is None or time.monotonic()>=state.get('expires_at',0)
                params={'nonce':secrets.token_urlsafe(32)}
                if not issue:params['expected_version']=previous['version']
                state['pending']=({'method':'credentials.issue' if issue else 'credentials.renew','params':params},
                    '' if issue else previous['access_token'])
            message,token=state['pending']
            try:value=await self._wire(binding,message,token)
            except RuntimeErrorCode as exc:
                if exc.code=='WORKER_CREDENTIAL_EXPIRED':
                    state['pending']=None;state['value']=None;state['expires_at']=0
                raise
            remaining=value['expires_at']-value['server_time']
            if not 0<remaining<=901:raise RuntimeErrorCode('WORKER_CREDENTIAL_RESPONSE_INVALID')
            state.update(value=value,pending=None,refresh_at=time.monotonic()+min(value['renew_after_seconds'],remaining*2/3),
                expires_at=time.monotonic()+remaining)
            return value

    async def keep_credential(self,binding):
        delay=2
        while True:
            await asyncio.sleep(delay)
            try:
                await self.credential(binding);delay=2
            except RuntimeErrorCode as exc:
                if exc.code in {'WORKER_CREDENTIAL_REVOKED','AUTH_REVOKED','STALE_OWNER','WORK_POLICY_CHANGED','WORKER_CREDENTIAL_BINDING_CHANGED'}:return
                delay=min(30,delay*2)
            except (OSError,ValueError):delay=min(30,delay*2)


class RemoteWorkerToolChannel(WorkerToolChannel):
    def __init__(self,provider,binding,policy,uid):
        super().__init__(SimpleNamespace(store=provider.store),binding,policy,uid)
        self.provider=provider;self.worker_secret_refs=[];self.renewal=None

    async def open(self):
        value=await self.provider.request(self.binding,{'method':'describe','params':{}})
        self.worker_secret_refs=value['worker_secret_refs']
        mount=await self.listen()
        if self.provider.automatic:self.renewal=asyncio.create_task(self.provider.keep_credential(self.binding))
        return mount

    async def bind_session(self,session_id):
        if self.session_id is not None and self.session_id!=session_id:raise RuntimeErrorCode('SESSION_CHANGED')
        await self.provider.request(self.binding,{'method':'ready','params':{'session_id':session_id}})
        self.session_id=session_id

    async def handle(self,message):
        if self.closing:raise RuntimeErrorCode('TOOL_CHANNEL_CLOSED')
        return await self.provider.request(self.binding,message)

    async def _deny(self,code):
        # Remote Workers have no controller database; bind the host diagnostic at the gateway.
        await self.provider.request(self.binding,{'method':'channel_error','params':{'code':code}})

    async def verify_activity(self,activity):
        return await self.handle({'method':'verify_activity','params':{'activity':activity}})

    async def close(self):
        if self.renewal:
            self.renewal.cancel();await asyncio.gather(self.renewal,return_exceptions=True)
        await super().close()
        self.provider.credentials.pop(tuple(self.binding),None)
