"""Immutable delegation-to-parent execution bindings."""

import hashlib
from pathlib import Path

from sqlalchemy import JSON as J
from sqlalchemy import Column as C
from sqlalchemy import Index, MetaData, Table, inspect, select
from sqlalchemy import Integer as I
from sqlalchemy import String as S

metadata = MetaData()
versions = Table(
    "runtime_child_handoff_schema",
    metadata,
    C("version", I, primary_key=True),
    C("checksum", S(64), nullable=False),
)
handoffs = Table(
    "runtime_child_handoffs",
    metadata,
    C("delegation_ref", S(80), primary_key=True),
    C("scope", S(160), nullable=False),
    C("parent_run_id", S(64), nullable=False),
    C("binding", J, nullable=False),
)
Index("runtime_handoff_parent", handoffs.c.scope, handoffs.c.parent_run_id)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("CHILD_HANDOFF_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("CHILD_HANDOFF_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("CHILD_HANDOFF_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
