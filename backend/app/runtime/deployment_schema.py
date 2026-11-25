"""Persistent deployment intent, rollout evidence and idempotent operator requests."""

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
    "runtime_deployment_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
deployments = Table(
    "runtime_deployments",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("id", String(160), primary_key=True),
    Column("definition_id", String(160), nullable=False),
    Column("version", Integer, nullable=False),
    Column("revision_id", String(80), nullable=False),
    Column("active_revision_id", String(80)),
    Column("previous_revision_id", String(80)),
    Column("state", String(32), nullable=False),
    Column("rollout_id", String(64), nullable=False),
    Column("error", String(80)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
rollouts = Table(
    "runtime_deployment_rollouts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("deployment_id", String(160), nullable=False),
    Column("revision_id", String(80), nullable=False),
    Column("previous_revision_id", String(80)),
    Column("state", String(32), nullable=False),
    Column("probe_run_id", String(64)),
    Column("probe_key", String(160), nullable=False),
    Column("actor", String(160), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("deadline", Float, nullable=False),
    Column("error", String(80)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
events = Table(
    "runtime_deployment_events",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("deployment_id", String(160), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("rollout_id", String(64), nullable=False),
    Column("type", String(80), nullable=False),
    Column("actor", String(160), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
receipts = Table(
    "runtime_deployment_receipts",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("request_digest", String(64), nullable=False),
    Column("deployment_id", String(160), nullable=False),
    Column("rollout_id", String(64), nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("DEPLOYMENT_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("DEPLOYMENT_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("DEPLOYMENT_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("DEPLOYMENT_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
