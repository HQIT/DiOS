"""Explicit migrations and database-owned clock/serialization for both providers."""
from contextlib import asynccontextmanager
import hashlib
from pathlib import Path
from sqlalchemy import select, text, inspect
from sqlalchemy.ext.asyncio import create_async_engine
from .schema import metadata, versions

SCHEMA_VERSION = 1
# The v1 schema source is frozen after release. Add a migration for future changes.
SCHEMA_CHECKSUM = hashlib.sha256(Path(__file__).with_name('schema.py').read_text(encoding='utf-8').encode('utf-8')).hexdigest()

class RuntimeErrorCode(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

class Database:
    def __init__(self, url):
        self.engine = create_async_engine(url)
        if self.engine.dialect.name not in {'sqlite', 'postgresql'}:
            raise RuntimeErrorCode('UNSUPPORTED_DATABASE')

    @asynccontextmanager
    async def transaction(self, scope, *, check=True):
        async with self.engine.connect() as c:
            try:
                if c.dialect.name == 'sqlite':
                    await c.execute(text('PRAGMA busy_timeout=15000'))
                    await c.execute(text('PRAGMA foreign_keys=ON'))
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
                return {'version': SCHEMA_VERSION, 'changed': False}
            if any(n.startswith('runtime_') for n in existing):
                raise RuntimeErrorCode('UNVERSIONED_RUNTIME_SCHEMA')
            await c.run_sync(metadata.create_all)
            await c.execute(versions.insert().values(version=SCHEMA_VERSION, checksum=SCHEMA_CHECKSUM))
            return {'version': SCHEMA_VERSION, 'changed': True}

    async def now(self, c):
        query = "SELECT (julianday('now') - 2440587.5) * 86400.0" if c.dialect.name == 'sqlite' else 'SELECT EXTRACT(EPOCH FROM clock_timestamp())'
        return float((await c.execute(text(query))).scalar_one())

    async def close(self):
        await self.engine.dispose()
