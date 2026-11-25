"""Scoped external declarations and explicit compatibility decisions."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
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
    "runtime_compatibility_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
endpoints = Table(
    "runtime_endpoint_compatibility",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("profile_digest", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("snapshot_digest", String(64), nullable=False),
    Column("accepted_digest", String(64)),
    Column("state", String(32), nullable=False),
    Column("error", String(80)),
    Column("observed_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
events = Table(
    "runtime_compatibility_events",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("profile_digest", String(64), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("type", String(80), nullable=False),
    Column("actor", String(160), nullable=False),
    Column("snapshot_digest", String(64), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("evidence", String(1000)),
    Column("created_at", Float, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("COMPATIBILITY_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("COMPATIBILITY_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("COMPATIBILITY_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("COMPATIBILITY_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
