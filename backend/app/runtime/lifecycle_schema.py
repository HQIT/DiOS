"""Frozen lifecycle component v1: persistent admission and restore hold."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData, Table, Column as C, String as S, Integer as I, Float as F
from sqlalchemy import select, inspect

metadata = MetaData()
versions = Table('runtime_lifecycle_schema', metadata,
    C('version', I, primary_key=True), C('checksum', S(64), nullable=False))
control = Table('runtime_lifecycle', metadata, C('key', S(80), primary_key=True),
    C('scope', S(160)), C('mode', S(40), nullable=False), C('revision', I, nullable=False),
    C('updated_at', F, nullable=False))
CHECKSUM = hashlib.sha256(Path(__file__).read_text(encoding='utf-8').encode()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables): return False
    if not set(metadata.tables).issubset(tables): raise RuntimeErrorCode('LIFECYCLE_SCHEMA_INCOMPLETE')
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]['version'] != 1: raise RuntimeErrorCode('LIFECYCLE_SCHEMA_INCOMPATIBLE')
    if rows[0]['checksum'] != CHECKSUM: raise RuntimeErrorCode('LIFECYCLE_CHECKSUM_MISMATCH')
    return True


async def migrate(c):
    from .database import RuntimeErrorCode
    if await check(c):
        for name, table in metadata.tables.items():
            columns=await c.run_sync(lambda s,n=name:inspect(s).get_columns(n))
            if not set(table.columns.keys()).issubset({col['name'] for col in columns}):
                raise RuntimeErrorCode('LIFECYCLE_SCHEMA_INCOMPLETE')
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
