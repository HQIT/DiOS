"""Scope admission control. Draining never kills or reassigns an active Attempt."""
import asyncio
import hashlib
import math
import time
from sqlalchemy import select, func
from .database import RuntimeErrorCode
from .schema import runs, attempts, events
from .lifecycle_schema import control, check


def scope_key(scope):
    return 'scope:'+hashlib.sha256(scope.encode()).hexdigest()


async def admission(c, scope, *, ownership=False):
    # Existing v1 installations keep their previous behavior until explicit migrate.
    if not await check(c): return True
    rows = (await c.execute(select(control).where(control.c.key.in_(['global', scope_key(scope)])))).mappings().all()
    for row in rows:
        if row['key'] == 'global' and row['mode'] != 'running':
            raise RuntimeErrorCode('RESTORE_RECONCILIATION_REQUIRED')
    return ownership or all(row['mode'] == 'running' for row in rows)


class Lifecycle:
    def __init__(self, database, scope):
        from .store import identifier
        identifier(scope)
        self.db, self.scope = database, scope

    async def _set(self, c, key, mode):
        if not await check(c): raise RuntimeErrorCode('LIFECYCLE_MIGRATION_REQUIRED')
        old = (await c.execute(select(control).where(control.c.key == key))).mappings().first()
        if old and old['mode'] == mode: return old['revision']
        value = dict(key=key, scope=None if key=='global' else self.scope, mode=mode,
                     revision=old['revision']+1 if old else 1, updated_at=await self.db.now(c))
        if old: await c.execute(control.update().where(control.c.key == key).values(**value))
        else: await c.execute(control.insert().values(**value))
        return value['revision']

    async def begin_drain(self):
        async with self.db.transaction(self.scope) as c:
            await admission(c, self.scope, ownership=True)
            await self._set(c, scope_key(self.scope), 'draining')
        return await self.status()

    async def resume(self, expected_revision):
        async with self.db.transaction(self.scope) as c:
            await admission(c, self.scope, ownership=True)
            row = (await c.execute(select(control).where(control.c.key == scope_key(self.scope)))).mappings().first()
            if not row or row['revision'] != expected_revision: raise RuntimeErrorCode('VERSION_CONFLICT')
            await self._set(c, scope_key(self.scope), 'running')
        return await self.status()

    async def hold_restored(self):
        """Only on a private, unpublished restore target; no automatic release API."""
        async with self.db.transaction('__restore__') as c:
            await self._set(c, 'global', 'restore_hold')

    async def status(self):
        from .store import ACTIVE_ATTEMPTS
        async with self.db.read(self.scope) as c:
            if not await check(c): raise RuntimeErrorCode('LIFECYCLE_MIGRATION_REQUIRED')
            controls = {r['key']: dict(r) for r in (await c.execute(select(control).where(
                control.c.key.in_(['global',scope_key(self.scope)])))).mappings()}
            row = controls.get(scope_key(self.scope), {'mode':'running','revision':0})
            held = controls.get('global', {}).get('mode', 'running') != 'running'
            states = dict((await c.execute(select(attempts.c.state,func.count()).join(runs,runs.c.id==attempts.c.run_id)
                .where(runs.c.scope==self.scope).group_by(attempts.c.state))).all())
            active = sum(states.get(s,0) for s in ACTIVE_ATTEMPTS)
            unknown = states.get('result_unknown',0)
            queued = (await c.execute(select(func.count()).select_from(runs).where(
                runs.c.scope==self.scope,runs.c.state=='queued'))).scalar_one()
            event_count = (await c.execute(select(func.count()).select_from(events.join(runs,runs.c.id==events.c.run_id))
                .where(runs.c.scope==self.scope))).scalar_one()
            return dict(scope=self.scope, mode='restore_hold' if held else row['mode'], revision=row['revision'],
                active_attempts=active, reconciliation_required=unknown, queued_runs=queued,
                event_count=event_count, observed_at=await self.db.now(c),
                ready_for_cutover=not held and row['mode']=='draining' and active==0 and unknown==0)

    async def wait_drained(self, timeout=30, poll_interval=.1):
        if not math.isfinite(timeout) or timeout < 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise RuntimeErrorCode('INVALID_DRAIN_LIMIT')
        deadline=time.monotonic()+timeout
        while True:
            try:
                status=await asyncio.wait_for(self.status(),max(.01,deadline-time.monotonic()))
            except TimeoutError:
                raise RuntimeErrorCode('DRAIN_OBSERVATION_TIMEOUT') from None
            if status['ready_for_cutover']: return status
            if status['mode']!='draining': raise RuntimeErrorCode('DRAIN_NOT_STARTED')
            if time.monotonic()>=deadline: raise RuntimeErrorCode('DRAIN_TIMEOUT')
            await asyncio.sleep(min(poll_interval,max(0,deadline-time.monotonic())))
