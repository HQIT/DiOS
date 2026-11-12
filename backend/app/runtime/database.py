"""Explicit migrations and database-owned clock/serialization for both providers."""
from contextlib import asynccontextmanager
import asyncio
import hashlib
from pathlib import Path
from sqlalchemy import select, text, inspect, event
from sqlalchemy.ext.asyncio import create_async_engine
from .schema import metadata, versions
from .write_queue import WriteGate, WriteQueueError

SCHEMA_VERSION = 1
# The v1 schema source is frozen after release. Add a migration for future changes.
SCHEMA_CHECKSUM = hashlib.sha256(Path(__file__).with_name('schema.py').read_text(encoding='utf-8').encode('utf-8')).hexdigest()

class RuntimeErrorCode(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _ReadConnection:
    """Finish each buffered DB operation before propagating task cancellation.

    Cancelling aiosqlite between execute and fetchall can strand an SQLite cursor
    even after connection invalidation. The driver operation must finish first.
    """
    def __init__(self, connection): self.connection = connection
    def __getattr__(self, name): return getattr(self.connection, name)
    async def _finish(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        try: return await asyncio.shield(task)
        except asyncio.CancelledError:
            try: await task
            finally: raise
    async def execute(self, *args, **kwargs):
        return await self._finish(self.connection.execute(*args, **kwargs))
    async def run_sync(self, *args, **kwargs):
        return await self._finish(self.connection.run_sync(*args, **kwargs))

class Database:
    def __init__(self, url, *, write_queue_capacity=64, write_queue_timeout=5.0):
        self.engine = create_async_engine(url)
        if self.engine.dialect.name not in {'sqlite', 'postgresql'}:
            raise RuntimeErrorCode('UNSUPPORTED_DATABASE')
        self.write_gate = WriteGate(write_queue_capacity, write_queue_timeout) if self.engine.dialect.name == 'sqlite' else None
        if self.write_gate:
            @event.listens_for(self.engine.sync_engine, 'connect')
            def configure_sqlite(connection, _):
                cursor = connection.cursor()
                cursor.execute('PRAGMA busy_timeout=15000')
                cursor.execute('PRAGMA foreign_keys=ON')
                cursor.execute('PRAGMA synchronous=FULL')
                cursor.close()

    @asynccontextmanager
    async def _write_slot(self):
        if self.write_gate is None:
            yield
            return
        try:
            async with self.write_gate.slot():
                yield
        except WriteQueueError as exc:
            raise RuntimeErrorCode(exc.code) from exc

    @asynccontextmanager
    async def read(self, scope):
        if self.write_gate is None:
            # Preserve the existing PostgreSQL snapshot/serialization contract.
            async with self.transaction(scope) as c:
                yield c
            return
        async with self.engine.connect() as c:
            reader = _ReadConnection(c)
            try:
                await reader.execute(text('PRAGMA query_only=ON'))
                await reader.execute(text('BEGIN'))
                await self.check_schema(reader)
                yield reader
            except asyncio.CancelledError:
                # Interrupted aiosqlite cursors must not return a read lock to the pool.
                cleanup = asyncio.create_task(c.invalidate())
                try: await asyncio.shield(cleanup)
                except asyncio.CancelledError: await cleanup
                raise
            finally:
                if not c.invalidated:
                    async def reset_read_connection():
                        try:
                            await c.rollback()
                            await c.execute(text('PRAGMA query_only=OFF'))
                            await c.commit()
                        except BaseException:
                            await c.invalidate()
                            raise
                    cleanup = asyncio.create_task(reset_read_connection())
                    try: await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        await cleanup
                        raise

    @asynccontextmanager
    async def transaction(self, scope, *, check=True):
        async with self._write_slot(), self.engine.connect() as c:
            try:
                if c.dialect.name == 'sqlite':
                    await c.execute(text('BEGIN IMMEDIATE'))
                else:
                    key = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], 'big', signed=True)
                    await c.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                if check:
                    await self.check_schema(c)
                yield c
                await c.commit()
            except BaseException:
                await c.rollback()
                raise

    async def check_schema(self, c):
        exists = await c.run_sync(lambda s: inspect(s).has_table('runtime_schema'))
        if not exists:
            raise RuntimeErrorCode('MIGRATION_REQUIRED')
        rows = (await c.execute(select(versions))).mappings().all()
        if len(rows) != 1 or rows[0]['version'] != SCHEMA_VERSION:
            raise RuntimeErrorCode('SCHEMA_INCOMPATIBLE')
        if rows[0]['checksum'] != SCHEMA_CHECKSUM:
            raise RuntimeErrorCode('MIGRATION_CHECKSUM_MISMATCH')

    async def migrate(self):
        from .history_schema import migrate as migrate_history
        from .lifecycle_schema import migrate as migrate_lifecycle
        async with self.transaction('__runtime_migration__', check=False) as c:
            existing = await c.run_sync(lambda s: inspect(s).get_table_names())
            if 'runtime_schema' in existing:
                await self.check_schema(c)
                actual = set(existing)
                if not set(metadata.tables).issubset(actual):
                    raise RuntimeErrorCode('SCHEMA_INCOMPLETE')
                for name, table in metadata.tables.items():
                    columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
                    if not set(table.columns.keys()).issubset({col['name'] for col in columns}):
                        raise RuntimeErrorCode('SCHEMA_INCOMPLETE')
                changed = await migrate_history(c)
                lifecycle_changed = await migrate_lifecycle(c)
                return {'version': SCHEMA_VERSION, 'history_version': 1, 'lifecycle_version': 1,
                        'changed': changed or lifecycle_changed}
            if any(n.startswith('runtime_') for n in existing):
                raise RuntimeErrorCode('UNVERSIONED_RUNTIME_SCHEMA')
            await c.run_sync(metadata.create_all)
            await c.execute(versions.insert().values(version=SCHEMA_VERSION, checksum=SCHEMA_CHECKSUM))
            await migrate_history(c)
            await migrate_lifecycle(c)
            return {'version': SCHEMA_VERSION, 'history_version': 1, 'lifecycle_version': 1, 'changed': True}

    async def now(self, c):
        query = "SELECT (julianday('now') - 2440587.5) * 86400.0" if c.dialect.name == 'sqlite' else 'SELECT EXTRACT(EPOCH FROM clock_timestamp())'
        return float((await c.execute(text(query))).scalar_one())

    async def close(self):
        if self.write_gate:
            await self.write_gate.drain()
        await self.engine.dispose()
