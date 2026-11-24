"""Additive delegation records and immutable work receipts in the Runtime store."""

import hashlib
from pathlib import Path
from sqlalchemy import (
    MetaData,
    Table,
    Column as C,
    String as S,
    Integer as I,
    Float as F,
    JSON as J,
    Index,
    inspect,
    select,
)
from .database import RuntimeErrorCode

metadata = MetaData()
versions = Table(
    "runtime_delegation_schema",
    metadata,
    C("version", I, primary_key=True),
    C("checksum", S(64), nullable=False),
)
links = Table(
    "runtime_delegations",
    metadata,
    C("id", S(80), primary_key=True),
    C("scope", S(160), nullable=False),
    C("request_digest", S(64), nullable=False),
    C("parent_ref", S(80)),
    C("subject_ref", S(160), nullable=False),
    C("actor_ref", S(160), nullable=False),
    C("audience", S(160), nullable=False),
    C("permissions", J, nullable=False),
    C("expires_at", F, nullable=False),
    C("depth", I, nullable=False),
    C("authorization_epoch", I, nullable=False),
    C("state", S(32), nullable=False),
    C("version", I, nullable=False),
    C("policy_evaluator", J, nullable=False),
    C("created_at", F, nullable=False),
)
receipts = Table(
    "runtime_delegation_receipts",
    metadata,
    C("authorization_ref", S(80), primary_key=True),
    C("scope", S(160), nullable=False),
    C("run_id", S(64), nullable=False),
    C("leaf_ref", S(80), nullable=False),
    C("binding", J, nullable=False),
    C("policy_evaluator", J, nullable=False),
    C("created_at", F, nullable=False),
)
dependencies = Table(
    "runtime_delegation_dependencies",
    metadata,
    C("authorization_ref", S(80), primary_key=True),
    C("delegation_ref", S(80), primary_key=True),
    C("scope", S(160), nullable=False),
)
Index("ix_delegation_dependency", dependencies.c.scope, dependencies.c.delegation_ref)
events = Table(
    "runtime_delegation_events",
    metadata,
    C("id", S(64), primary_key=True),
    C("scope", S(160), nullable=False),
    C("delegation_ref", S(80), nullable=False),
    C("operation", S(40), nullable=False),
    C("details", J, nullable=False),
    C("created_at", F, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables) <= tables:
        raise RuntimeErrorCode("DELEGATION_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("DELEGATION_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    if not await check(c):
        raise RuntimeErrorCode("DELEGATION_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
