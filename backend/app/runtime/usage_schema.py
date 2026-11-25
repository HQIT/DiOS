"""Append-only agent-reported usage evidence, separate from financial settlement."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
)

metadata = MetaData()
versions = Table(
    "runtime_usage_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
observations = Table(
    "runtime_usage_observations",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("attempt_id", String(64), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("session_id", String(160), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("value", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("recorded_at", Float, nullable=False),
)
Index(
    "ix_usage_run",
    observations.c.scope,
    observations.c.run_id,
    observations.c.attempt_id,
    observations.c.sequence,
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("USAGE_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("USAGE_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("USAGE_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
