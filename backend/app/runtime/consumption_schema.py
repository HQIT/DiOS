"""Additive reservation and settlement accounting v1; integer quantities only."""

import hashlib
from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
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
    "runtime_consumption_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
accounts = Table(
    "runtime_consumption_accounts",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("id", String(160), primary_key=True),
    Column("selector", JSON, nullable=False),
    Column("unit", String(32), nullable=False),
    Column("maximum", BigInteger, nullable=False),
    Column("per_attempt", BigInteger, nullable=False),
    Column("enforcement", String(32), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", Float, nullable=False),
)
reservations = Table(
    "runtime_consumption_reservations",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("account_id", String(160), primary_key=True),
    Column("attempt_id", String(64), primary_key=True),
    Column("run_id", String(64), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("account_revision", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
)
settlements = Table(
    "runtime_consumption_settlements",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("account_id", String(160), primary_key=True),
    Column("attempt_id", String(64), primary_key=True),
    Column("actual", BigInteger, nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("actor", String(160), nullable=False),
    Column("created_at", Float, nullable=False),
)
receipts = Table(
    "runtime_consumption_receipts",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("digest", String(64), nullable=False),
    Column("record", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
Index(
    "ix_consumption_run",
    reservations.c.scope,
    reservations.c.run_id,
    reservations.c.attempt_id,
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("CONSUMPTION_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("CONSUMPTION_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("CONSUMPTION_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
