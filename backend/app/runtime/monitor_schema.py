"""Versioned control-loop observations, independent from execution ownership."""

import hashlib
from pathlib import Path

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, inspect, select

metadata = MetaData()
versions = Table(
    "runtime_monitor_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
loops = Table(
    "runtime_loop_observations",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("service", String(64), primary_key=True),
    Column("instance", String(64), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("heartbeat_at", Float, nullable=False),
    Column("cycle_started_at", Float),
    Column("completed_at", Float),
    Column("succeeded_at", Float),
    Column("duration_seconds", Float),
    Column("cycles", Integer, nullable=False),
    Column("consecutive_errors", Integer, nullable=False),
    Column("error", String(64)),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("MONITOR_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("MONITOR_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("MONITOR_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
