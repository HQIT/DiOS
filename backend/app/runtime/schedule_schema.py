"""Additive durable schedule state; separate from the frozen runtime v1 schema."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    select,
)

from .database import RuntimeErrorCode

metadata = MetaData()
versions = Table(
    "runtime_schedule_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
schedules = Table(
    "runtime_schedules",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("key", String(160), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("template", JSON, nullable=False),
    Column("binding", JSON, nullable=False),
    Column("expression", String(64), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
    Column("next_due", Float, nullable=False),
    Column("active_id", String(32)),
    Column("buffered_id", String(32)),
    Column("lease_owner", String(32)),
    Column("lease_epoch", Integer, nullable=False),
    Column("lease_until", Float, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint("scope", "key", name="uq_runtime_schedule_key"),
)
occurrences = Table(
    "runtime_schedule_occurrences",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("schedule_id", String(32), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("first_due", Float, nullable=False),
    Column("last_due", Float, nullable=False),
    Column("trigger_count", Integer),
    Column("key", String(160), nullable=False, unique=True),
    Column("template", JSON, nullable=False),
    Column("binding", JSON, nullable=False),
    Column("state", String(32), nullable=False),
    Column("run_id", String(64)),
    Column("failures", Integer, nullable=False),
    Column("retry_at", Float),
    Column("error", String(128), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    UniqueConstraint(
        "scope", "schedule_id", "revision", "first_due", name="uq_runtime_schedule_slot"
    ),
)
events = Table(
    "runtime_schedule_events",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("schedule_id", String(32), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
)
Index(
    "ix_runtime_schedule_scan",
    schedules.c.scope,
    schedules.c.updated_at,
    schedules.c.id,
)
Index(
    "ix_runtime_schedule_occurrence",
    occurrences.c.scope,
    occurrences.c.schedule_id,
    occurrences.c.first_due,
)
Index(
    "ix_runtime_schedule_event",
    events.c.scope,
    events.c.schedule_id,
    events.c.created_at,
)
CHECKSUM = hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode()).hexdigest()


async def check(c):
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("SCHEDULE_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("SCHEDULE_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    if not await check(c):
        raise RuntimeErrorCode("SCHEDULE_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
