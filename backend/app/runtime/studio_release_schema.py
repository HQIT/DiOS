"""Additive immutable Studio release ledger. Freeze after release."""

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
    text,
)

metadata = MetaData()
versions = Table(
    "runtime_studio_release_schema",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("checksum", String(64), nullable=False),
)
releases = Table(
    "runtime_studio_releases",
    metadata,
    Column("scope", String(160), primary_key=True),
    Column("id", String(160), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("state", String(20), nullable=False),
    Column("request_digest", String(64), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("actor", String(160), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
GUARDS = {
    f"{releases.name}_no_{action.lower()}": (releases.name, action)
    for action in ("UPDATE", "DELETE")
}
CHECKSUM = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


async def check(c):
    from .database import RuntimeErrorCode

    tables = set(await c.run_sync(lambda s: inspect(s).get_table_names()))
    if not tables.intersection(metadata.tables):
        return False
    if not set(metadata.tables).issubset(tables):
        raise RuntimeErrorCode("STUDIO_RELEASE_SCHEMA_INCOMPLETE")
    for name, table in metadata.tables.items():
        columns = await c.run_sync(lambda s, n=name: inspect(s).get_columns(n))
        if not set(table.columns.keys()).issubset(col["name"] for col in columns):
            raise RuntimeErrorCode("STUDIO_RELEASE_SCHEMA_INCOMPLETE")
    rows = (await c.execute(select(versions))).mappings().all()
    if len(rows) != 1 or rows[0]["version"] != 1 or rows[0]["checksum"] != CHECKSUM:
        raise RuntimeErrorCode("STUDIO_RELEASE_SCHEMA_INCOMPATIBLE")
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
        raise RuntimeErrorCode("STUDIO_RELEASE_IMMUTABILITY_GUARD_MISSING")
    return True


async def require(c):
    from .database import RuntimeErrorCode

    if not await check(c):
        raise RuntimeErrorCode("STUDIO_RELEASE_MIGRATION_REQUIRED")


async def migrate(c):
    if await check(c):
        return False
    await c.run_sync(metadata.create_all)
    if c.dialect.name == "postgresql":
        await c.execute(
            text("""
            CREATE FUNCTION runtime_reject_studio_release_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'STUDIO_RELEASE_IMMUTABLE';
            END; $$
        """)
        )
    for guard, (table, action) in GUARDS.items():
        if c.dialect.name == "sqlite":
            statement = (
                f"CREATE TRIGGER {guard} BEFORE {action} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'STUDIO_RELEASE_IMMUTABLE'); END"
            )
        else:
            statement = (
                f"CREATE TRIGGER {guard} BEFORE {action} ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION runtime_reject_studio_release_mutation()"
            )
        await c.execute(text(statement))
    await c.execute(versions.insert().values(version=1, checksum=CHECKSUM))
    return True
