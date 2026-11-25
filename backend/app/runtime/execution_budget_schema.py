"""Additive execution budget policies and immutable per-Run deadline bindings v1."""

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
    "runtime_execution_budget_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
policies = Table(
    "runtime_execution_budget_policies",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("limit_id", String(160), primary_key=True),
    Column("max_runtime_seconds", Integer),
    Column("max_dispatches", Integer),
    Column("dispatch_window_seconds", Integer),
)
bindings = Table(
    "runtime_execution_budget_bindings",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("started_at", Float, nullable=False),
    Column("deadline_at", Float),
    Column("limits", JSON, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("EXECUTION_BUDGET_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("EXECUTION_BUDGET_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("EXECUTION_BUDGET_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
