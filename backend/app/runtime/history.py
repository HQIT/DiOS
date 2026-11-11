"""Scope-bound history queries for CLI and future authorized API consumers.

Imported records and native executions have distinct identities and acceptance.
Source adapters supply explicit links; no relation is guessed from raw JSON.
"""
import base64
import json
from sqlalchemy import select, and_, tuple_
from .database import RuntimeErrorCode
from .store import identifier, digest, encoded
from .schema import imports, runs, attempts, artifacts
from .history_schema import links, check as check_history

RELATIONS = {
    'has_attempt': ('task', 'attempt'),
    'uses_session': ('attempt', 'session'),
    'produced_artifact': ('attempt', 'artifact'),
    'has_artifact': ('task', 'artifact'),
}
SUMMARY = ('source', 'kind', 'source_id', 'target_id', 'digest', 'acceptance')


def identity(source, kind, source_id):
    for value in (source, kind, source_id): identifier(value)
    return source, kind, source_id


def page_args(limit, cursor, binding, types):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RuntimeErrorCode('INVALID_LIMIT')
    if cursor is None: return None
    try:
        if not isinstance(cursor, str) or len(cursor) > 4096: raise ValueError()
        value = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
        if value['v'] != 1 or value['binding'] != digest(binding): raise ValueError()
        key = value['after']
        if not isinstance(key, list) or len(key) != len(types): raise ValueError()
        if any(type(item) is not expected for item, expected in zip(key, types)): raise ValueError()
        return key
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise RuntimeErrorCode('INVALID_CURSOR') from None


def page(rows, limit, binding, key):
    more = len(rows) > limit
    items = rows[:limit]
    cursor = None
    if more:
        cursor = base64.urlsafe_b64encode(encoded({'v': 1, 'binding': digest(binding), 'after': key(items[-1])})).decode('ascii')
    return {'items': items, 'next_cursor': cursor}


