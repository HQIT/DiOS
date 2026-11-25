"""Durable offline collection plans and per-version release evidence."""

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
    "runtime_object_collection_schema",
    metadata,
    C("version", Integer, primary_key=True),
    C("checksum", String(64), nullable=False),
)
batches = Table(
    "runtime_object_collection_batches",
    metadata,
    C("key", String(160), primary_key=True),
    C("digest", String(64), nullable=False),
    C("plan", JSON, nullable=False),
    C("report", JSON),
)
releases = Table(
    "runtime_object_collections",
    metadata,
    C("scope", String(160), primary_key=True),
    C("kind", String(20), primary_key=True),
    C("object_id", String(160), primary_key=True),
    C("object_version", Integer, primary_key=True),
    C("key", String(64), nullable=False),
    C("state", String(40), nullable=False),
    C("operation_key", String(160), nullable=False),
    C("updated_at", Float, nullable=False),
)
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("OBJECT_COLLECTION_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("OBJECT_COLLECTION_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("OBJECT_COLLECTION_SCHEMA_INCOMPATIBLE")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("OBJECT_COLLECTION_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
