"""Additive history component v1; the original runtime schema stays frozen."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData, Table, Column as C, String as S, Integer as I, Float as F, Index
from sqlalchemy import select, inspect

metadata = MetaData()
versions = Table('runtime_history_schema', metadata,
    C('version', I, primary_key=True), C('checksum', S(64), nullable=False))
links = Table('runtime_history_links', metadata,
    C('id', S(64), primary_key=True), C('scope', S(160), nullable=False),
    C('source', S(160), nullable=False), C('kind', S(40), nullable=False), C('source_id', S(160), nullable=False),
    C('relation', S(40), nullable=False), C('target_source', S(160), nullable=False),
    C('target_kind', S(40), nullable=False), C('target_source_id', S(160), nullable=False),
    C('origin_digest', S(64), nullable=False), C('created_at', F, nullable=False))
Index('ix_runtime_history_origin', links.c.scope, links.c.source, links.c.kind, links.c.source_id)
Index('ix_runtime_history_target', links.c.scope, links.c.target_source, links.c.target_kind, links.c.target_source_id)
CHECKSUM = hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode('utf-8')).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode
    if not await c.run_sync(lambda s: inspect(s).has_table('runtime_history_schema')):
        raise RuntimeErrorCode('HISTORY_MIGRATION_REQUIRED')
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]['version'] != 1:
        raise RuntimeErrorCode('HISTORY_SCHEMA_INCOMPATIBLE')
    if rows[0]['checksum'] != CHECKSUM:
        raise RuntimeErrorCode('HISTORY_CHECKSUM_MISMATCH')


async def migrate(c):
    from .database import RuntimeErrorCode
    existing = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if 'runtime_history_schema' in existing:
        await check(c)
        for name, table in metadata.tables.items():
            if name not in existing: raise RuntimeErrorCode('HISTORY_SCHEMA_INCOMPLETE')
            columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
            if not set(table.columns.keys()).issubset({col['name'] for col in columns}):
                raise RuntimeErrorCode('HISTORY_SCHEMA_INCOMPLETE')
        return False
    if existing.intersection(metadata.tables): raise RuntimeErrorCode('HISTORY_SCHEMA_UNVERSIONED')
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
