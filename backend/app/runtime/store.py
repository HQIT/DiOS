"""Transactional runtime repository. Trusted server-side calls, not an auth API."""
import hashlib
import json
from uuid import uuid4
from sqlalchemy import select, and_, exists
from .database import RuntimeErrorCode
from .schema import runs, workers, attempts, events, acks, projections, artifacts, imports

TERMINAL = {'succeeded', 'failed', 'cancelled'}
ACTIVE_ATTEMPTS = {'claimed', 'preparing', 'creating_session', 'waiting_model', 'streaming', 'validating'}
PHASES = {
    'claimed': {'preparing'}, 'preparing': {'creating_session'},
    'creating_session': {'waiting_model'}, 'waiting_model': {'streaming','validating'},
    'streaming': {'streaming','validating'}, 'validating': set(),
}

def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()

def bounded(value, limit=16384):
    if len(encoded(value)) > limit:
        raise RuntimeErrorCode('METADATA_TOO_LARGE')

def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise RuntimeErrorCode('INVALID_IDENTIFIER')

class RuntimeStore:
    def __init__(self, database, scope):
        identifier(scope)
        self.db, self.scope = database, scope

    async def _run(self, c, run_id):
        row = (await c.execute(select(runs).where(runs.c.id == run_id, runs.c.scope == self.scope))).mappings().first()
        if row is None:
            raise RuntimeErrorCode('NOT_FOUND')
        return dict(row)

    async def _event(self, c, run, kind, detail, now, **changes):
        version = run['version'] + 1
        state = changes.get('state', run['state'])
        payload = {'run_id': run['id'], 'version': version, 'state': state, 'detail': detail}
        bounded(payload)
        await c.execute(runs.update().where(runs.c.id == run['id']).values(version=version, updated_at=now, **changes))
        await c.execute(events.insert().values(id=str(uuid4()), run_id=run['id'], version=version,
            type=kind, payload=payload, digest=digest(payload), created_at=now))

    async def submit(self, key, value):
        identifier(key)
        if not isinstance(value, dict): raise RuntimeErrorCode("INVALID_ARGUMENT")
        bounded(value)
        hashed = digest(value)
        async with self.db.transaction(self.scope) as c:
            existing = (await c.execute(select(runs).where(runs.c.scope == self.scope, runs.c.idempotency_key == key))).mappings().first()
            if existing:
                if existing['input_digest'] != hashed:
                    raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                return dict(existing)
            now = await self.db.now(c)
            record = dict(id=str(uuid4()), scope=self.scope, idempotency_key=key, input_digest=hashed,
                input=value, state='queued', version=0, epoch=0, attempt_id=None, authorization_epoch=1,
                revoked=False, cancel_requested=False, created_at=now, updated_at=now)
            await c.execute(runs.insert().values(**record))
            await self._event(c, record, 'run.accepted', {}, now)
            return await self._run(c, record['id'])

    async def register_worker(self, worker_id, instance, host, pid, slots=1):
        for value in (worker_id, instance, host): identifier(value)
        if len(worker_id)>64 or len(instance)>64 or not 1 <= slots <= 128 or pid <= 0:
            raise RuntimeErrorCode('INVALID_WORKER')
        async with self.db.transaction(self.scope) as c:
            now = await self.db.now(c)
            old = (await c.execute(select(workers).where(workers.c.id == worker_id))).mappings().first()
            if old:
                if old['scope'] != self.scope or old['instance'] != instance:
                    raise RuntimeErrorCode('WORKER_ID_IN_USE')
                # Registration must not reset a persisted drain request.
                await c.execute(workers.update().where(workers.c.id == worker_id).values(heartbeat_at=now))
            else:
                await c.execute(workers.insert().values(id=worker_id, instance=instance, scope=self.scope,
                    host=host, pid=pid, slots=slots, draining=False, heartbeat_at=now))

    async def drain(self, worker_id):
        async with self.db.transaction(self.scope) as c:
            result = await c.execute(workers.update().where(workers.c.id == worker_id, workers.c.scope == self.scope).values(draining=True))
            if result.rowcount != 1: raise RuntimeErrorCode('NOT_FOUND')

    async def claim(self, worker_id, instance, lease_seconds=60):
        if not 1 <= lease_seconds <= 60: raise RuntimeErrorCode('INVALID_LEASE')
        async with self.db.transaction(self.scope) as c:
            now = await self.db.now(c)
            w = (await c.execute(select(workers).where(workers.c.id == worker_id, workers.c.scope == self.scope))).mappings().first()
            if not w or w['instance'] != instance: raise RuntimeErrorCode('WORKER_MISMATCH')
            if w['draining']: return None
            # Expired, unreconciled attempts still occupy a slot. Their processes may be alive.
            busy = (await c.execute(select(attempts.c.id).where(attempts.c.worker_id == worker_id,
                attempts.c.state.in_(ACTIVE_ATTEMPTS | {'result_unknown'})))).all()
            if len(busy) >= w['slots']: return None
            row = (await c.execute(select(runs).where(runs.c.scope == self.scope, runs.c.state == 'queued',
                runs.c.revoked == False, runs.c.cancel_requested == False).order_by(runs.c.created_at, runs.c.id).limit(1))).mappings().first()
            if not row: return None
            run = dict(row)
            aid, epoch = str(uuid4()), run['epoch'] + 1
            record = dict(id=aid, run_id=run['id'], worker_id=worker_id, worker_instance=instance,
                epoch=epoch, authorization_epoch=run['authorization_epoch'], lease_until=now+lease_seconds,
                state='claimed', session_id=None, process_id=None, process_instance=None, last_sequence=0,
                report_digest=None, remote={}, error=None, last_output_at=None, started_at=now, finished_at=None)
            await c.execute(attempts.insert().values(**record))
            await c.execute(workers.update().where(workers.c.id == worker_id).values(heartbeat_at=now))
            await self._event(c, run, 'attempt.claimed', {'attempt_id': aid, 'epoch': epoch}, now,
                state='executing', epoch=epoch, attempt_id=aid)
            return record

    async def _owned(self, c, run_id, attempt_id, worker_id, instance, epoch, *, authorize=False):
        run = await self._run(c, run_id)
        a = (await c.execute(select(attempts).where(attempts.c.id == attempt_id, attempts.c.run_id == run_id))).mappings().first()
        now = await self.db.now(c)
        if not a or (run['attempt_id'], run['epoch']) != (attempt_id, epoch) or (a['worker_id'], a['worker_instance']) != (worker_id, instance) or a['lease_until'] <= now or a['state'] not in ACTIVE_ATTEMPTS:
            raise RuntimeErrorCode('STALE_OWNER')
        if authorize and (run['revoked'] or run['cancel_requested'] or a['authorization_epoch'] != run['authorization_epoch']):
            raise RuntimeErrorCode('AUTH_REVOKED')
        return run, dict(a), now

    async def heartbeat(self, run_id, attempt_id, worker_id, instance, epoch):
        async with self.db.transaction(self.scope) as c:
            _, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            await c.execute(attempts.update().where(attempts.c.id == a['id']).values(lease_until=now+60))
            await c.execute(workers.update().where(workers.c.id == worker_id).values(heartbeat_at=now))
            return now+60

    async def report(self, run_id, attempt_id, worker_id, instance, epoch, sequence, phase,
                     *, session_id=None, process_id=None, process_instance=None, remote=None, output=False):
        if not isinstance(sequence, int) or sequence < 1 or phase not in ACTIVE_ATTEMPTS:
            raise RuntimeErrorCode('INVALID_REPORT')
        report = dict(sequence=sequence, phase=phase, session_id=session_id, process_id=process_id,
                      process_instance=process_instance, remote=remote, output=output)
        bounded(report)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            if sequence < a['last_sequence']: return False
            if sequence == a['last_sequence']:
                if digest(report) != a['report_digest']: raise RuntimeErrorCode('REPORT_CONFLICT')
                return False
            if phase != a['state'] and phase not in PHASES[a['state']]: raise RuntimeErrorCode('INVALID_TRANSITION')
            changes = dict(state=phase, last_sequence=sequence, report_digest=digest(report))
            if session_id:
                identifier(session_id)
                if a['session_id'] and a['session_id'] != session_id: raise RuntimeErrorCode('SESSION_CHANGED')
                changes['session_id'] = session_id
            if phase in {'waiting_model','streaming','validating'} and not (session_id or a['session_id']):
                raise RuntimeErrorCode('SESSION_REQUIRED')
            if process_id is not None:
                if process_id <= 0 or not process_instance: raise RuntimeErrorCode('PROCESS_EVIDENCE_REQUIRED')
                if a['process_instance'] and (a['process_instance'],a['process_id']) != (process_instance,process_id):
                    raise RuntimeErrorCode('PROCESS_CHANGED')
                changes.update(process_id=process_id, process_instance=process_instance)
            if remote is not None: changes['remote'] = remote
            if output: changes['last_output_at'] = now
            await c.execute(attempts.update().where(attempts.c.id == attempt_id).values(**changes))
            await self._event(c, run, 'attempt.phase', {'attempt_id': attempt_id, 'phase': phase, 'sequence': sequence}, now,
                state='validating' if phase == 'validating' else 'executing')
            return True

    async def revoke(self, run_id, expected_version):
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            await self._event(c, run, 'authorization.revoked', {}, await self.db.now(c), revoked=True,
                authorization_epoch=run['authorization_epoch']+1)

    async def expire(self):
        """Loss of observation fences the owner and requires reconciliation, never requeue."""
        async with self.db.transaction(self.scope) as c:
            now = await self.db.now(c)
            rows = (await c.execute(select(attempts).join(runs, runs.c.id == attempts.c.run_id).where(
                runs.c.scope == self.scope, attempts.c.state.in_(ACTIVE_ATTEMPTS), attempts.c.lease_until <= now))).mappings().all()
            for a in rows:
                run = await self._run(c, a['run_id'])
                await c.execute(attempts.update().where(attempts.c.id == a['id']).values(state='result_unknown', error={'code':'LEASE_EXPIRED'}))
                await self._event(c, run, 'attempt.observation_expired', {'attempt_id':a['id']}, now,
                    state='reconciliation_required', epoch=run['epoch']+1)
            return len(rows)

    async def reconcile_absent(self, run_id, expected_version, evidence_ref):
        """Trusted reconciler supplies evidence of process exit and absence of external effects."""
        identifier(evidence_ref)
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            if run['state'] != 'reconciliation_required': raise RuntimeErrorCode('INVALID_TRANSITION')
            now = await self.db.now(c)
            await c.execute(attempts.update().where(attempts.c.id == run['attempt_id']).values(state='abandoned', finished_at=now))
            await self._event(c, run, 'attempt.reconciled_absent', {'evidence_ref': evidence_ref}, now, state='retry_wait')

    async def retry(self, run_id, expected_version):
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            if run['state'] != 'retry_wait' or run['revoked'] or run['cancel_requested']:
                raise RuntimeErrorCode('RETRY_NOT_ALLOWED')
            await self._event(c, run, 'run.retry_queued', {}, await self.db.now(c), state='queued')

    async def complete(self, run_id, attempt_id, worker_id, instance, epoch, provider, key, validator):
        # Validation and object I/O stay outside the short database transaction.
        blob = provider.verify(key)
        observed = hashlib.sha256()
        count = 0
        def verified_chunks():
            nonlocal count
            for chunk in provider.read(key):
                observed.update(chunk); count += len(chunk)
                yield chunk
        stream = verified_chunks()
        valid = validator(stream)
        for _ in stream: pass
        if observed.hexdigest() != blob.digest or count != blob.size:
            raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
        if valid is not True:
            raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            if a['state'] != 'validating': raise RuntimeErrorCode('INVALID_TRANSITION')
            aid = str(uuid4())
            await c.execute(artifacts.insert().values(id=aid, run_id=run_id, attempt_id=attempt_id,
                digest=blob.digest, size=blob.size, provider=provider.name, storage_key=blob.key, created_at=now))
            await c.execute(attempts.update().where(attempts.c.id == attempt_id).values(state='completed', finished_at=now))
            await self._event(c, run, 'run.completed', {'artifact_id':aid}, now, state='succeeded')
            return aid

    async def get(self, run_id):
        # Keep run and attempt snapshots coherent under the same scope transaction.
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            run['attempts'] = [dict(a) for a in (await c.execute(select(attempts).where(attempts.c.run_id == run_id).order_by(attempts.c.epoch))).mappings()]
            run['observed_at'] = await self.db.now(c)
            return run

    async def pending_events(self, consumer, limit=100):
        identifier(consumer)
        if not 1 <= limit <= 1000: raise RuntimeErrorCode('INVALID_LIMIT')
        async with self.db.engine.connect() as c:
            await self.db.check_schema(c)
            query = select(events).join(runs, runs.c.id == events.c.run_id).where(runs.c.scope == self.scope,
                ~exists(select(acks.c.event_id).where(acks.c.event_id == events.c.id, acks.c.consumer == consumer)))
            return [dict(e) for e in (await c.execute(query.order_by(events.c.created_at, events.c.id).limit(limit))).mappings()]

    async def acknowledge(self, consumer, event_id, expected_digest):
        async with self.db.transaction(self.scope) as c:
            event = (await c.execute(select(events).join(runs, runs.c.id == events.c.run_id).where(events.c.id == event_id, runs.c.scope == self.scope))).mappings().first()
            if not event or event['digest'] != expected_digest: raise RuntimeErrorCode('EVENT_MISMATCH')
            old = (await c.execute(select(acks).where(acks.c.consumer == consumer, acks.c.event_id == event_id))).first()
            if not old: await c.execute(acks.insert().values(consumer=consumer,event_id=event_id))

    async def apply_projection(self, consumer, event):
        """Receiver: monotonic aggregate versions, not a global transaction sequence cursor."""
        bounded(event['payload'])
        if digest(event['payload']) != event['digest'] or event['payload']['run_id'] != event['run_id'] or event['payload']['version'] != event['version']:
            raise RuntimeErrorCode('EVENT_MISMATCH')
        async with self.db.transaction(self.scope) as c:
            await self._run(c, event['run_id'])
            old = (await c.execute(select(projections).where(projections.c.consumer == consumer, projections.c.run_id == event['run_id']))).mappings().first()
            if old and event['version'] < old['version']: return False
            if old and event['version'] == old['version']:
                if event['digest'] != old['digest']: raise RuntimeErrorCode('EVENT_CONFLICT')
                return False
            value = dict(consumer=consumer, run_id=event['run_id'], version=event['version'], digest=event['digest'], payload=event['payload'], observed_at=await self.db.now(c))
            if old: await c.execute(projections.update().where(projections.c.consumer == consumer, projections.c.run_id == event['run_id']).values(**value))
            else: await c.execute(projections.insert().values(**value))
            return True

    async def import_history(self, source, kind, source_id, record):
        """Preserve supplied source records; never create runnable tasks or call a model."""
        for value in (source,kind,source_id): identifier(value)
        bounded(record, 65536)
        hashed = digest(record)
        async with self.db.transaction(self.scope) as c:
            where = and_(imports.c.scope==self.scope, imports.c.source==source, imports.c.kind==kind, imports.c.source_id==source_id)
            old = (await c.execute(select(imports).where(where))).mappings().first()
            if old:
                if old['digest'] != hashed: raise RuntimeErrorCode('IMPORT_CONFLICT')
                return dict(old)
            value = dict(scope=self.scope,source=source,kind=kind,source_id=source_id,target_id=str(uuid4()),digest=hashed,
                         record=record, acceptance='legacy_unverified')
            await c.execute(imports.insert().values(**value))
            return value


    async def cancel(self, run_id, expected_version):
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            if run['state'] in TERMINAL: return run
            # Unclaimed work is safe to cancel immediately; running work needs confirmation.
            state = 'cancelled' if run['state'] == 'queued' else run['state']
            await self._event(c, run, 'run.cancel_requested', {}, await self.db.now(c), state=state, cancel_requested=True)
            return await self._run(c, run_id)

    async def confirm_cancel(self, run_id, attempt_id, worker_id, instance, epoch, evidence_ref):
        identifier(evidence_ref)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch)
            if not run['cancel_requested']: raise RuntimeErrorCode('CANCEL_NOT_REQUESTED')
            await c.execute(attempts.update().where(attempts.c.id==attempt_id).values(state='cancelled',finished_at=now))
            await self._event(c, run, 'run.cancel_confirmed', {'evidence_ref':evidence_ref}, now, state='cancelled')
