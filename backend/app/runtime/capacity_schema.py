"""Additive shared execution limits and immutable administration receipts (v1)."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
)

metadata = MetaData()
versions = Table(
    "runtime_capacity_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
limits = Table(
    "runtime_capacity_limits",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("id", String(160), primary_key=True),
    Column("selector", JSON, nullable=False),
    Column("max_concurrent", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", Float, nullable=False),
)
changes = Table(
    "runtime_capacity_changes",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("limit_id", String(160), primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("record", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("CAPACITY_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("CAPACITY_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("CAPACITY_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
