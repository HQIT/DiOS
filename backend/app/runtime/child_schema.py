"""Versioned parent execution bindings; task identity and history remain durable."""
import hashlib
from pathlib import Path
from sqlalchemy import MetaData, Table, Column as C, String as S, Integer as I, Float as F, UniqueConstraint, Index, select, inspect

metadata = MetaData()
versions = Table('runtime_child_schema', metadata, C('version', I, primary_key=True), C('checksum', S(64), nullable=False))
children = Table('runtime_child_runs', metadata,
    C('run_id', S(64), primary_key=True), C('scope', S(160), nullable=False),
    C('parent_run_id', S(64), nullable=False), C('parent_attempt_id', S(64), nullable=False),
    C('parent_session_id', S(160), nullable=False), C('parent_epoch', I, nullable=False),
    C('parent_authorization_epoch', I, nullable=False), C('root_run_id', S(64), nullable=False),
    C('depth', I, nullable=False), C('root_limit', I, nullable=False), C('depth_limit', I, nullable=False),
    C('request_key', S(160), nullable=False), C('created_at', F, nullable=False),
    UniqueConstraint('scope', 'parent_run_id', 'request_key'))
Index('runtime_children_parent', children.c.scope, children.c.parent_run_id)
Index('runtime_children_root', children.c.scope, children.c.root_run_id)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables): return False
    if not set(metadata.tables).issubset(tables): raise RuntimeErrorCode('CHILD_SCHEMA_INCOMPLETE')
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]['version'] != 1: raise RuntimeErrorCode('CHILD_SCHEMA_INCOMPATIBLE')
    if rows[0]['checksum'] != CHECKSUM: raise RuntimeErrorCode('CHILD_CHECKSUM_MISMATCH')
    return True


async def require(c):
    from .database import RuntimeErrorCode
    if not await check(c): raise RuntimeErrorCode('CHILD_MIGRATION_REQUIRED')


async def migrate(c):
    if await check(c): return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
