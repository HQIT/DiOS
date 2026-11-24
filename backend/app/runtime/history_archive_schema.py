"""Versioned archive references for imported history, independent of native Runs."""

import hashlib
from pathlib import Path

from sqlalchemy import Column as C
from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
)

from .schema import imports

metadata = MetaData()
versions = Table(
    "runtime_history_archive_schema",
    metadata,
    C("version", Integer, primary_key=True),
    C("checksum", String(64), nullable=False),
)
objects = Table(
    "runtime_history_archive_objects",
    metadata,
    C("scope", String(160), primary_key=True),
    C("source", String(160), primary_key=True),
    C("kind", String(40), primary_key=True),
    C("source_id", String(160), primary_key=True),
    C("digest", String(64), primary_key=True),
    C("size", Integer, nullable=False),
    C("provider", String(80), nullable=False),
    ForeignKeyConstraint(
        ["scope", "source", "kind", "source_id"],
        [imports.c.scope, imports.c.source, imports.c.kind, imports.c.source_id],
    ),
)
Index("ix_history_archive_digest", objects.c.digest)
CHECKSUM = hashlib.sha256(
    Path(__file__).read_text(encoding="utf-8").encode()
).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    if not await c.run_sync(lambda s: inspect(s).has_table(versions.name)):
        raise RuntimeErrorCode("HISTORY_ARCHIVE_MIGRATION_REQUIRED")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("HISTORY_ARCHIVE_SCHEMA_INCOMPATIBLE")


async def migrate(c):
    from .database import RuntimeErrorCode

    existing = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if versions.name in existing:
        await check(c)
        for name, table in metadata.tables.items():
            if name not in existing:
                raise RuntimeErrorCode("HISTORY_ARCHIVE_SCHEMA_INCOMPLETE")
            columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
            if not set(table.columns.keys()).issubset({col["name"] for col in columns}):
                raise RuntimeErrorCode("HISTORY_ARCHIVE_SCHEMA_INCOMPLETE")
        return False
    if existing.intersection(metadata.tables):
        raise RuntimeErrorCode("HISTORY_ARCHIVE_SCHEMA_UNVERSIONED")
    await c.run_sync(metadata.create_all)
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
