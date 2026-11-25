"""Independent trusted Worker. One process/slot, one registered profile, durable ownership."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import socket
import time
from uuid import uuid4
from .adapter import LaunchSpec, Mount, RuntimeAdapter
from .artifacts import LocalArtifacts
from .bridge import JsonlBridge
from .database import RuntimeErrorCode
from .store import RuntimeStore, encoded, identifier
from .authorization import authorize_work
from .reporting import report_spool, enqueue_report, relay_reports, confirm_report
from .reconciliation import ExitReceipts, reconcile_run
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, InterfaceError
from .schema import runs


def read_profile(path, *, value=None):
    value = json.loads(Path(path).read_text(encoding='utf-8')) if value is None else value
    identifier(value['id'])
    if value.get('bridge') == 'a2a-0.3-jsonrpc':
        from .a2a_profile import read
        return read(value)
    if value.get('bridge') != 'jsonl-v1': raise RuntimeErrorCode('BRIDGE_UNSUPPORTED')
    config = dict(value['launch'])
    from .worker_tool_channel import profile_policy, POLICY_ENV, SOCKET_ENV
    environment=dict(config.get('environment',{}))
    if POLICY_ENV in environment or SOCKET_ENV in environment:raise RuntimeErrorCode('TOOL_CHANNEL_ENV_RESERVED')
    if value.get('tool_channel') is not None:
        if value.get('governance',{}).get('mode')!='e2ag':raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        environment[POLICY_ENV]=encoded(profile_policy(value['tool_channel'])).decode('utf-8')
    from .faults import POLICY_ENV as RECOVERY_ENV,policy as recovery_policy
    if RECOVERY_ENV in environment:raise RuntimeErrorCode('RECOVERY_ENV_RESERVED')
    if value.get('recovery') is not None:
        if value.get('governance',{}).get('mode')!='e2ag':raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        environment[RECOVERY_ENV]=encoded(recovery_policy(value['recovery'])).decode('utf-8')
    config['environment']=environment
    config['command'] = tuple(config['command'])
    config['mounts'] = tuple(Mount(**m) for m in config.get('mounts', []))
    spec = LaunchSpec(**config)
    spec.validate()
    if spec.kind != 'task': raise RuntimeErrorCode('WORKER_REQUIRES_TASK_PROFILE')
    return value['id'], spec


def boot_identity():
    path = Path('/proc/sys/kernel/random/boot_id')
    return path.read_text().strip() if path.exists() else 'unknown'


class Worker:
    def __init__(self, store: RuntimeStore, adapter: RuntimeAdapter, profile_id, spec: LaunchSpec,
                 artifacts: LocalArtifacts, *, poll_interval=.25, timeout=300, output_limit=1024*1024, authorization_verifier=None, secret_resolver=None, secret_requests=(), tool_gate=None, remote_tools=None):
        if not 0 < poll_interval <= 5 or not 0 < timeout <= 86400 or not 1 <= output_limit <= 4*1024*1024:
            raise RuntimeErrorCode('INVALID_WORKER_LIMIT')
        spec.validate()
        if spec.kind != 'task': raise RuntimeErrorCode('WORKER_REQUIRES_TASK_PROFILE')
        self.store, self.adapter, self.profile_id, self.spec = store, adapter, profile_id, spec
        self.artifacts = artifacts
        from .faults import from_spec
        self.recovery_policy=from_spec(spec)
        self.tool_gate=tool_gate;self.remote_tools=remote_tools
        if tool_gate is not None and remote_tools is not None:raise RuntimeErrorCode('TOOL_PROVIDER_CONFLICT')
        if remote_tools and 'DIOS_TOOL_CHANNEL_POLICY' not in spec.environment:raise RuntimeErrorCode('TOOL_CHANNEL_PROFILE_INVALID')
        self.spool = report_spool(artifacts, store.scope)
        self.receipts = ExitReceipts(artifacts, store.scope)
        self.authorization_verifier = authorization_verifier
        self.secret_resolver, self.secret_requests = secret_resolver, list(secret_requests)
        self.poll_interval, self.timeout, self.output_limit = poll_interval, timeout, output_limit
        self.worker_id, self.instance = (getattr(store,'worker_id',None) or (remote_tools.worker_id if remote_tools else str(uuid4()))), str(uuid4())
        self.boot_id = boot_identity()
        self.stopping = False
        self._recovery_cursor = None

    async def once(self):
        await self.store.register_worker(self.worker_id, self.instance, socket.gethostname(), os.getpid(), slots=1)
        a = await self.store.claim(self.worker_id, self.instance, profile_id=self.profile_id,recovery_policy=self.recovery_policy)
        if a is None: return {'worker_id':self.worker_id,'state':'idle'}
        binding = (a['run_id'], a['id'], self.worker_id, self.instance, a['epoch'])
        budget_observation_started = time.monotonic()
        run = await self.store.get(a['run_id'])
        budget = run.get('execution_budget')
        # Conservatively subtract the full observation round trip; host clocks need not agree.
        budget_deadline = budget_observation_started+budget['remaining_seconds'] if budget else None
        if run['input'].get('profile_digest') != self.spec.fingerprint():
            await self.store.reject_unlaunched(binding,'PROFILE_CHANGED')
            return await self.store.get(a['run_id'])
        channel=None
        handle, started, paused_instance = None, None, None
        async def maintain_authority():
            if handle is None:return  # No Grant exists yet; initial wait stays below the claim lease.
            if budget_deadline is not None and time.monotonic() >= budget_deadline:
                raise RuntimeErrorCode('RUN_BUDGET_EXHAUSTED')
            if started is not None and time.monotonic()-started >= self.timeout:
                raise RuntimeErrorCode('PROCESS_DEADLINE_EXCEEDED')
            await self.store.heartbeat(*binding)
            if paused_instance is not None:
                current=await self.adapter.observe(handle)
                if current.process_instance!=paused_instance:raise RuntimeErrorCode('PROCESS_CHANGED')
                await observe(current)
        async def suspend_identity():
            nonlocal paused_instance
            if handle is None:return
            current=await self.adapter.observe(handle)
            if not current.alive and current.state in {'exited','dead'}:return
            if not callable(getattr(self.adapter,'pause',None)) or not callable(getattr(self.adapter,'resume',None)):
                raise RuntimeErrorCode('AUTH_PROVIDER_UNAVAILABLE')
            paused=await self.adapter.pause(handle,current.process_instance)
            await observe(paused)
            if paused.state!='paused' or not paused.alive:
                raise RuntimeErrorCode('RUNTIME_PAUSE_UNCONFIRMED')
            paused_instance=paused.process_instance
        async def resume_identity():
            nonlocal paused_instance
            if paused_instance is None:return
            resumed=await self.adapter.resume(handle,paused_instance)
            await observe(resumed)
            if resumed.state not in {'running','exited','dead'}:
                raise RuntimeErrorCode('RUNTIME_RESUME_UNCONFIRMED')
            paused_instance=None
        async def authorized(action,session_id=None):
            from .identity_wait import identity_wait
            async def check():
                return await authorize_work(self.store,await self.store.get(a['run_id']),a,action,
                    session_id=session_id,verifier=self.authorization_verifier)
            await identity_wait(check,maintain=maintain_authority,suspend=suspend_identity,resume=resume_identity,
                timeout=min(60,self.timeout-(time.monotonic()-started) if started is not None else 15))
        try:
            await authorized('execute')
            secret_mounts = ()
            from .worker_tool_channel import WorkerToolChannel, POLICY_ENV, SOCKET_ENV, MOUNT_PATH
            if POLICY_ENV in self.spec.environment:
                if self.tool_gate is None and self.remote_tools is None:raise RuntimeErrorCode('TOOL_GATE_NOT_CONFIGURED')
                if self.adapter.name!='local-docker-v1':raise RuntimeErrorCode('TOOL_CHANNEL_RUNTIME_UNSUPPORTED')
                if not self.spec.user.split(':')[0].isdigit() or int(self.spec.user.split(':')[0])==0:raise RuntimeErrorCode('TOOL_CHANNEL_NUMERIC_USER_REQUIRED')
                if any(m.target==MOUNT_PATH or m.target.startswith(MOUNT_PATH+'/') for m in self.spec.mounts+secret_mounts):
                    raise RuntimeErrorCode('SECRET_TARGET_CONFLICT')
                policy=json.loads(self.spec.environment[POLICY_ENV]);uid=int(self.spec.user.split(':')[0])
                channel=self.remote_tools.channel(binding,policy,uid) if self.remote_tools else WorkerToolChannel(self.tool_gate,binding,policy,uid)
                secret_mounts=secret_mounts+(await channel.open(),)
            if self.secret_requests:
                if not run['input'].get('work_authorization_ref'):
                    raise RuntimeErrorCode('SECRET_AUTHORIZATION_REQUIRED')
                if run['input'].get('secret_refs') != self.secret_requests:
                    raise RuntimeErrorCode('SECRET_REFERENCE_CHANGED')
                requested=self.secret_requests
                if self.remote_tools:requested=[x for x in requested if x['ref'] in channel.worker_secret_refs]
                if requested:
                    if self.secret_resolver is None:raise RuntimeErrorCode('SECRET_AUTHORIZATION_REQUIRED')
                    resolved=await asyncio.to_thread(self.secret_resolver.resolve,self.profile_id,requested)
                    if channel and any(m.target==MOUNT_PATH or m.target.startswith(MOUNT_PATH+'/') or MOUNT_PATH.startswith(m.target.rstrip('/')+'/') for m in resolved):
                        raise RuntimeErrorCode('SECRET_TARGET_CONFLICT')
                    secret_mounts=secret_mounts+resolved
                if {m.target for m in self.spec.mounts}.intersection(m.target for m in secret_mounts):
                    raise RuntimeErrorCode('SECRET_TARGET_CONFLICT')

        except Exception as exc:
            if channel:await channel.close()
            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'WORK_PREFLIGHT_UNAVAILABLE'
            await self.store.reject_unlaunched(binding, code)
            return await self.store.get(a['run_id'])
        handle = a['id']
        spec = replace(self.spec, mounts=self.spec.mounts+secret_mounts, environment=dict(self.spec.environment,
            DIOS_RUN_ID=a['run_id'], DIOS_ATTEMPT_ID=a['id'],
            DIOS_WORK_INPUT=encoded(run['input'].get('payload',{})).decode('utf-8')))
        if channel:spec=replace(spec,environment=dict(spec.environment,**{SOCKET_ENV:MOUNT_PATH+'/channel.sock'}))
        bridge = JsonlBridge()
        sequence, phase, output_count = 0, 'claimed', 0
        last_receipt, code = None, None
        usage_cursor = 0
        async def report(next_phase, *, output=False, usage=None):
            nonlocal sequence, phase, last_receipt
            sequence += 1
            last_receipt = await enqueue_report(self.store, self.spool, binding, sequence, next_phase,
                session_id=bridge.session_id, output=output, usage=usage,
                output_excerpt=bridge.output_excerpt if output else None,
                output_observed_at=time.time() if output else None, authorization_epoch=a['authorization_epoch'])
            phase = next_phase
        async def observe(observation):
            await self.store.observe_process(binding, observation, adapter=self.adapter.name, boot_id=self.boot_id)
        async def retain(logs):
            for kind, data in [('stdout',logs.stdout),('stderr',logs.stderr)]:
                if data:
                    blob = await asyncio.to_thread(self.artifacts.put,[data])
                    await self.store.retain_output(binding,self.artifacts,blob.key,kind)
        try:
            # Save launch intent before touching the execution backend.
            sequence += 1
            last_receipt = await enqueue_report(self.store,self.spool,binding,sequence,'preparing',
                authorization_epoch=a['authorization_epoch'],remote={
                'launch':{'handle':handle,'adapter':self.adapter.name,'profile_id':self.profile_id,
                          'profile_digest':self.spec.fingerprint(),'worker_boot_id':self.boot_id}})
            phase = 'preparing'
            await report('creating_session')
            await confirm_report(self.store,self.spool,last_receipt)
            # Check current ownership/authorization after the asynchronous verifier.
            await self.store.heartbeat(*binding)
            if budget_deadline is not None and time.monotonic() >= budget_deadline:
                raise RuntimeErrorCode('RUN_BUDGET_EXHAUSTED')
            observation = await self.adapter.start(handle,spec)
            await observe(observation)
            started, heartbeat_at = time.monotonic(), time.monotonic()
            code = None
            while True:
                # Database read failures cannot be interpreted as process death.
                budget_expired = budget_deadline is not None and time.monotonic() >= budget_deadline
                if budget_expired:
                    current = run
                elif budget_deadline is not None:
                    try:
                        current = await asyncio.wait_for(self.store.get(a['run_id']),max(.001,budget_deadline-time.monotonic()))
                    except TimeoutError:
                        if time.monotonic()<budget_deadline:raise
                        current,budget_expired=run,True
                else:
                    current = await self.store.get(a['run_id'])
                cancelled = current['cancel_requested']
                revoked = current['revoked']
                budget_expired = budget_expired or bool(current.get('execution_budget') and current['execution_budget']['exhausted'])
                if cancelled or revoked or budget_expired or time.monotonic()-started >= self.timeout:
                    observation = await self.adapter.stop(handle)
                    await observe(observation)
                    code = 'AUTH_REVOKED' if revoked else 'RUN_BUDGET_EXHAUSTED' if budget_expired else 'PROCESS_DEADLINE_EXCEEDED'
                else:
                    observation = await self.adapter.observe(handle)
                    await observe(observation)
                if observation.state == 'absent':
                    await self.store.mark_unknown(binding,'RUNTIME_DISAPPEARED')
                    return await self.store.get(a['run_id'])
                logs = await self.adapter.logs(handle,self.output_limit)
                if logs.truncated: code = 'OUTPUT_LIMIT_EXCEEDED'
                if not cancelled and not revoked and not code:
                    try:
                        bridge.feed(logs.stdout)
                        if bridge.session_id and phase == 'creating_session':
                            if run['input'].get('work_authorization_ref'):
                                await authorized('execute',bridge.session_id)
                            await report('waiting_model')
                            if channel:await channel.bind_session(bridge.session_id)
                        if bridge.output_count > output_count:
                            await report('streaming',output=True)
                            output_count = bridge.output_count
                    except RuntimeErrorCode as exc:
                        code = exc.code
                    if phase!='creating_session':
                        while usage_cursor < len(bridge.usage):
                            batch=bridge.usage[usage_cursor:usage_cursor+16]
                            await report(phase,usage=batch)
                            usage_cursor+=len(batch)
                if code and observation.alive:
                    observation = await self.adapter.stop(handle)
                    await observe(observation)
                    logs = await self.adapter.logs(handle,self.output_limit)
                if not observation.alive:
                    cancelled=(await self.store.get(a['run_id']))['cancel_requested']
                    # "Created" or a missing container does not prove execution/cleanup.
                    if observation.state not in {'exited','dead'}:
                        await retain(logs)
                        await self.store.mark_unknown(binding,'RUNTIME_EXIT_UNCONFIRMED')
                        return await self.store.get(a['run_id'])
                    # Persist original bytes and exit evidence before controller writes.
                    await asyncio.to_thread(self.receipts.save, a, observation, logs, failure=code)
                    await retain(logs)
                    if cancelled:
                        await self.store.confirm_cancel(*binding,'runtime-exit:'+observation.process_instance)
                    else:
                        recover_complete=bridge.result is not None and self.recovery_policy and self.recovery_policy['accept_complete_on_error']
                        if not code and bridge.result is not None and self.recovery_policy and observation.exit_code != 0 and not recover_complete:
                            code='RESULT_EXIT_CONFLICT'
                        code = code or ('PROCESS_EXITED' if observation.exit_code != 0 and not recover_complete else None)
                        if not code:
                            try:
                                bridge.feed(logs.stdout,final=True)
                                from .result_contract import validate_result
                                validate_result(run['input'],bridge.result)
                            except RuntimeErrorCode as exc: code = exc.code
                        if code:
                            await self.store.finish_failed(binding,code)
                        else:
                            if phase == 'creating_session':
                                if run['input'].get('work_authorization_ref'):
                                    await authorized('execute',bridge.session_id)
                                await report('waiting_model')
                            await report('validating')
                            await confirm_report(self.store,self.spool,last_receipt)
                            await authorized('accept_result',bridge.session_id)
                            blob = await asyncio.to_thread(self.artifacts.put,[encoded(bridge.result)])
                            await self.store.complete(*binding,self.artifacts,blob.key,JsonlBridge.validate)
                    # Explicit cleanup follows durable terminal state and raw-output retention.
                    await self.adapter.remove(handle)
                    return await self.store.get(a['run_id'])
                await relay_reports(self.store, self.spool)
                if time.monotonic()-heartbeat_at >= 5:
                    if run['input'].get('work_authorization_ref'):
                        try:
                            await authorized('execute',bridge.session_id)
                            if self.secret_requests and requested:
                                await asyncio.to_thread(self.secret_resolver.resolve,self.profile_id,requested)
                        except Exception as exc:
                            code=exc.code if isinstance(exc,RuntimeErrorCode) else 'WORK_AUTHORIZATION_UNAVAILABLE'
                            # Offline execution is disabled for governed work. Preserve exit output.
                            observation=await self.adapter.stop(handle)
                            await observe(observation)
                            continue
                    await self.store.heartbeat(*binding)
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            # Abrupt Worker loss leaves a discoverable workload; restart must reconcile it.
            raise
        except Exception as exc:
            # Best effort local evidence, independent of controller availability.
            # A live workload stays discoverable; a read error never proves its death.
            exception_code = exc.code if isinstance(exc,RuntimeErrorCode) else 'RUNTIME_OBSERVATION_UNAVAILABLE'
            try:
                observed = await self.adapter.stop(handle) if run['input'].get('work_authorization_ref') or budget else await self.adapter.observe(handle)
                if not observed.alive and observed.state in {'exited','dead'}:
                    saved_logs = await self.adapter.logs(handle,self.output_limit)
                    await asyncio.to_thread(self.receipts.save,a,observed,saved_logs,
                        failure=exception_code if exception_code=='RUN_BUDGET_EXHAUSTED' else code)
            except Exception:
                pass
            code = exc.code if isinstance(exc,RuntimeErrorCode) else 'RUNTIME_OBSERVATION_UNAVAILABLE'
            try:
                await self.store.mark_unknown(binding,code)
            except Exception:
                # Storage unavailable: lease expiration supplies the durable uncertainty state.
                pass
            raise
        finally:
            if channel:await channel.close()

    async def recover(self, *, limit=100):
        """Bounded maintenance pass, including while the scope is draining."""
        if type(limit) is not int or not 1 <= limit <= 100: raise RuntimeErrorCode('INVALID_LIMIT')
        if hasattr(self.store,'recover_worker'):return await self.store.recover_worker(self,limit)
        await relay_reports(self.store, self.spool)
        if self.tool_gate:self.last_tool_recovery=await self.tool_gate.recover_pending(self.profile_id,limit)
        await self.store.expire()
        async with self.store.db.read(self.store.scope) as c:
            query = select(runs.c.id).where(runs.c.scope == self.store.scope,
                runs.c.state == 'reconciliation_required',
                runs.c.input['runtime_profile'].as_string() == self.profile_id)
            if self._recovery_cursor: query = query.where(runs.c.id > self._recovery_cursor)
            ids = (await c.execute(query.order_by(runs.c.id).limit(limit))).scalars().all()
        # Cycle through unresolved work; an old live/blocked batch cannot starve later runs.
        self._recovery_cursor = ids[-1] if len(ids) == limit else None
        results = []
        for run_id in ids:
            try:
                result = await reconcile_run(self.store,self.adapter,self.artifacts,run_id,
                    output_limit=self.output_limit,verifier=self.authorization_verifier)
                results.append({'run_id':run_id,'state':result['state']})
            except (RuntimeErrorCode, ConnectionError, TimeoutError) as exc:
                results.append({'run_id':run_id,'error':getattr(exc,'code','OBSERVATION_UNAVAILABLE')})
        self.last_recovery = results
        return results

    async def serve(self, *, once=False):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):
            try: loop.add_signal_handler(sig,lambda:setattr(self,'stopping',True))
            except (NotImplementedError,RuntimeError): pass
        try:
            recovered_at, backoff = 0, 1
            while not self.stopping:
                try:
                    if time.monotonic()-recovered_at >= 5:
                        await self.recover()
                        recovered_at = time.monotonic()
                    result = await self.once()
                    backoff = 1
                except (ConnectionError, TimeoutError, OperationalError, InterfaceError):
                    if once: raise
                    # One probe at a time; no new claims while storage is unavailable.
                    await asyncio.sleep(backoff)
                    backoff = min(30, backoff*2)
                    continue
                except RuntimeErrorCode as exc:
                    if once or exc.code not in {'REPORT_DELIVERY_PENDING','WRITE_QUEUE_FULL',
                            'WRITE_QUEUE_TIMEOUT','WRITE_DRAINING','STALE_OWNER','AUTH_REVOKED'}: raise
                    await asyncio.sleep(backoff)
                    backoff = min(30,backoff*2)
                    continue
                if once: return result
                if result.get('state')=='idle': await asyncio.sleep(self.poll_interval)
        finally:
            try: await self.store.drain(self.worker_id)
            except (RuntimeErrorCode, OperationalError, InterfaceError, ConnectionError, TimeoutError): pass
            for sig in (signal.SIGTERM,signal.SIGINT):
                try: loop.remove_signal_handler(sig)
                except (NotImplementedError,RuntimeError): pass
        return {'worker_id':self.worker_id,'state':'drained'}
