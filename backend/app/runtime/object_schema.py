"""Additive object versions and explicit provenance. Freeze after release."""
import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    Float,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
)
from sqlalchemy import Column as C

metadata = MetaData()
versions = Table('runtime_object_schema', metadata,
    C('version', Integer, primary_key=True), C('checksum', String(64), nullable=False))
heads = Table('runtime_objects', metadata,
    C('scope', String(160), primary_key=True), C('id', String(80), primary_key=True),
    C('version', Integer, nullable=False), C('created_at', Float, nullable=False))
contents = Table('runtime_object_versions', metadata,
    C('scope', String(160), primary_key=True), C('object_id', String(80), primary_key=True),
    C('version', Integer, primary_key=True), C('digest', String(64), nullable=False),
    C('size', Integer, nullable=False), C('media_type', String(160), nullable=False),
    C('provider', String(80), nullable=False), C('storage_key', String(160), nullable=False),
    C('created_by', String(160), nullable=False), C('created_at', Float, nullable=False),
    ForeignKeyConstraint(['scope','object_id'], ['runtime_objects.scope','runtime_objects.id']))
receipts = Table('runtime_object_receipts', metadata,
    C('scope', String(160), primary_key=True), C('key', String(160), primary_key=True),
    C('request_digest', String(64), nullable=False), C('object_id', String(80), nullable=False),
    C('version', Integer, nullable=False),
    ForeignKeyConstraint(['scope','object_id','version'],
        ['runtime_object_versions.scope','runtime_object_versions.object_id','runtime_object_versions.version']))
links = Table('runtime_object_links', metadata,
    C('scope', String(160), primary_key=True), C('id', String(64), primary_key=True),
    C('source_kind', String(20), nullable=False), C('source_id', String(160), nullable=False),
    C('source_version', Integer, nullable=False), C('relation', String(30), nullable=False),
    C('target', JSON, nullable=False), C('created_by', String(160), nullable=False), C('created_at', Float, nullable=False))
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode('OBJECT_SCHEMA_INCOMPLETE')
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col['name'] for col in columns):
            raise RuntimeErrorCode('OBJECT_SCHEMA_INCOMPLETE')
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows)!=1 or rows[0]['version']!=1 or rows[0]['checksum']!=CHECKSUM:
        raise RuntimeErrorCode('OBJECT_SCHEMA_INCOMPATIBLE')
    return True


async def require(c):
    from .database import RuntimeErrorCode
    if not await check(c):
        raise RuntimeErrorCode('OBJECT_MIGRATION_REQUIRED')


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
