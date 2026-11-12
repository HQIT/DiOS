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
                 artifacts: LocalArtifacts, *, poll_interval=.25, timeout=300, output_limit=1024*1024):
        if not 0 < poll_interval <= 5 or not 0 < timeout <= 86400 or not 1 <= output_limit <= 4*1024*1024:
            raise RuntimeErrorCode('INVALID_WORKER_LIMIT')
        spec.validate()
        if spec.kind != 'task': raise RuntimeErrorCode('WORKER_REQUIRES_TASK_PROFILE')
        self.store, self.adapter, self.profile_id, self.spec = store, adapter, profile_id, spec
        self.artifacts = artifacts
        self.poll_interval, self.timeout, self.output_limit = poll_interval, timeout, output_limit
        self.worker_id, self.instance = str(uuid4()), str(uuid4())
        self.boot_id = boot_identity()
        self.stopping = False

    async def once(self):
        await self.store.register_worker(self.worker_id, self.instance, socket.gethostname(), os.getpid(), slots=1)
        a = await self.store.claim(self.worker_id, self.instance, profile_id=self.profile_id)
        if a is None: return {'worker_id':self.worker_id,'state':'idle'}
        binding = (a['run_id'], a['id'], self.worker_id, self.instance, a['epoch'])
        run = await self.store.get(a['run_id'])
        if run['input'].get('profile_digest') != self.spec.fingerprint():
            await self.store.reject_unlaunched(binding,'PROFILE_CHANGED')
            return await self.store.get(a['run_id'])
        handle = a['id']
        spec = replace(self.spec, environment=dict(self.spec.environment,
            DIOS_RUN_ID=a['run_id'], DIOS_ATTEMPT_ID=a['id'],
            DIOS_WORK_INPUT=encoded(run['input'].get('payload',{})).decode('utf-8')))
        bridge = JsonlBridge()
        sequence, phase, output_count = 0, 'claimed', 0
        async def report(next_phase, *, output=False):
            nonlocal sequence, phase
            sequence += 1
            await self.store.report(*binding,sequence,next_phase,session_id=bridge.session_id,output=output)
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
            await self.store.report(*binding,sequence,'preparing',remote={
                'launch':{'handle':handle,'adapter':self.adapter.name,'profile_id':self.profile_id,
                          'profile_digest':self.spec.fingerprint(),'worker_boot_id':self.boot_id}})
            phase = 'preparing'
            await report('creating_session')
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
                            blob = await asyncio.to_thread(self.artifacts.put,[encoded(bridge.result)])
                            await self.store.complete(*binding,self.artifacts,blob.key,JsonlBridge.validate)
                    # Explicit cleanup follows durable terminal state and raw-output retention.
                    await self.adapter.remove(handle)
                    return await self.store.get(a['run_id'])
                if time.monotonic()-heartbeat_at >= 5:
                    await self.store.heartbeat(*binding)
                    heartbeat_at = time.monotonic()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            # Abrupt Worker loss leaves a discoverable workload; restart must reconcile it.
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc,RuntimeErrorCode) else 'RUNTIME_OBSERVATION_UNAVAILABLE'
            try:
                await self.store.mark_unknown(binding,code)
            except Exception:
                # Storage unavailable: lease expiration supplies the durable uncertainty state.
                pass
            raise

    async def serve(self, *, once=False):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):
            try: loop.add_signal_handler(sig,lambda:setattr(self,'stopping',True))
            except (NotImplementedError,RuntimeError): pass
        try:
            while not self.stopping:
                result = await self.once()
                if once: return result
                if result.get('state')=='idle': await asyncio.sleep(self.poll_interval)
        finally:
            await self.store.drain(self.worker_id)
            for sig in (signal.SIGTERM,signal.SIGINT):
                try: loop.remove_signal_handler(sig)
                except (NotImplementedError,RuntimeError): pass
        return {'worker_id':self.worker_id,'state':'drained'}