class HistoryQueries:
    def __init__(self, database, scope):
        identifier(scope)
        self.db, self.scope = database, scope

    def _where(self, source, kind, source_id):
        identity(source, kind, source_id)
        return and_(imports.c.scope == self.scope, imports.c.source == source,
                    imports.c.kind == kind, imports.c.source_id == source_id)

    async def _record(self, c, source, kind, source_id, include_record=False):
        columns = [imports.c[name] for name in SUMMARY]
        if include_record: columns.append(imports.c.record)
        value = (await c.execute(select(*columns).where(self._where(source, kind, source_id)))).mappings().first()
        if value is None: raise RuntimeErrorCode('NOT_FOUND')
        return dict(value)

    async def imported(self, source, kind, source_id, *, include_record=False):
        async with self.db.read(self.scope) as c:
            value = await self._record(c, source, kind, source_id, include_record)
            return {'origin': 'imported', 'scope': self.scope, 'record': value, 'observed_at': await self.db.now(c)}

    async def list_imported(self, *, source=None, kind=None, limit=50, cursor=None):
        for value in (source, kind):
            if value is not None: identifier(value)
        binding = ['imports', self.scope, source, kind]
        after = page_args(limit, cursor, binding, [str, str, str])
        query = select(*(imports.c[name] for name in SUMMARY)).where(imports.c.scope == self.scope)
        if source is not None: query = query.where(imports.c.source == source)
        if kind is not None: query = query.where(imports.c.kind == kind)
        order = (imports.c.source, imports.c.kind, imports.c.source_id)
        if after: query = query.where(tuple_(*order) > tuple(after))
        async with self.db.read(self.scope) as c:
            rows = [dict(r) for r in (await c.execute(query.order_by(*order).limit(limit+1))).mappings()]
            return {'origin': 'imported', 'scope': self.scope, 'observed_at': await self.db.now(c),
                    **page(rows, limit, binding, lambda r: [r['source'], r['kind'], r['source_id']])}

    async def link(self, source, kind, source_id, relation, target_source, target_kind, target_source_id):
        """Trusted source-adapter mapping. Immutable, idempotent and separate from raw history."""
        identity(source, kind, source_id)
        identity(target_source, target_kind, target_source_id)
        if not isinstance(relation, str) or RELATIONS.get(relation) != (kind, target_kind):
            raise RuntimeErrorCode('INVALID_HISTORY_RELATION')
        values = dict(scope=self.scope, source=source, kind=kind, source_id=source_id, relation=relation,
                      target_source=target_source, target_kind=target_kind, target_source_id=target_source_id)
        link_id = digest(values)
        async with self.db.transaction(self.scope) as c:
            await check_history(c)
            origin = await self._record(c, source, kind, source_id)
            old = (await c.execute(select(links).where(links.c.id == link_id, links.c.scope == self.scope))).mappings().first()
            if old: return dict(old)
            values.update(id=link_id, origin_digest=origin['digest'], created_at=await self.db.now(c))
            await c.execute(links.insert().values(**values))
            return values

    async def related(self, source, kind, source_id, *, direction='outgoing', limit=50, cursor=None):
        identity(source, kind, source_id)
        if direction not in {'outgoing', 'incoming'}: raise RuntimeErrorCode('INVALID_DIRECTION')
        binding = ['links', self.scope, source, kind, source_id, direction]
        after = page_args(limit, cursor, binding, [str])
        own_prefix, peer_prefix = ('', 'target_') if direction == 'outgoing' else ('target_', '')
        conditions = [links.c.scope == self.scope]
        for name, value in zip(('source', 'kind', 'source_id'), (source, kind, source_id)):
            conditions.append(links.c[own_prefix+name] == value)
        if after: conditions.append(links.c.id > after[0])
        join = and_(imports.c.scope == links.c.scope,
                    *(imports.c[name] == links.c[peer_prefix+name] for name in ('source', 'kind', 'source_id')))
        query = select(links, *(imports.c[name].label('peer_'+name) for name in ('target_id', 'digest', 'acceptance')))
        query = query.select_from(links.outerjoin(imports, join)).where(*conditions).order_by(links.c.id).limit(limit+1)
        async with self.db.read(self.scope) as c:
            await check_history(c)
            root = await self._record(c, source, kind, source_id)
            rows = []
            for row in (await c.execute(query)).mappings():
                peer = {name: row[peer_prefix+name] for name in ('source', 'kind', 'source_id')}
                peer.update({name: row['peer_'+name] for name in ('target_id', 'digest', 'acceptance')})
                rows.append({'id': row['id'], 'relation': row['relation'], 'direction': direction,
                    'origin_digest': row['origin_digest'], 'created_at': row['created_at'],
                    'resolution': 'resolved' if peer['target_id'] is not None else 'unresolved', 'peer': peer})
            return {'origin': 'imported', 'scope': self.scope, 'root': root, 'observed_at': await self.db.now(c),
                    **page(rows, limit, binding, lambda r: [r['id']])}

    async def run(self, run_id, *, limit=50, attempt_cursor=None, artifact_cursor=None):
        """Bounded native execution projection. Session handles are qualified by Attempt."""
        identifier(run_id)
        attempt_binding = ['run-attempts', self.scope, run_id]
        artifact_binding = ['run-artifacts', self.scope, run_id]
        after_attempt = page_args(limit, attempt_cursor, attempt_binding, [int])
        after_artifact = page_args(limit, artifact_cursor, artifact_binding, [str])
        async with self.db.read(self.scope) as c:
            root = (await c.execute(select(runs.c.id, runs.c.state, runs.c.version, runs.c.attempt_id,
                runs.c.created_at, runs.c.updated_at).where(runs.c.scope == self.scope, runs.c.id == run_id))).mappings().first()
            if root is None: raise RuntimeErrorCode('NOT_FOUND')
            aq = select(attempts.c.id, attempts.c.run_id, attempts.c.epoch, attempts.c.worker_id,
                attempts.c.worker_instance, attempts.c.state, attempts.c.session_id, attempts.c.process_id,
                attempts.c.process_instance, attempts.c.lease_until, attempts.c.last_output_at,
                attempts.c.started_at, attempts.c.finished_at).where(attempts.c.run_id == run_id)
            if after_attempt: aq = aq.where(attempts.c.epoch > after_attempt[0])
            attempt_rows = [dict(r) for r in (await c.execute(aq.order_by(attempts.c.epoch).limit(limit+1))).mappings()]
            bq = select(artifacts.c.id, artifacts.c.run_id, artifacts.c.attempt_id, artifacts.c.digest,
                artifacts.c.size, artifacts.c.provider, artifacts.c.created_at).where(artifacts.c.run_id == run_id)
            if after_artifact: bq = bq.where(artifacts.c.id > after_artifact[0])
            artifact_rows = [dict(r) for r in (await c.execute(bq.order_by(artifacts.c.id).limit(limit+1))).mappings()]
            attempt_page = page(attempt_rows, limit, attempt_binding, lambda r: [r['epoch']])
            sessions = [{'attempt_id': r['id'], 'worker_id': r['worker_id'], 'worker_instance': r['worker_instance'],
                         'session_id': r['session_id']} for r in attempt_page['items'] if r['session_id'] is not None]
            return {'origin': 'native', 'scope': self.scope, 'run': dict(root), 'observed_at': await self.db.now(c),
                    'attempts': attempt_page, 'sessions': sessions,
                    'artifacts': page(artifact_rows, limit, artifact_binding, lambda r: [r['id']])}
