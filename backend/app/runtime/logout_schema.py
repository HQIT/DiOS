"""Durable verified logout inbox and revocation tombstones."""

import hashlib
from pathlib import Path
from sqlalchemy import Column as C, String as S, Float as F, Integer as I
from sqlalchemy import MetaData, Table, Index, inspect, select
from .database import RuntimeErrorCode

metadata = MetaData()
versions = Table(
    "runtime_logout_schema",
    metadata,
    C("version", I, primary_key=True),
    C("checksum", S(64), nullable=False),
)
events = Table(
    "runtime_logout_events",
    metadata,
    C("id", S(64), primary_key=True),
    C("scope", S(160), nullable=False),
    C("issuer", S(160), nullable=False),
    C("client_id", S(160), nullable=False),
    C("subject", S(160), nullable=False),
    C("session_id", S(160), nullable=False),
    C("event_id", S(1024), nullable=False),
    C("issued_at", F, nullable=False),
    C("expires_at", F, nullable=False),
    C("received_at", F, nullable=False),
    C("digest", S(64), nullable=False),
    C("token_digest", S(64), nullable=False),
    C("revoked_work_count", I, nullable=False),
)
Index(
    "ix_logout_scope_source",
    events.c.scope,
    events.c.issuer,
    events.c.client_id,
    events.c.subject,
    events.c.session_id,
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables) <= tables:
        raise RuntimeErrorCode("LOGOUT_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("LOGOUT_SCHEMA_INCOMPATIBLE")
    return True


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
