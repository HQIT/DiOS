"""Object-backed knowledge Provider v1. No business entities or content cache.

The source binding index is transactional; content and permissions are read
through the existing governed object boundary for every query.
"""
import math
from sqlalchemy import select
from app.runtime.database import RuntimeErrorCode
from app.runtime.knowledge_schema import sources, receipts, require
from app.runtime.objects import validate_ref
from app.runtime.store import digest, identifier
from app.services.object_access import ObjectReader

CAPABILITIES = {
    'contract': 'dios.knowledge/v1', 'provider': 'object-read-through-v1',
    'operations': ['search', 'source.replace', 'source.delete'],
    'content': ['text/plain; charset=utf-8'], 'index': 'object-reference',
    'content_cache': False, 'mutation_completion': 'applied',
    'max_sources': 100, 'max_source_bytes': 1024 * 1024, 'max_query_bytes': 8 * 1024 * 1024,
}


class ObjectKnowledgeProvider:
    provider_id = 'object-read-through-v1'
    ranking = 'literal-terms-v1'
    def __init__(self, console):
        self.console = console
        self.db, self.scope = console.db, console.scope
        self.reader = ObjectReader(console)

    def where(self, source_id):
        identifier(source_id)
        return sources.c.scope == self.scope, sources.c.id == source_id

    def capabilities(self):
        return dict(CAPABILITIES, provider=self.provider_id)

    async def rank(self, candidates, terms):
        matches = []
        for row, value, text in candidates:
            score = sum(term in text.casefold() for term in terms)
            if score:
                matches.append((score, row, value, text))
        return sorted(matches, key=lambda hit: (-hit[0], hit[1]['id']))

    async def source(self, source_id):
        if self.console.principal is not None:
            raise RuntimeErrorCode('AUTH_OPERATOR_REQUIRED')
        async with self.db.read(self.scope) as c:
            await require(c)
            row = (await c.execute(select(sources).where(*self.where(source_id)))).mappings().first()
        if row is None:
            raise RuntimeErrorCode('NOT_FOUND')
        return {key: value for key, value in row.items() if key != 'scope'}

    async def change(self, source_id, body):
        if self.console.principal is not None:
            raise RuntimeErrorCode('AUTH_OPERATOR_REQUIRED')
        self.where(source_id)
        allowed = {'expected_revision', 'idempotency_key', 'object_ref', 'provenance', 'expires_at', 'state'}
        if not isinstance(body, dict) or set(body) != allowed:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_MUTATION')
        key, revision, state = body['idempotency_key'], body['expected_revision'], body['state']
        identifier(key)
        if type(revision) is not int or revision < 0 or state not in {'active', 'deleted'}:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_MUTATION')
        expiry = body['expires_at']
        if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0):
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_EXPIRY')
        ref, provenance = body['object_ref'], body['provenance']
        if state == 'active':
            validate_ref(ref)
            if ref['kind'] not in {'file', 'artifact'}:
                raise RuntimeErrorCode('INVALID_KNOWLEDGE_CONTENT')
            if not isinstance(provenance, dict) or set(provenance) != {'namespace', 'key', 'version', 'locator'}:
                raise RuntimeErrorCode('INVALID_KNOWLEDGE_PROVENANCE')
            for value in provenance.values():
                identifier(value)
        elif ref is not None or provenance is not None or expiry is not None:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_MUTATION')
        hashed = digest([source_id, body])
        # Check replay before reading a source that may since have been deleted.
        async with self.db.read(self.scope) as c:
            await require(c)
            old = (await c.execute(select(receipts).where(receipts.c.scope == self.scope, receipts.c.key == key))).mappings().first()
        if old:
            if old['digest'] != hashed:
                raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
            return old['response']
        if state == 'active':
            await self.content(ref)
        async with self.db.transaction(self.scope) as c:
            await require(c)
            old = (await c.execute(select(receipts).where(receipts.c.scope == self.scope, receipts.c.key == key))).mappings().first()
            if old:
                if old['digest'] != hashed:
                    raise RuntimeErrorCode('IDEMPOTENCY_CONFLICT')
                return old['response']
            current = (await c.execute(select(sources).where(*self.where(source_id)))).mappings().first()
            if (current['revision'] if current else 0) != revision:
                raise RuntimeErrorCode('VERSION_CONFLICT')
            now = await self.db.now(c)
            if expiry is not None and expiry <= now:
                raise RuntimeErrorCode('INVALID_KNOWLEDGE_EXPIRY')
            if current is None:
                count = len((await c.execute(select(sources.c.id).where(sources.c.scope == self.scope))).all())
                if count >= CAPABILITIES['max_sources']:
                    raise RuntimeErrorCode('KNOWLEDGE_CAPACITY_EXCEEDED')
            row = dict(revision=revision + 1, state=state, object_ref=ref, provenance=provenance,
                       expires_at=expiry, updated_at=now)
            if current:
                await c.execute(sources.update().where(*self.where(source_id)).values(**row))
            else:
                await c.execute(sources.insert().values(scope=self.scope, id=source_id, **row))
            response = {'contract': 'dios.knowledge/v1', 'source_id': source_id, 'revision': revision + 1,
                        'state': state, 'operation_status': 'applied',
                        'index_status': 'applied', 'cache_status': 'not_used'}
            await c.execute(receipts.insert().values(scope=self.scope, key=key, digest=hashed,
                           request={'source_id': source_id, **body}, response=response))
            return response

    async def content(self, ref):
        value = await self.reader.get(ref)
        if value['descriptor'] is None or value['descriptor']['size'] > CAPABILITIES['max_source_bytes']:
            raise RuntimeErrorCode('KNOWLEDGE_CONTENT_TOO_LARGE')
        raw = b''.join([chunk async for chunk in self.reader.stream(value)])
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_ENCODING') from None
        if '\x00' in text:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_ENCODING')
        return value, text

    async def snapshot(self):
        async with self.db.read(self.scope) as c:
            await require(c)
            rows = (await c.execute(select(sources).where(sources.c.scope == self.scope).order_by(sources.c.id)
                                   .limit(CAPABILITIES['max_sources'] + 1))).mappings().all()
            now = await self.db.now(c)
        if len(rows) > CAPABILITIES['max_sources']:
            raise RuntimeErrorCode('KNOWLEDGE_CAPACITY_EXCEEDED')
        return [dict(row) for row in rows], now

    async def search(self, query, limit=3):
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 20:
            raise RuntimeErrorCode('INVALID_KNOWLEDGE_QUERY')
        snapshot, now = await self.snapshot()
        candidates, consumed = [], 0
        terms = query.casefold().split()
        for row in snapshot:
            if row['state'] != 'active' or row['expires_at'] is not None and row['expires_at'] <= now:
                continue
            try:
                value, text = await self.content(row['object_ref'])
            except RuntimeErrorCode as exc:
                if exc.code == 'NOT_FOUND':
                    continue
                raise
            consumed += value['descriptor']['size']
            if consumed > CAPABILITIES['max_query_bytes']:
                raise RuntimeErrorCode('KNOWLEDGE_QUERY_TOO_LARGE')
            candidates.append((row, value, text))
        matches = await self.rank(candidates, terms)
        # A source replacement/delete during retrieval invalidates the response.
        current, now = await self.snapshot()
        if current != snapshot:
            raise RuntimeErrorCode('KNOWLEDGE_VIEW_CHANGED')
        results = []
        for _, row, value, text in matches:
            if row['expires_at'] is not None and row['expires_at'] <= now:
                continue
            try:
                await self.reader.authorize(value, 'object.read')
                await self.reader.authorize(value, 'object.download')
            except RuntimeErrorCode as exc:
                if exc.code == 'NOT_FOUND':
                    continue
                raise
            results.append({'source_id': row['id'], 'source_revision': row['revision'],
                            'object_ref': row['object_ref'], 'provenance': row['provenance'],
                            'descriptor': value['descriptor'], 'text': text})
            if len(results) == limit:
                break
        refresh = getattr(self.console, 'credential_recheck', None)
        if refresh:
            await refresh()
        current, final_time = await self.snapshot()
        if current != snapshot:
            raise RuntimeErrorCode('KNOWLEDGE_VIEW_CHANGED')
        expiries = {row['id']: row['expires_at'] for row in snapshot}
        results = [item for item in results if expiries[item['source_id']] is None or expiries[item['source_id']] > final_time]
        return {'contract': 'dios.knowledge/v1', 'provider': self.provider_id, 'items': results,
                'ranking': self.ranking, 'cache': 'none'}
