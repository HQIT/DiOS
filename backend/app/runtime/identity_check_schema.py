"""Additive identity observation/lease component; authorization remains in E2AG."""

import hashlib
from pathlib import Path

from sqlalchemy import Column as C
from sqlalchemy import Float as F
from sqlalchemy import Index, MetaData, Table, inspect, select
from sqlalchemy import Integer as I
from sqlalchemy import String as S

from .database import RuntimeErrorCode

metadata = MetaData()
versions = Table(
    "runtime_identity_check_schema",
    metadata,
    C("version", I, primary_key=True),
    C("checksum", S(64), nullable=False),
)
checks = Table(
    "runtime_identity_checks",
    metadata,
    C("run_id", S(64), primary_key=True),
    C("scope", S(160), nullable=False),
    C("authorization_id", S(80), nullable=False),
    C("binding_digest", S(64), nullable=False),
    C("generation", I, nullable=False),
    C("owner", S(64)),
    C("lease_until", F, nullable=False),
    C("next_check_at", F, nullable=False),
    C("checked_at", F),
    C("last_success_at", F),
    C("outcome", S(32), nullable=False),
    C("reason", S(80)),
    C("failures", I, nullable=False),
)
Index(
    "ix_identity_check_due",
    checks.c.scope,
    checks.c.next_check_at,
    checks.c.lease_until,
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    names = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not names.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(names):
        raise RuntimeErrorCode("IDENTITY_CHECK_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("IDENTITY_CHECK_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    if not await check(c):
        raise RuntimeErrorCode("IDENTITY_CHECK_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
