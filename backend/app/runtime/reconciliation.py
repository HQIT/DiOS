"""Recover original attempts from trusted lifecycle evidence and immutable bytes.

No launch, adoption, lease renewal or automatic requeue is performed here.
Local receipts are executor evidence; remote providers must authenticate their source.
"""
import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .authorization import authorize_work
from .bridge import JsonlBridge
from .database import RuntimeErrorCode
from .store import TERMINAL, encoded


class ExitReceipts:
    def __init__(self, provider, scope):
        self.provider, self.scope = provider, scope
        self.root = provider.root / ('.exits-' + hashlib.sha256(scope.encode()).hexdigest())
        self.root.mkdir(mode=0o700, exist_ok=True)

    def path(self, attempt_id):
        return self.root / hashlib.sha256(attempt_id.encode()).hexdigest()

    def save(self, attempt, observation, logs, *, failure=None):
        if observation.alive or observation.state not in {'exited', 'dead'} or not observation.process_instance:
            return None
        value = dict(scope=self.scope, run_id=attempt['run_id'], attempt_id=attempt['id'],
            worker_id=attempt['worker_id'], worker_instance=attempt['worker_instance'],
            epoch=attempt['epoch'], authorization_epoch=attempt['authorization_epoch'],
            observation=asdict(observation), failure=failure,
            truncated=logs.truncated, stdout=self.provider.put([logs.stdout]).key,
            stderr=self.provider.put([logs.stderr]).key)
        target = self.path(attempt['id'])
        if target.exists():
            old = self.load(attempt)
            if old['observation']['process_instance'] != observation.process_instance:
                raise RuntimeErrorCode('PROCESS_CHANGED')
            if any(old[k] != value[k] for k in ('stdout', 'stderr', 'observation', 'truncated')):
                raise RuntimeErrorCode('EXIT_RECEIPT_CONFLICT')
            # A later observation must not erase an earlier deadline/validation failure.
            value['failure'] = old['failure'] or failure
        fd, temp = tempfile.mkstemp(prefix='.receipt-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temp, target)
            if os.name != 'nt':
                directory = os.open(self.root, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
        finally:
            Path(temp).unlink(missing_ok=True)
        return value

    def load(self, attempt):
        path = self.path(attempt['id'])
        if not path.exists(): return None
        if path.stat().st_size > 16384: raise RuntimeErrorCode('INVALID_EXIT_RECEIPT')
        value = json.loads(path.read_bytes())
        expected = dict(scope=self.scope, run_id=attempt['run_id'], attempt_id=attempt['id'],
            worker_id=attempt['worker_id'], worker_instance=attempt['worker_instance'],
            epoch=attempt['epoch'], authorization_epoch=attempt['authorization_epoch'])
        if any(value.get(k) != v for k, v in expected.items()):
            raise RuntimeErrorCode('EXIT_RECEIPT_BINDING_MISMATCH')
        for kind in ('stdout', 'stderr'): self.provider.verify(value[kind])
        return value


async def reconcile_run(store, adapter, provider, run_id, *, output_limit=1024*1024, verifier=None):
    # Expiration uses server time. Query timeout alone never expires ownership.
    await store.expire()
    run = await store.get(run_id)
    if run['state'] in TERMINAL: return run
    if run['state'] != 'reconciliation_required': return run
    a = next(a for a in run['attempts'] if a['id'] == run['attempt_id'])
    launch = a['remote'].get('launch', {})
    if launch.get('adapter') != adapter.name or launch.get('handle') != a['id']:
        raise RuntimeErrorCode('RUNTIME_BINDING_NOT_FOUND')
    observation = await adapter.observe(a['id'])
    if a['process_instance'] and observation.process_instance and a['process_instance'] != observation.process_instance:
        raise RuntimeErrorCode('PROCESS_CHANGED')
    if observation.alive: return run
    receipts = ExitReceipts(provider, store.scope)
    receipt = await asyncio.to_thread(receipts.load, a)
    if observation.state in {'exited', 'dead'}:
        logs = await adapter.logs(a['id'], output_limit)
        receipt = await asyncio.to_thread(receipts.save, a, observation, logs,
            failure=receipt['failure'] if receipt else None)
    elif observation.state != 'absent':
        return run
    if receipt is None: return run
    obs = receipt['observation']
    if obs['alive'] or obs['state'] not in {'exited', 'dead'} or not obs['process_instance']:
        raise RuntimeErrorCode('PROCESS_EXIT_NOT_CONFIRMED')
    if a['process_instance'] and a['process_instance'] != obs['process_instance']:
        raise RuntimeErrorCode('PROCESS_CHANGED')
    if obs['handle'] != a['id']: raise RuntimeErrorCode('RUNTIME_BINDING_NOT_FOUND')
    bridge, result_key = JsonlBridge(), None
    failure = receipt['failure'] or ('OUTPUT_LIMIT_EXCEEDED' if receipt['truncated'] else None)
    failure = failure or ('PROCESS_EXITED' if obs['exit_code'] != 0 else None)
    try:
        data = await asyncio.to_thread(lambda: b''.join(provider.read(receipt['stdout'])))
        if len(data) > output_limit: raise RuntimeErrorCode('OUTPUT_LIMIT_EXCEEDED')
        bridge.feed(data, final=True)
    except RuntimeErrorCode as exc:
        failure = failure or exc.code
    if a['session_id'] and bridge.session_id != a['session_id']:
        raise RuntimeErrorCode('SESSION_CHANGED')
    if not failure and not run['cancel_requested'] and not run['revoked']:
        await authorize_work(store, run, a, 'accept_result', session_id=bridge.session_id, verifier=verifier)
        result_key = (await asyncio.to_thread(provider.put, [encoded(bridge.result)])).key
    try:
        await store.resolve_exit(run, a, receipt, provider, session_id=bridge.session_id,
            result_key=result_key, failure=failure)
    except RuntimeErrorCode as exc:
        if exc.code != 'VERSION_CONFLICT': raise
        current = await store.get(run_id)
        if current['state'] not in TERMINAL or current['attempt_id'] != a['id']: raise
    return await store.get(run_id)
