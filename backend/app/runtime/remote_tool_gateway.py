"""Dedicated mTLS tool Provider. A single authority; no public operator API."""
import asyncio
import copy
import json
import time
from .database import RuntimeErrorCode
from .store import encoded, identifier, digest
from .worker_identity import PROTOCOL, document, peer_identity
from .worker_tool_channel import WorkerToolChannel, POLICY_ENV
from .work_authorizations import audit
from app.services.e2ag import Decision

REQUEST_LIMIT=98304
RESPONSE_LIMIT=2*1024*1024


class ToolGateway:
    def __init__(self,gate,identities,profiles,credentials=None):
        self.gate,self.identities,self.profiles=gate,identities,profiles
        self.credentials=credentials
        self.runtime_control=None
        self.active={};self.tasks=set();self.server=None;self.stopping=False
        self.last_recovery=[];self.recovery_gates={}

    async def handle(self,fingerprint,certificate_expiry,envelope):
        if self.stopping:raise RuntimeErrorCode('WORKER_GATEWAY_DRAINING')
        if isinstance(envelope,dict) and isinstance(envelope.get('message'),dict) and str(envelope['message'].get('method','')).startswith('runtime.'):
            if self.runtime_control is None:raise RuntimeErrorCode('REMOTE_CONTROL_DISABLED')
            return await self.runtime_control.handle(fingerprint,certificate_expiry,envelope)
        fields={'protocol','audience','scope','profile','profile_digest','binding','message','access_token'}
        if not isinstance(envelope,dict) or set(envelope) not in (fields,fields|{'credential_mode'}) or envelope['protocol']!=PROTOCOL:
            raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        if envelope['audience']!=self.identities.audience or envelope['scope']!=self.gate.store.scope:
            raise RuntimeErrorCode('WORKER_IDENTITY_SCOPE_DENIED')
        binding=envelope['binding']
        if not isinstance(binding,list) or len(binding)!=5 or type(binding[4]) is not int or binding[4]<1:
            raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        for value in binding[:4]:identifier(value)
        profile=envelope['profile'];identifier(profile)
        mode=envelope.get('credential_mode','file')
        if mode not in {'file','automatic'}:raise RuntimeErrorCode('WORKER_CREDENTIAL_MODE_INVALID')
        message=envelope['message']
        if not isinstance(message,dict) or set(message)!={'method','params'}:raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
        exchange=message.get('method') in {'credentials.issue','credentials.renew'}
        if (exchange or mode=='automatic') and self.credentials is None:raise RuntimeErrorCode('CREDENTIAL_ISSUER_NOT_CONFIGURED')
        if exchange and mode!='automatic':raise RuntimeErrorCode('WORKER_CREDENTIAL_MODE_INVALID')
        bound_entry=None;bound_spec=None;bound_revision=None
        async def identity_guard(connection=None):
            if time.time()>=certificate_expiry:raise RuntimeErrorCode('WORKER_CERTIFICATE_EXPIRED')
            if mode=='automatic':
                if exchange:
                    machine=await asyncio.to_thread(self.identities.machine,fingerprint,envelope['scope'],profile,binding[2])
                    peer={'subject':machine['subject'],'certificate_sha256':fingerprint,'worker_id':binding[2],'audience':self.identities.audience}
                else:peer=await self.credentials.authorize(fingerprint,certificate_expiry,envelope,connection)
            else:peer=await asyncio.to_thread(self.identities.authorize,fingerprint,envelope['scope'],profile,binding[2],envelope['access_token'])
            if bound_entry is not None:
                fresh=await asyncio.to_thread(document,self.profiles)
                if fresh.get('profiles',{}).get(profile)!=bound_entry:raise RuntimeErrorCode('WORKER_PROFILE_CHANGED')
                from .remote_profiles import resolve
                from .remote_control import TransactionView
                database=self.gate.store.db if connection is None else TransactionView(self.gate.store.db,connection,envelope['scope'])
                await resolve(database,envelope['scope'],bound_entry,profile,bound_spec,bound_revision)
            return peer
        peer=await identity_guard()
        # Profile and tool mapping are server-owned; callers cannot supply an allowlist.
        config=await asyncio.to_thread(document,self.profiles)
        entry=config.get('profiles',{}).get(profile)
        if not isinstance(entry,dict) or entry.get('enabled') is not True:raise RuntimeErrorCode('WORKER_PROFILE_DENIED')
        from .remote_profiles import resolve
        async with self.gate.store.db.read(envelope['scope']) as c:
            selected_run=await self.gate.store._run(c,binding[0])
        bound_revision=selected_run['input'].get('revision_ref')
        spec,_=await resolve(self.gate.store.db,envelope['scope'],entry,profile,envelope['profile_digest'],bound_revision)
        if POLICY_ENV not in spec.environment:
            raise RuntimeErrorCode('PROFILE_CHANGED')
        bound_entry=entry;bound_spec=spec.fingerprint()
        # One in-flight message per Worker, bounded globally. Retry uses the same call identity.
        if binding[2] in self.active or len(self.active)>=64:raise RuntimeErrorCode('WORKER_GATEWAY_BUSY')
        self.active[binding[2]]=binding[1]
        try:
            gate=copy.copy(self.gate);gate.identity_guard=identity_guard;gate.sender_evidence=peer
            channel=WorkerToolChannel(gate,tuple(binding),json.loads(spec.environment[POLICY_ENV]),-1)
            run,a=await channel._owner(wait=True)
            if run['input']['runtime_profile']!=profile or run['input']['profile_digest']!=spec.fingerprint():
                raise RuntimeErrorCode('PROFILE_CHANGED')
            await channel.initialize()
            # This provider's model/Agent mounts are explicitly partitioned from gateway secrets.
            mount_refs=channel.policy.get('worker_secret_refs',[])
            if not isinstance(mount_refs,list) or any(not isinstance(x,str) for x in mount_refs) or len(set(mount_refs))!=len(mount_refs):
                raise RuntimeErrorCode('SECRET_ACCESS_DENIED')
            known={x['ref'] for x in run['input'].get('secret_refs',[])}
            tool_refs={x['credential_ref']['ref'] for x in channel.registered.values() if x.get('credential_ref')}
            if set(mount_refs)-known or set(mount_refs)&tool_refs:raise RuntimeErrorCode('SECRET_TARGET_CONFLICT')
            if a['session_id'] is not None:await channel.bind_session(a['session_id'])
            await identity_guard()
            # Audit binds the TLS sender to the original execution, before any business action.
            async with gate.store.db.transaction(gate.store.scope) as c:
                await gate.store._owned(c,*binding,authorize=True)
                now=await gate.store.db.now(c)
                await audit(c,gate.store.scope,binding[0],binding[1],'remote-tool-channel',
                    Decision('worker-identity','allow',('WORKER_CERTIFICATE_BOUND',),evidence={**peer,
                        'worker_instance':binding[3],'lease_epoch':binding[4],'message_digest':digest(envelope['message'])}),now)
            if message['method']=='channel_error':
                params=message['params']
                if not isinstance(params,dict) or set(params)!={'code'}:raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
                identifier(params['code'])
                await channel._deny(params['code'])
                return {'recorded':True}
            value=await self.credentials.exchange(fingerprint,certificate_expiry,envelope) if exchange else await channel.handle(envelope['message'])
            if envelope['message'].get('method')=='describe':value={**value,'worker_secret_refs':mount_refs}
            return value
        finally:self.active.pop(binding[2],None)

    async def connection(self,reader,writer):
        if len(self.tasks)>=64 or self.stopping:writer.close();return
        task=asyncio.current_task();self.tasks.add(task)
        try:
            fingerprint,expiry=peer_identity(writer)
            async with asyncio.timeout(110):
                async with asyncio.timeout(10):raw=await reader.readline()
                if not raw.endswith(b'\n') or len(raw)>REQUEST_LIMIT:raise RuntimeErrorCode('WORKER_GATEWAY_FRAME_INVALID')
                value=await self.handle(fingerprint,expiry,json.loads(raw.decode('utf-8')))
                response=encoded({'ok':True,'value':value})+b'\n'
                if len(response)>RESPONSE_LIMIT:raise RuntimeErrorCode('TOOL_RESULT_TOO_LARGE')
                writer.write(response);await writer.drain()
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'WORKER_GATEWAY_UNAVAILABLE'
            try:
                writer.write(encoded({'ok':False,'error':code})+b'\n');await writer.drain()
            except (OSError,ConnectionError):pass
        finally:
            self.tasks.discard(task);writer.close()
            try:await asyncio.wait_for(writer.wait_closed(),3)
            except (OSError,TimeoutError):pass

    async def maintain(self):
        while not self.stopping:
            try:
                if self.runtime_control is not None:await self.runtime_control.store.expire()
                config=await asyncio.to_thread(document,self.profiles)
                results=[]
                profiles=config.get('profiles',{})
                if not isinstance(profiles,dict) or len(profiles)>64:raise RuntimeErrorCode('WORKER_PROFILE_CONFIG_INVALID')
                for profile in profiles:
                    if profile not in self.recovery_gates:
                        recovery=copy.copy(self.gate);recovery._receipt_cursor=None;self.recovery_gates[profile]=recovery
                    results.extend(await self.recovery_gates[profile].recover_pending(profile,50))
                for profile in set(self.recovery_gates)-set(profiles):del self.recovery_gates[profile]
                self.last_recovery=results
            except Exception:self.last_recovery=[{'error':'WORKER_GATEWAY_RECOVERY_UNAVAILABLE'}]
            await asyncio.sleep(5)

    async def serve(self,host,port,context,stop):
        self.server=await asyncio.start_server(self.connection,host,port,ssl=context,
            ssl_handshake_timeout=10,limit=REQUEST_LIMIT+1)
        maintenance=asyncio.create_task(self.maintain())
        try:await stop.wait()
        finally:
            self.stopping=True;self.server.close();await self.server.wait_closed()
            # Graceful shutdown waits for accepted calls; their receipts precede response delivery.
            await asyncio.gather(*list(self.tasks),return_exceptions=True)
            maintenance.cancel();await asyncio.gather(maintenance,return_exceptions=True)
