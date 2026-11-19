"""Worker-side RuntimeStore protocol over mTLS; no database URL or shared artifact directory."""
import asyncio
import base64
from dataclasses import asdict
import socket
import os
from uuid import uuid4
from .database import RuntimeErrorCode
from .store import digest,TERMINAL
from .worker_identity import PROTOCOL
from .reconciliation import reconcile_run
from .reporting import relay_reports


class RemoteRuntimeStore:
    def __init__(self,scope,transport):
        self.scope,self.transport=scope,transport
        self.worker_id=transport.worker_id;self.instance=None;self.bindings={};self.pending_claim=None;self.cursor=None
    async def call(self,method,params,request_id=None):
        e=dict(protocol=PROTOCOL,audience=self.transport.config['audience'],scope=self.scope,
            profile=self.transport.profile,profile_digest=self.transport.spec.fingerprint(),worker_id=self.worker_id,
            instance=self.instance,request_id=request_id or digest([method,params]),message={'method':'runtime.'+method,'params':params})
        try:return await self.transport.exchange(e)
        except RuntimeErrorCode as exc:
            if exc.code in {'WORKER_GATEWAY_OBSERVATION_UNAVAILABLE','WORKER_GATEWAY_UNAVAILABLE','WORKER_GATEWAY_DRAINING','WORKER_GATEWAY_BUSY'}:
                raise ConnectionError(exc.code) from None
            raise
    async def bound(self,method,binding,**params):return await self.call(method,dict(binding=list(binding),**params))
    async def register_worker(self,worker_id,instance,host,pid,slots=1):
        if worker_id!=self.worker_id or slots!=1:raise RuntimeErrorCode('WORKER_MISMATCH')
        self.instance=instance
        return await self.call('register',dict(host=host,pid=pid))
    async def claim(self,worker_id,instance,*,profile_id,recovery_policy=None):
        if (worker_id,instance,profile_id)!=(self.worker_id,self.instance,self.transport.profile):raise RuntimeErrorCode('WORKER_MISMATCH')
        self.pending_claim=self.pending_claim or uuid4().hex
        a=await self.call('claim',{},self.pending_claim);self.pending_claim=None
        if a:self.bindings[a['run_id']]=[a['run_id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch']]
        return a
    async def get(self,run_id):return await self.bound('get',self.bindings[run_id])
    async def drain(self,worker_id):
        if worker_id!=self.worker_id:raise RuntimeErrorCode('WORKER_MISMATCH')
        return await self.call('drain',{})
    async def expire(self):return 0  # Authoritative expiry is performed by the control plane.
    async def heartbeat(self,*binding):return await self.bound('heartbeat',binding)
    async def report(self,run_id,attempt_id,worker_id,instance,epoch,sequence,phase,**details):
        return await self.bound('report',[run_id,attempt_id,worker_id,instance,epoch],report=dict(sequence=sequence,phase=phase,**details))
    async def observe_process(self,binding,observation,*,adapter,boot_id):
        return await self.bound('observe',binding,observation=asdict(observation),adapter=adapter,boot_id=boot_id)
    async def reject_unlaunched(self,binding,code):return await self.bound('reject_unlaunched',binding,code=code)
    async def finish_failed(self,binding,code):return await self.bound('finish_failed',binding,code=code)
    async def mark_unknown(self,binding,code):return await self.bound('mark_unknown',binding,code=code)
    async def confirm_cancel(self,run_id,attempt_id,worker_id,instance,epoch,evidence_ref):
        return await self.bound('confirm_cancel',[run_id,attempt_id,worker_id,instance,epoch],evidence_ref=evidence_ref)
    async def verifier(self,context,action):
        return await self.bound('authorize',[context.run_id,context.attempt_id,context.worker_id,context.worker_instance,context.lease_epoch],
            action=action,session_id=context.session_id)
    async def upload(self,binding,provider,key):
        blob=await asyncio.to_thread(provider.verify,key)
        state=await self.bound('upload.begin',binding,key=key,size=blob.size)
        if state['complete']:return
        offset=state['received']
        # Never decode binary/UTF-8 at transport chunk boundaries.
        with provider.path(key).open('rb') as stream:
            stream.seek(offset)
            while data:=stream.read(65536):
                response=await self.bound('upload.chunk',binding,key=key,offset=offset,data=base64.b64encode(data).decode())
                if response['received']!=offset+len(data):raise RuntimeErrorCode('UPLOAD_OFFSET_CONFLICT')
                offset=response['received']
        await self.bound('upload.finish',binding,key=key)
    async def retain_output(self,binding,provider,key,kind):
        await self.upload(binding,provider,key)
        return await self.bound('retain',binding,key=key,kind=kind)
    async def complete(self,run_id,attempt_id,worker_id,instance,epoch,provider,key,validator):
        b=[run_id,attempt_id,worker_id,instance,epoch]
        await self.upload(b,provider,key)
        return await self.bound('complete',b,key=key)
    async def resolve_exit(self,run,a,receipt,provider,*,session_id,result_key,failure):
        b=[run['id'],a['id'],a['worker_id'],a['worker_instance'],a['epoch']]
        for key in (receipt['stdout'],receipt['stderr'],result_key):
            if key is not None:await self.upload(b,provider,key)
        return await self.bound('resolve_exit',b,version=run['version'],receipt=receipt,session_id=session_id,result_key=result_key,failure=failure)
    async def recover_worker(self,worker,limit):
        await self.register_worker(worker.worker_id,worker.instance,socket.gethostname(),os.getpid())
        await relay_reports(self,worker.spool)
        rows=await self.call('recoverable',{'cursor':self.cursor,'limit':limit})
        self.cursor=rows[-1]['run_id'] if len(rows)==limit else None
        result=[]
        for row in rows:
            run_id=row['run_id'];self.bindings[run_id]=row['binding']
            try:
                run=await self.get(run_id);a=run['attempts'][0]
                # Fenced, governed orphan workloads cannot keep executing offline.
                launch=a['remote'].get('launch',{})
                if launch.get('adapter')==worker.adapter.name and launch.get('handle')==a['id']:
                    obs=await worker.adapter.observe(a['id'])
                    if obs.alive:
                        if a['process_instance'] and a['process_instance']!=obs.process_instance:raise RuntimeErrorCode('PROCESS_CHANGED')
                        await worker.adapter.stop(a['id'])
                current=await reconcile_run(self,worker.adapter,worker.artifacts,run_id,output_limit=worker.output_limit,verifier=self.verifier)
                if current['state'] in TERMINAL:await worker.adapter.remove(a['id'])
                result.append({'run_id':run_id,'state':current['state']})
            except (RuntimeErrorCode,ConnectionError,TimeoutError) as exc:
                result.append({'run_id':run_id,'error':getattr(exc,'code','OBSERVATION_UNAVAILABLE')})
        worker.last_recovery=result;return result


