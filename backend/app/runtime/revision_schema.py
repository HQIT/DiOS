"""Additive immutable Revision storage. Released schema source is frozen."""

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
    UniqueConstraint,
    inspect,
    select,
    text,
)

metadata = MetaData()
versions = Table(
    "runtime_revision_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
revisions = Table(
    "runtime_revisions",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("scope", String(160), nullable=False),
    Column("definition_id", String(160), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("content_digest", String(71), nullable=False),
    Column("config_digest", String(71), nullable=False),
    Column("manifest", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("created_by", String(160), nullable=False),
    UniqueConstraint("scope", "definition_id", "sequence"),
    UniqueConstraint("scope", "definition_id", "content_digest"),
)
receipts = Table(
    "runtime_revision_receipts",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("key", String(160), primary_key=True),
    Column("request_digest", String(71), nullable=False),
    Column("revision_id", String(80), nullable=False),
    Column("actor", String(160), nullable=False),
    Column("created_at", Float, nullable=False),
)
GUARDS = {
    f"{name}_no_{action.lower()}": (name, action)
    for name in (revisions.name, receipts.name)
    for action in ("UPDATE", "DELETE")
}
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("REVISION_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("REVISION_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("REVISION_SCHEMA_INCOMPATIBLE")
    if c.dialect.name == "sqlite":
        guards = set(
            (
                await c.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger'")
                )
            ).scalars()
        )
    else:
        guards = set(
            (
                await c.execute(
                    text(
                        "SELECT trigger_name FROM information_schema.triggers WHERE trigger_schema=current_schema()"
                    )
                )
            ).scalars()
        )
    if not set(GUARDS).issubset(guards):
        raise RuntimeErrorCode("REVISION_IMMUTABILITY_GUARD_MISSING")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("REVISION_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    if c.dialect.name == "postgresql":
        await c.execute(
            text("""
            CREATE FUNCTION runtime_reject_revision_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'REVISION_IMMUTABLE';
            END; $$
        """)
        )
    for guard, (table, action) in GUARDS.items():
        if c.dialect.name == "sqlite":
            statement = (
                f"CREATE TRIGGER {guard} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'REVISION_IMMUTABLE'); END"
            )
        else:
            statement = (
                f"CREATE TRIGGER {guard} BEFORE {action} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION runtime_reject_revision_mutation()"
            )
        await c.execute(text(statement))
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
