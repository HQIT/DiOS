"""Immutable authenticated issuer bindings for delegation records."""

import hashlib
from pathlib import Path
from sqlalchemy import (
    MetaData,
    Table,
    Column as C,
    String as S,
    Integer as I,
    JSON as J,
    Index,
    inspect,
    select,
)
from .database import RuntimeErrorCode

metadata = MetaData()
versions = Table(
    "runtime_delegation_identity_schema",
    metadata,
    C("version", I, primary_key=True),
    C("checksum", S(64), nullable=False),
)
issuers = Table(
    "runtime_delegation_issuers",
    metadata,
    C("delegation_ref", S(80), primary_key=True),
    C("scope", S(160), nullable=False),
    C("provider_path", S(2048), nullable=False),
    C("principal", J, nullable=False),
    C("profile_id", S(160), nullable=False),
)
Index("ix_delegation_issuer_scope", issuers.c.scope)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables) <= tables:
        raise RuntimeErrorCode("DELEGATION_IDENTITY_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("DELEGATION_IDENTITY_SCHEMA_INCOMPATIBLE")
    return True


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
