"""Transactional runtime repository. Trusted server-side calls, not an auth API."""
import asyncio
import hashlib
import json
import math
from uuid import uuid4

from sqlalchemy import and_, exists, select

from .database import RuntimeErrorCode
from .lifecycle import admission
from .schema import (
    acks,
    artifacts,
    attempts,
    events,
    imports,
    projections,
    runs,
    workers,
)

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
        if state in TERMINAL and run['attempt_id']:
            from .faults import outcome
            await outcome(c,self.scope,run['attempt_id'],state=='succeeded',now)
            from .fault_schema import plans
            await c.execute(plans.update().where(plans.c.run_id==run['id']).values(state=state,retry_at=None,updated_at=now))
        if state in TERMINAL and run['input'].get('work_authorization_ref'):
            from .work_authorizations import close
            await close(c,run)
        if state in TERMINAL:
            from .tool_invocations import close_unstarted
            await close_unstarted(c, self.scope, run['id'], state, now)
        if (state in TERMINAL or changes.get('cancel_requested') or changes.get('revoked')
                or changes.get('epoch',run['epoch']) != run['epoch']):
            from .child_authorizations import cancel_children
            await cancel_children(c,self,run['id'],now)
        payload = {'run_id': run['id'], 'version': version, 'state': state, 'detail': detail}
        bounded(payload)
        await c.execute(runs.update().where(runs.c.id == run['id']).values(version=version, updated_at=now, **changes))
        event_id=str(uuid4())
        await c.execute(events.insert().values(id=event_id, run_id=run['id'], version=version,
            type=kind, payload=payload, digest=digest(payload), created_at=now))
        if state in TERMINAL and run['state'] not in TERMINAL:
            from .outcome_schema import outcomes, require
            await require(c)
            await c.execute(outcomes.insert().values(scope=self.scope,run_id=run['id'],state=state,
                finished_at=now,event_id=event_id,event_version=version,event_digest=digest(payload)))

    async def submission_receipt(self, key, submission_digest):
        """Read an immutable accepted request without creating or renewing Work."""
        identifier(key)
        async with self.db.read(self.scope) as c:
            existing = (await c.execute(select(runs).where(
                runs.c.scope == self.scope, runs.c.idempotency_key == key))).mappings().first()
            if existing is None:return None
            # Legacy records retain exact input/authority matching in submit().
            if 'submission_digest' not in existing['input']:return None
            if existing['input'].get('submission_digest') != submission_digest:
                raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
            return dict(existing)

    async def submit(self, key, value, *, authorization=None, submission_digest=None, admission_guard=None, parent_binding=None, revision_ref=None):
        identifier(key)
        if not isinstance(value, dict): raise RuntimeErrorCode("INVALID_ARGUMENT")
        if {'revision_ref','revision_digest'}.intersection(value):raise RuntimeErrorCode('REVISION_BINDING_SERVER_OWNED')
        if revision_ref is not None:identifier(revision_ref)
        if 'parent_execution' in value:raise RuntimeErrorCode('CHILD_BINDING_INVALID')
        if parent_binding is not None:
            from .child_authorizations import validate_binding
            validate_binding(parent_binding)
            value=dict(value,parent_execution=parent_binding)
        if submission_digest is not None:
            if (not isinstance(submission_digest,str) or len(submission_digest)!=64
                    or any(x not in '0123456789abcdef' for x in submission_digest)):
                raise RuntimeErrorCode('INVALID_ARGUMENT')
            value=dict(value,submission_digest=submission_digest)
        if authorization is not None:
            bounded(authorization)
            if authorization.get('delegation_ref'):
                value=dict(value,delegation_ref=authorization['delegation_ref'])
            elif 'delegation_ref' in value:raise RuntimeErrorCode('DELEGATION_BINDING_MISMATCH')
            value=dict(value,work_authorization_ref='wa-'+digest([self.scope,key,authorization['policy_digest']])[:60],
                work_policy_digest=authorization['policy_digest'])
        elif 'delegation_ref' in value:raise RuntimeErrorCode('WORK_AUTHORIZATION_REQUIRED')
        bounded(value)
        async with self.db.transaction(self.scope) as c:
            if revision_ref is not None:
                from .execution_revisions import bind_input
                value=await bind_input(c,self.scope,revision_ref,value,authorized=authorization is not None)
                bounded(value)
            hashed = digest(value)
            existing = (await c.execute(select(runs).where(runs.c.scope == self.scope, runs.c.idempotency_key == key))).mappings().first()
            if existing:
                same_receipt = (submission_digest is not None
                    and existing['input'].get('submission_digest') == submission_digest
                    and existing['input'].get('revision_ref') == value.get('revision_ref'))
                legacy_exact = ('submission_digest' not in existing['input']
                    and existing['input_digest'] == digest({k:v for k,v in value.items() if k!='submission_digest'}))
                if existing['input_digest'] != hashed and not same_receipt and not legacy_exact:
                    raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                return dict(existing)
            if not await admission(c, self.scope): raise RuntimeErrorCode('RUNTIME_DRAINING')
            if admission_guard is not None:
                await admission_guard(c)
            now = await self.db.now(c)
            record = dict(id=str(uuid4()), scope=self.scope, idempotency_key=key, input_digest=hashed,
                input=value, state='queued', version=0, epoch=0, attempt_id=None, authorization_epoch=1,
                revoked=False, cancel_requested=False, created_at=now, updated_at=now)
            await c.execute(runs.insert().values(**record))
            if parent_binding is not None:
                from .child_authorizations import bind
                authorization=await bind(c,self,record,authorization,parent_binding)
            if authorization is not None:
                from .work_authorizations import create
                await create(c,self.scope,record,authorization,now)
            if parent_binding is not None:
                from .child_authorizations import validate_parent
                await validate_parent(c,record,await self.db.now(c))
                if admission_guard is not None:await admission_guard(c)
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

    async def claim(self, worker_id, instance, lease_seconds=60, *, profile_id=None, recovery_policy=None, profile_digest=None, revision_ref=None):
        if not 1 <= lease_seconds <= 60: raise RuntimeErrorCode('INVALID_LEASE')
        if profile_digest is not None and (not isinstance(profile_digest,str) or len(profile_digest)!=64 or any(x not in '0123456789abcdef' for x in profile_digest)):
            raise RuntimeErrorCode('INVALID_PROFILE_DIGEST')
        if revision_ref is not None:
            identifier(revision_ref)
            if profile_digest is None:raise RuntimeErrorCode('REVISION_PROFILE_DIGEST_REQUIRED')
        async with self.db.transaction(self.scope) as c:
            if not await admission(c, self.scope): return None
            now = await self.db.now(c)
            w = (await c.execute(select(workers).where(workers.c.id == worker_id, workers.c.scope == self.scope))).mappings().first()
            if not w or w['instance'] != instance: raise RuntimeErrorCode('WORKER_MISMATCH')
            if w['draining']: return None
            # Expired, unreconciled attempts still occupy a slot. Their processes may be alive.
            busy = (await c.execute(select(attempts.c.id).where(attempts.c.worker_id == worker_id,
                attempts.c.state.in_(ACTIVE_ATTEMPTS | {'result_unknown'})))).all()
            if len(busy) >= w['slots']: return None
            from .faults import claimed, policy, ready
            from .execution_budgets import expire_waiting, admit_query as budget_query, bind as bind_budget
            await expire_waiting(c,self,now)
            recovery_policy=policy(recovery_policy)
            if not await ready(c,self,recovery_policy,profile_id,now):return None
            query = select(runs).where(runs.c.scope == self.scope, runs.c.state == 'queued',
                runs.c.revoked == False, runs.c.cancel_requested == False)
            if profile_id is not None:
                identifier(profile_id)
                query = query.where(runs.c.input['runtime_profile'].as_string() == profile_id)
            if profile_digest is not None:
                query=query.where(runs.c.input['profile_digest'].as_string()==profile_digest)
            query=query.where(runs.c.input['revision_ref'].as_string()==revision_ref if revision_ref is not None else runs.c.input['revision_ref'].as_string().is_(None))
            from .capacity import admit_query
            query = await admit_query(c, self.scope, query)
            query = await budget_query(c, self.scope, query, now)
            row = (await c.execute(query.order_by(runs.c.created_at, runs.c.id).limit(1))).mappings().first()
            if not row: return None
            run = dict(row)
            deadline = await bind_budget(c,self.scope,run,now)
            if deadline is not None and deadline <= now:
                await self._event(c,run,'run.execution_budget_exhausted',{},now,state='failed')
                return None
            aid, epoch = str(uuid4()), run['epoch'] + 1
            record = dict(id=aid, run_id=run['id'], worker_id=worker_id, worker_instance=instance,
                epoch=epoch, authorization_epoch=run['authorization_epoch'], lease_until=now+lease_seconds,
                state='claimed', session_id=None, process_id=None, process_instance=None, last_sequence=0,
                report_digest=None, remote={}, error=None, last_output_at=None, started_at=now, finished_at=None)
            await c.execute(attempts.insert().values(**record))
            await claimed(c,self.scope,record,recovery_policy,now)
            await c.execute(workers.update().where(workers.c.id == worker_id).values(heartbeat_at=now))
            await self._event(c, run, 'attempt.claimed', {'attempt_id': aid, 'epoch': epoch}, now,
                state='executing', epoch=epoch, attempt_id=aid)
            return record

    async def _owned(self, c, run_id, attempt_id, worker_id, instance, epoch, *, authorize=False):
        await admission(c, self.scope, ownership=True)
        run = await self._run(c, run_id)
        a = (await c.execute(select(attempts).where(attempts.c.id == attempt_id, attempts.c.run_id == run_id))).mappings().first()
        now = await self.db.now(c)
        if not a or (run['attempt_id'], run['epoch']) != (attempt_id, epoch) or (a['worker_id'], a['worker_instance']) != (worker_id, instance) or a['lease_until'] <= now or a['state'] not in ACTIVE_ATTEMPTS:
            raise RuntimeErrorCode('STALE_OWNER')
        if authorize and (run['revoked'] or run['cancel_requested'] or a['authorization_epoch'] != run['authorization_epoch']):
            raise RuntimeErrorCode('AUTH_REVOKED')
        if authorize:
            from .work_authorizations import require_current
            await require_current(c,run,a,now)
            now=await self.db.now(c)
            if a['lease_until']<=now:raise RuntimeErrorCode('STALE_OWNER')
        return run, dict(a), now

    async def heartbeat(self, run_id, attempt_id, worker_id, instance, epoch):
        async with self.db.transaction(self.scope) as c:
            _, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            await c.execute(attempts.update().where(attempts.c.id == a['id']).values(lease_until=now+60))
            await c.execute(workers.update().where(workers.c.id == worker_id).values(heartbeat_at=now))
            return now+60

    async def report(self, run_id, attempt_id, worker_id, instance, epoch, sequence, phase,
                     *, session_id=None, process_id=None, process_instance=None, remote=None, output=False,
                     output_observed_at=None, authorization_epoch=None, output_excerpt=None, usage=None):
        if not isinstance(sequence, int) or sequence < 1 or phase not in ACTIVE_ATTEMPTS:
            raise RuntimeErrorCode('INVALID_REPORT')
        if output_observed_at is not None and (type(output_observed_at) not in (int, float)
                or not math.isfinite(output_observed_at) or output_observed_at < 0):
            raise RuntimeErrorCode('INVALID_REPORT')
        report = dict(sequence=sequence, phase=phase, session_id=session_id, process_id=process_id,
                      process_instance=process_instance, remote=remote, output=output)
        # Preserve digests for existing producers that do not supply an observation time.
        if output_observed_at is not None: report['output_observed_at'] = output_observed_at
        if authorization_epoch is not None: report['authorization_epoch'] = authorization_epoch
        if output_excerpt is not None:
            if not output or not isinstance(output_excerpt,str) or len(output_excerpt)>2000:
                raise RuntimeErrorCode('INVALID_REPORT')
            report['output_excerpt']=output_excerpt
        if usage is not None:
            if not isinstance(usage,list) or len(usage)>64:raise RuntimeErrorCode("INVALID_USAGE")
            from .usage import validate
            for value in usage:validate(value)
            report["usage"]=usage
        bounded(report)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            if authorization_epoch is not None and authorization_epoch != a['authorization_epoch']:
                raise RuntimeErrorCode('AUTH_REVOKED')
            if session_id and run['input'].get('work_authorization_ref'):
                from .work_authorizations import require_current
                await require_current(c,run,a,now,session_id=session_id)
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
            if output_excerpt is not None:
                changes['remote']=dict(changes.get('remote',a['remote']),progress={'text':output_excerpt})
            if output:
                observed = now if output_observed_at is None else min(output_observed_at, now)
                changes['last_output_at'] = max(a['last_output_at'] or 0, observed)
            if usage:
                from .usage import record
                await record(c,self.scope,run,dict(a,session_id=session_id or a['session_id']),usage,now,{'report_sequence':sequence})
            await c.execute(attempts.update().where(attempts.c.id == attempt_id).values(**changes))
            detail = {'attempt_id': attempt_id, 'phase': phase, 'sequence': sequence}
            if output_observed_at is not None: detail['output_observed_at'] = output_observed_at
            await self._event(c, run, 'attempt.phase', detail, now,
                state='validating' if phase == 'validating' else 'executing')
            return True

    async def revoke(self, run_id, expected_version, *, access_guard=None):
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if access_guard:await asyncio.to_thread(access_guard,run)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            now=await self.db.now(c)
            from .work_authorizations import revoke
            await revoke(c,run,now)
            await self._event(c, run, 'authorization.revoked', {}, now, revoked=True,
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
            from .tool_invocations import require_settled
            await require_settled(c,self.scope,run_id)
            from .fault_schema import plans
            if (await c.execute(select(plans.c.run_id).where(plans.c.run_id==run_id))).first():
                raise RuntimeErrorCode('RETRY_MANAGED_BY_POLICY')
            await self._event(c, run, 'run.retry_queued', {}, await self.db.now(c), state='queued')

    async def complete(self, run_id, attempt_id, worker_id, instance, epoch, provider, key, validator):
        # Validation and object I/O stay outside the short database transaction.
        async with self.db.read(self.scope) as c:
            snapshot=await self._run(c,run_id)
        declared=snapshot['input'].get('input_registration',{}).get('result_schema') is not None
        result_bytes=bytearray()
        blob = provider.verify(key)
        observed = hashlib.sha256()
        count = 0
        def verified_chunks():
            nonlocal count
            for chunk in provider.read(key):
                observed.update(chunk); count += len(chunk)
                if declared:
                    if count>65536:raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
                    result_bytes.extend(chunk)
                yield chunk
        stream = verified_chunks()
        valid = validator(stream)
        for _ in stream: pass
        if observed.hexdigest() != blob.digest or count != blob.size:
            raise RuntimeErrorCode('ARTIFACT_DIGEST_MISMATCH')
        if valid is not True:
            raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
        from .result_contract import validate_bytes
        validate_bytes(snapshot['input'],result_bytes)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch, authorize=True)
            if a['state'] != 'validating': raise RuntimeErrorCode('INVALID_TRANSITION')
            from .work_authorizations import require_current
            await require_current(c,run,a,now,'accept_result',session_id=a['session_id'])
            from .tool_invocations import require_settled
            await require_settled(c,self.scope,run_id)
            aid = str(uuid4())
            await c.execute(artifacts.insert().values(id=aid, run_id=run_id, attempt_id=attempt_id,
                digest=blob.digest, size=blob.size, provider=provider.name, storage_key=blob.key, created_at=now))
            await c.execute(attempts.update().where(attempts.c.id == attempt_id).values(state='completed', finished_at=now))
            await self._event(c, run, 'run.completed', {'artifact_id':aid}, now, state='succeeded')
            return aid

    async def get(self, run_id, *, diagnostics=False):
        # Keep run and attempt snapshots coherent without joining the SQLite writer queue.
        async with self.db.read(self.scope) as c:
            run = await self._run(c, run_id)
            run['attempts'] = [dict(a) for a in (await c.execute(select(attempts).where(attempts.c.run_id == run_id).order_by(attempts.c.epoch))).mappings()]
            from .faults import bound_policy, view
            run['recovery']=await view(c,self.scope,run_id)
            run['recovery_policy']=await bound_policy(c,self.scope,run['attempt_id'])
            run['observed_at'] = await self.db.now(c)
            from .execution_budgets import view as budget_view
            run['execution_budget']=await budget_view(c,self.scope,run_id,run['observed_at'])
            from .tool_budgets import view as tool_budget_view
            run['tool_budget']=await tool_budget_view(c,self.scope,run_id)
            from .usage import view as usage_view
            run['model_usage']=await usage_view(c,self.scope,run_id)
            if diagnostics:
                from .diagnostics import view as diagnostics_view
                run['diagnostics']=await diagnostics_view(c,self.scope,run,await self.db.now(c))
            from .child_authorizations import view as family_view
            run['family']=await family_view(c,self.scope,run_id)
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
            return await self._import_history(c, source, kind, source_id, record, hashed)

    async def _import_history(self, c, source, kind, source_id, record, hashed):
        where = and_(imports.c.scope==self.scope, imports.c.source==source, imports.c.kind==kind, imports.c.source_id==source_id)
        old = (await c.execute(select(imports).where(where))).mappings().first()
        if old:
            if old['digest'] != hashed: raise RuntimeErrorCode('IMPORT_CONFLICT')
            return dict(old)
        value = dict(scope=self.scope,source=source,kind=kind,source_id=source_id,target_id=str(uuid4()),digest=hashed,
                     record=record, acceptance='legacy_unverified')
        await c.execute(imports.insert().values(**value))
        return value


    async def cancel(self, run_id, expected_version, *, access_guard=None):
        async with self.db.transaction(self.scope) as c:
            run = await self._run(c, run_id)
            if access_guard:await asyncio.to_thread(access_guard,run)
            if run['version'] != expected_version: raise RuntimeErrorCode('VERSION_CONFLICT')
            if run['state'] in TERMINAL: return run
            # Unclaimed work is safe to cancel immediately; running work needs confirmation.
            state = 'cancelled' if run['state'] in {'queued','retry_wait'} else run['state']
            await self._event(c, run, 'run.cancel_requested', {}, await self.db.now(c), state=state, cancel_requested=True)
            return await self._run(c, run_id)

    async def confirm_cancel(self, run_id, attempt_id, worker_id, instance, epoch, evidence_ref):
        identifier(evidence_ref)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, run_id, attempt_id, worker_id, instance, epoch)
            if not run['cancel_requested']: raise RuntimeErrorCode('CANCEL_NOT_REQUESTED')
            await c.execute(attempts.update().where(attempts.c.id==attempt_id).values(state='cancelled',finished_at=now))
            await self._event(c, run, 'run.cancel_confirmed', {'evidence_ref':evidence_ref}, now, state='cancelled')

    async def observe_process(self, binding, observation, *, adapter, boot_id):
        """Trusted lifecycle evidence, never supplied by a model message."""
        from dataclasses import asdict
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, *binding)
            value = dict(asdict(observation), adapter=adapter, worker_boot_id=boot_id, observed_at=now)
            bounded(value)
            old = a['remote'].get('runtime', {})
            if old.get('process_instance') and observation.process_instance and old['process_instance'] != observation.process_instance:
                raise RuntimeErrorCode('PROCESS_CHANGED')
            changes = {'remote':dict(a['remote'], runtime=value)}
            if observation.pid and observation.process_instance:
                changes.update(process_id=observation.pid, process_instance=observation.process_instance)
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(**changes))
            if (old.get('state'), old.get('process_instance')) != (observation.state, observation.process_instance):
                await self._event(c, run, 'runtime.observed', value, now)

    async def reject_unlaunched(self, binding, code, *, retry_unlaunched=False, retry_after=0):
        identifier(code)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, *binding)
            if a['state']!='claimed' or a['process_instance'] or a['process_id'] or a['session_id'] or a['remote']:
                raise RuntimeErrorCode('LAUNCH_ALREADY_STARTED')
            state,error='failed',{'code':code}
            if retry_unlaunched:
                from .faults import failed
                state,error=await failed(c,self,run,a,code,now,retry_unlaunched=True,retry_after=retry_after)
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(
                state='failed',error=error,finished_at=now))
            await self._event(c,run,'attempt.preflight_failed',{'attempt_id':a['id'],**error},now,state=state)

    async def retain_output(self, binding, provider, key, kind):
        """Retain raw stdout/stderr even for failed/cancelled results; no success claim."""
        if kind not in {'stdout', 'stderr'}: raise RuntimeErrorCode('INVALID_OUTPUT_KIND')
        blob = provider.verify(key)
        from .usage import extract,record
        frames=await asyncio.to_thread(lambda:extract(b''.join(provider.read(key)))) if kind=='stdout' and blob.size<=16777216 else []
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, *binding)
            if frames and a['session_id']:await record(c,self.scope,run,a,frames,now,{'artifact_key':key})
            old = (await c.execute(select(artifacts.c.id).where(artifacts.c.attempt_id==a['id'],
                artifacts.c.digest==blob.digest))).first()
            if old: return old[0]
            aid = str(uuid4())
            await c.execute(artifacts.insert().values(id=aid,run_id=run['id'],attempt_id=a['id'],
                digest=blob.digest,size=blob.size,provider=provider.name,storage_key=key,created_at=now))
            await self._event(c,run,'attempt.output_retained',{'artifact_id':aid,'kind':kind},now)
            return aid

    async def finish_failed(self, binding, code):
        identifier(code)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, *binding)
            observation = a['remote'].get('runtime', {})
            if observation.get('alive') is not False or observation.get('state') not in {'exited','dead'}:
                raise RuntimeErrorCode('PROCESS_EXIT_NOT_CONFIRMED')
            from .faults import failed
            state,error=await failed(c,self,run,a,code,now)
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(
                state='failed',error=error,finished_at=now))
            await self._event(c,run,'attempt.failed',dict(attempt_id=a['id'],**error),now,state=state)

    async def mark_unknown(self, binding, code):
        """Observation/launch ambiguity fences the owner and never requeues automatically."""
        identifier(code)
        async with self.db.transaction(self.scope) as c:
            run, a, now = await self._owned(c, *binding)
            await c.execute(attempts.update().where(attempts.c.id==a['id']).values(
                state='result_unknown',error={'code':code}))
            await self._event(c,run,'attempt.reconciliation_required',{'attempt_id':a['id'],'code':code},now,
                state='reconciliation_required',epoch=run['epoch']+1)

    async def runtime_status(self, *, fresh_seconds=15):
        if not 0 < fresh_seconds <= 300: raise RuntimeErrorCode('INVALID_OBSERVATION_WINDOW')
        async with self.db.read(self.scope) as c:
            now = await self.db.now(c)
            worker_rows = [dict(r) for r in (await c.execute(select(workers).where(workers.c.scope==self.scope))).mappings()]
            from .fault_schema import circuits
            circuit_rows=[dict(r) for r in (await c.execute(select(circuits).where(circuits.c.scope==self.scope).order_by(circuits.c.updated_at.desc()).limit(50))).mappings()]
            rows = (await c.execute(select(attempts).join(runs,runs.c.id==attempts.c.run_id).where(
                runs.c.scope==self.scope, attempts.c.state.in_(ACTIVE_ATTEMPTS|{'result_unknown'})))).mappings().all()
            observations, running, sessions, unknown, paused = [], 0, 0, 0, 0
            for a in rows:
                obs = a['remote'].get('runtime', {})
                fresh = (a['state']!='result_unknown' and a['lease_until']>now
                    and obs.get('observed_at',0)>=now-fresh_seconds)
                exists = fresh and obs.get('alive') is True and bool(obs.get('pid')) and bool(obs.get('process_instance'))
                paused += int(bool(exists and obs.get('state')=='paused'))
                alive = exists and obs.get('state')!='paused'
                running += int(bool(alive))
                sessions += int(bool(alive and a['session_id'] and a['state'] in {'waiting_model','streaming','validating'}))
                unknown += int(not fresh)
                observations.append({'run_id':a['run_id'],'attempt_id':a['id'],'phase':a['state'],
                    'worker_id':a['worker_id'],'session_id':a['session_id'],'process_id':a['process_id'],
                    'process_instance':a['process_instance'],'last_output_at':a['last_output_at'],
                    'freshness':'fresh' if fresh else 'stale','runtime':obs})
            return {'scope':self.scope,'observed_at':now,'registered_slots':sum(w['slots'] for w in worker_rows),
                'confirmed_running_processes':running,'confirmed_running_sessions':sessions,'confirmed_paused_processes':paused,
                'actual_concurrency':None if unknown else sessions,'unknown_attempts':unknown,
                'workers':worker_rows,'attempts':observations,'circuits':circuit_rows}

    async def resolve_exit(self, expected_run, expected_attempt, receipt, provider, *, session_id, result_key, failure):
        """Trusted reconciler commits evidence under CAS; it never restores the old lease."""
        from .bridge import JsonlBridge
        blobs = {kind: provider.verify(receipt[kind]) for kind in ('stdout', 'stderr')}
        if result_key:
            blobs['result'] = provider.verify(result_key)
            if JsonlBridge.validate(provider.read(result_key)) is not True:
                raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
            async with self.db.read(self.scope) as c:
                snapshot=await self._run(c,expected_run['id'])
            if snapshot['input'].get('input_registration',{}).get('result_schema') is not None:
                if blobs['result'].size>65536:raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
                from .result_contract import validate_bytes
                validate_bytes(snapshot['input'],b''.join(provider.read(result_key)))
        from .usage import extract,record
        frames=await asyncio.to_thread(lambda:extract(b''.join(provider.read(receipt['stdout'])))) if blobs['stdout'].size<=16777216 else []
        obs = receipt['observation']
        if obs['alive'] or obs['state'] not in {'exited', 'dead'} or not obs['process_instance']:
            raise RuntimeErrorCode('PROCESS_EXIT_NOT_CONFIRMED')
        async with self.db.transaction(self.scope) as c:
            await admission(c, self.scope, ownership=True)
            run = await self._run(c, expected_run['id'])
            if run['version'] != expected_run['version']: raise RuntimeErrorCode('VERSION_CONFLICT')
            a = (await c.execute(select(attempts).where(attempts.c.id == run['attempt_id']))).mappings().first()
            if (run['state'] != 'reconciliation_required' or not a or a['state'] != 'result_unknown'
                    or a['id'] != expected_attempt['id'] or run['epoch'] != a['epoch']+1):
                raise RuntimeErrorCode('STALE_OWNER')
            expected = dict(scope=self.scope, run_id=run['id'], attempt_id=a['id'],
                worker_id=a['worker_id'], worker_instance=a['worker_instance'],
                epoch=a['epoch'], authorization_epoch=a['authorization_epoch'])
            if any(receipt.get(k) != v for k, v in expected.items()):
                raise RuntimeErrorCode('EXIT_RECEIPT_BINDING_MISMATCH')
            if obs['handle'] != a['id'] or (a['process_instance'] and a['process_instance'] != obs['process_instance']):
                raise RuntimeErrorCode('PROCESS_CHANGED')
            if a['session_id'] and a['session_id'] != session_id: raise RuntimeErrorCode('SESSION_CHANGED')
            if run['revoked'] or run['authorization_epoch'] != a['authorization_epoch']:
                failure, result_key = 'AUTH_REVOKED', None
            if run['cancel_requested']: state, attempt_state = 'cancelled', 'cancelled'
            elif failure: state, attempt_state = 'failed', 'failed'
            elif result_key: state, attempt_state = 'succeeded', 'completed'
            else: raise RuntimeErrorCode('EVIDENCE_INSUFFICIENT')
            now = await self.db.now(c)
            if state=='succeeded':
                from .work_authorizations import require_current
                await require_current(c,run,a,now,'accept_result',session_id=session_id)
                from .tool_invocations import require_settled
                await require_settled(c,self.scope,run['id'])
            error={'code':failure} if failure else None
            if state=='failed':
                from .faults import failed
                state,error=await failed(c,self,run,a,failure,now)
            linked = {}
            for kind, blob in blobs.items():
                if kind == 'result' and state != 'succeeded': continue
                old = (await c.execute(select(artifacts.c.id).where(artifacts.c.attempt_id == a['id'],
                    artifacts.c.digest == blob.digest))).first()
                aid = old[0] if old else str(uuid4())
                if not old:
                    await c.execute(artifacts.insert().values(id=aid, run_id=run['id'], attempt_id=a['id'],
                        digest=blob.digest, size=blob.size, provider=provider.name, storage_key=blob.key, created_at=now))
                linked[kind] = aid
            if frames and session_id:
                await record(c,self.scope,run,dict(a,session_id=session_id),frames,now,{'artifact_key':receipt['stdout'],'exit_receipt':digest(receipt)})
            remote = dict(a['remote'], runtime=dict(obs, adapter=a['remote']['launch']['adapter'], observed_at=now),
                reconciliation=dict(receipt_digest=digest(receipt), original_epoch=a['epoch'],
                    authorization_epoch=a['authorization_epoch']))
            await c.execute(attempts.update().where(attempts.c.id == a['id']).values(state=attempt_state,
                session_id=session_id or a['session_id'], process_instance=obs['process_instance'], remote=remote,
                error=error, finished_at=now))
            await self._event(c, run, 'attempt.exit_reconciled', dict(attempt_id=a['id'], artifacts=linked,
                receipt_digest=digest(receipt), original_epoch=a['epoch'], authorization_epoch=a['authorization_epoch']),
                now, state=state)
