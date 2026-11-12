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
from .reporting import report_spool, enqueue_report, relay_reports
from .reconciliation import ExitReceipts, reconcile_run
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, InterfaceError
from .schema import runs


def read_profile(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    identifier(value['id'])
    if value.get('bridge') != 'jsonl-v1': raise RuntimeErrorCode('BRIDGE_UNSUPPORTED')
    config = dict(value['launch'])
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
                 artifacts: LocalArtifacts, *, poll_interval=.25, timeout=300, output_limit=1024*1024, authorization_verifier=None):
        if not 0 < poll_interval <= 5 or not 0 < timeout <= 86400 or not 1 <= output_limit <= 4*1024*1024:
            raise RuntimeErrorCode('INVALID_WORKER_LIMIT')
        spec.validate()
        if spec.kind != 'task': raise RuntimeErrorCode('WORKER_REQUIRES_TASK_PROFILE')
        self.store, self.adapter, self.profile_id, self.spec = store, adapter, profile_id, spec
        self.artifacts = artifacts
        self.spool = report_spool(artifacts, store.scope)
        self.receipts = ExitReceipts(artifacts, store.scope)
        self.authorization_verifier = authorization_verifier
        self.poll_interval, self.timeout, self.output_limit = poll_interval, timeout, output_limit
        self.worker_id, self.instance = str(uuid4()), str(uuid4())
        self.boot_id = boot_identity()
        self.stopping = False
        self._recovery_cursor = None

    async def once(self):
        await self.store.register_worker(self.worker_id, self.instance, socket.gethostname(), os.getpid(), slots=1)
        a = await self.store.claim(self.worker_id, self.instance, profile_id=self.profile_id)
        if a is None: return {'worker_id':self.worker_id,'state':'idle'}
        binding = (a['run_id'], a['id'], self.worker_id, self.instance, a['epoch'])
        run = await self.store.get(a['run_id'])
        if run['input'].get('profile_digest') != self.spec.fingerprint():
            await self.store.reject_unlaunched(binding,'PROFILE_CHANGED')
            return await self.store.get(a['run_id'])
        try:
            await authorize_work(self.store, run, a, 'execute', verifier=self.authorization_verifier)
        except RuntimeErrorCode as exc:
            await self.store.reject_unlaunched(binding, exc.code)
            return await self.store.get(a['run_id'])
        handle = a['id']
        spec = replace(self.spec, environment=dict(self.spec.environment,
            DIOS_RUN_ID=a['run_id'], DIOS_ATTEMPT_ID=a['id'],
            DIOS_WORK_INPUT=encoded(run['input'].get('payload',{})).decode('utf-8')))
        bridge = JsonlBridge()
        sequence, phase, output_count = 0, 'claimed', 0
        last_receipt, code = None, None
        async def report(next_phase, *, output=False):
            nonlocal sequence, phase, last_receipt
            sequence += 1
            last_receipt = await enqueue_report(self.store, self.spool, binding, sequence, next_phase,
                session_id=bridge.session_id, output=output,
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
            if await asyncio.to_thread(self.spool.contains, last_receipt):
                raise RuntimeErrorCode('REPORT_DELIVERY_PENDING')
            # Check current ownership/authorization after the asynchronous verifier.
            await self.store.heartbeat(*binding)
            observation = await self.adapter.start(handle,spec)
            await observe(observation)
            started, heartbeat_at = time.monotonic(), time.monotonic()
            code = None
            while True:
                # Database read failures cannot be interpreted as process death.
                current = await self.store.get(a['run_id'])
                cancelled = current['cancel_requested']
                revoked = current['revoked']
                if cancelled or revoked or time.monotonic()-started >= self.timeout:
                    observation = await self.adapter.stop(handle)
                    await observe(observation)
                    code = 'AUTH_REVOKED' if revoked else 'PROCESS_DEADLINE_EXCEEDED'
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
                        if bridge.session_id and phase == 'creating_session': await report('waiting_model')
                        if bridge.output_count > output_count:
                            await report('streaming',output=True)
                            output_count = bridge.output_count
                    except RuntimeErrorCode as exc:
                        code = exc.code
                if code and observation.alive:
                    observation = await self.adapter.stop(handle)
                    await observe(observation)
                    logs = await self.adapter.logs(handle,self.output_limit)
                if not observation.alive:
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
                        code = code or ('PROCESS_EXITED' if observation.exit_code != 0 else None)
                        if not code:
                            try:
                                bridge.feed(logs.stdout,final=True)
                            except RuntimeErrorCode as exc: code = exc.code
                        if code:
                            await self.store.finish_failed(binding,code)
                        else:
                            if phase == 'creating_session': await report('waiting_model')
                            await report('validating')
                            if await asyncio.to_thread(self.spool.contains, last_receipt):
                                raise RuntimeErrorCode('REPORT_DELIVERY_PENDING')
                            await authorize_work(self.store, await self.store.get(a['run_id']), a,
                                'accept_result', session_id=bridge.session_id, verifier=self.authorization_verifier)
                            blob = await asyncio.to_thread(self.artifacts.put,[encoded(bridge.result)])
                            await self.store.complete(*binding,self.artifacts,blob.key,JsonlBridge.validate)
                    # Explicit cleanup follows durable terminal state and raw-output retention.
                    await self.adapter.remove(handle)
                    return await self.store.get(a['run_id'])
                await relay_reports(self.store, self.spool)
                if time.monotonic()-heartbeat_at >= 5:
                    await self.store.heartbeat(*binding)
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            # Abrupt Worker loss leaves a discoverable workload; restart must reconcile it.
            raise
        except Exception as exc:
            # Best effort local evidence, independent of controller availability.
            # A live workload stays discoverable; a read error never proves its death.
            try:
                observed = await self.adapter.observe(handle)
                if not observed.alive and observed.state in {'exited','dead'}:
                    saved_logs = await self.adapter.logs(handle,self.output_limit)
                    await asyncio.to_thread(self.receipts.save,a,observed,saved_logs,failure=code)
            except Exception:
                pass
            code = exc.code if isinstance(exc,RuntimeErrorCode) else 'RUNTIME_OBSERVATION_UNAVAILABLE'
            try:
                await self.store.mark_unknown(binding,code)
            except Exception:
                # Storage unavailable: lease expiration supplies the durable uncertainty state.
                pass
            raise

    async def recover(self, *, limit=100):
        """Bounded maintenance pass, including while the scope is draining."""
        if type(limit) is not int or not 1 <= limit <= 100: raise RuntimeErrorCode('INVALID_LIMIT')
        await relay_reports(self.store, self.spool)
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
            except (RuntimeErrorCode, OperationalError, InterfaceError): pass
            for sig in (signal.SIGTERM,signal.SIGINT):
                try: loop.remove_signal_handler(sig)
                except (NotImplementedError,RuntimeError): pass
        return {'worker_id':self.worker_id,'state':'drained'}