async def serve_remote(args):
    from pathlib import Path
    from types import SimpleNamespace
    from .worker import Worker,read_profile
    from .work_policy import security_profile
    from .remote_tool_channel import RemoteToolProvider
    from .artifacts import LocalArtifacts
    from .secret_provider import FileSecretMounts
    from .docker_adapter import DockerRuntimeAdapter
    from .worker_tool_channel import POLICY_ENV
    profile,spec=read_profile(args.profile);mode,secret_refs=security_profile(args.profile)
    if mode!='e2ag':raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
    transport=RemoteToolProvider(args.control_file,SimpleNamespace(scope=args.scope),profile,spec)
    if not transport.automatic:raise RuntimeErrorCode('WORKER_CREDENTIAL_MODE_INVALID')
    store=RemoteRuntimeStore(args.scope,transport);transport.store=store
    worker=Worker(store,DockerRuntimeAdapter(args.scope),profile,spec,LocalArtifacts(Path(args.artifacts)),
        timeout=args.timeout,poll_interval=args.poll_interval,authorization_verifier=store.verifier,
        remote_tools=transport if POLICY_ENV in spec.environment else None,
        secret_requests=secret_refs,secret_resolver=FileSecretMounts(args.secrets_file,args.scope) if args.secrets_file else None)
    return await worker.serve(once=args.once)
