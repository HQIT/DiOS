"""Immutable first terminal transition per logical Run, with source event identity."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
)

metadata = MetaData()
versions = Table(
    "runtime_outcome_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
outcomes = Table(
    "runtime_run_outcomes",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("run_id", String(64), primary_key=True),
    Column("state", String(40), nullable=False),
    Column("finished_at", Float, nullable=False),
    Column("event_id", String(64), nullable=False),
    Column("event_version", Integer, nullable=False),
    Column("event_digest", String(64), nullable=False),
)
Index("ix_runtime_outcome_time", outcomes.c.scope, outcomes.c.finished_at)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("OUTCOME_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("OUTCOME_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("OUTCOME_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    from .schema import events, runs

    await c.run_sync(metadata.create_all)
    state = events.c.payload["state"].as_string()
    first = (
        select(
            runs.c.scope,
            events.c.run_id,
            state.label("state"),
            events.c.created_at.label("finished_at"),
            events.c.id.label("event_id"),
            events.c.version.label("event_version"),
            events.c.digest.label("event_digest"),
            func.row_number()
            .over(partition_by=events.c.run_id, order_by=events.c.version)
            .label("rank"),
        )
        .join(runs, runs.c.id == events.c.run_id)
        .where(state.in_(["succeeded", "failed", "cancelled"]))
        .subquery()
    )
    names = list(outcomes.c.keys())
    await c.execute(
        outcomes.insert().from_select(
            names, select(*(first.c[key] for key in names)).where(first.c.rank == 1)
        )
    )
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
