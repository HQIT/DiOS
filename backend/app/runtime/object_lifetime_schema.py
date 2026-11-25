"""Object retention, expiration and immutable tombstone receipts."""

import hashlib
from pathlib import Path
from sqlalchemy import (
    JSON,
    Column as C,
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
    "runtime_object_lifetime_schema",
    metadata,
    C("version", Integer, primary_key=True),
    C("checksum", String(64), nullable=False),
)
states = Table(
    "runtime_object_lifetimes",
    metadata,
    C("scope", String(160), primary_key=True),
    C("kind", String(20), primary_key=True),
    C("object_id", String(160), primary_key=True),
    C("object_version", Integer, primary_key=True),
    C("revision", Integer, nullable=False),
    C("retain_until", Float, nullable=False),
    C("expires_at", Float),
    C("deleted_at", Float),
    C("reason", String(160)),
    C("updated_at", Float, nullable=False),
    C("updated_by", String(160), nullable=False),
)
receipts = Table(
    "runtime_object_lifetime_receipts",
    metadata,
    C("scope", String(160), primary_key=True),
    C("key", String(160), primary_key=True),
    C("digest", String(64), nullable=False),
    C("response", JSON, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("OBJECT_LIFETIME_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("OBJECT_LIFETIME_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("OBJECT_LIFETIME_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("OBJECT_LIFETIME_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
