"""Append-only, scoped technical release configuration and evidence records."""

from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .store import bounded, digest, identifier
from .technical_release_schema import records, require

KINDS = {"suite", "environment", "binding", "evaluation", "promotion"}


async def get(c, scope, kind, record_id, version=None, *, optional=False):
    await require(c)
    identifier(record_id)
    if kind not in KINDS:
        raise RuntimeErrorCode("INVALID_TECHNICAL_RECORD")
    query = select(records).where(
        records.c.scope == scope, records.c.kind == kind, records.c.id == record_id
    )
    if version is not None:
        query = query.where(records.c.version == version)
    row = (
        (await c.execute(query.order_by(records.c.version.desc()).limit(1)))
        .mappings()
        .first()
    )
    if row is None and not optional:
        raise RuntimeErrorCode("NOT_FOUND")
    return dict(row) if row else None


async def append(c, db, scope, kind, record_id, document, actor, expected_version=0):
    identifier(actor)
    bounded(document, 262144)
    prior = await get(c, scope, kind, record_id, optional=True)
    version = prior["version"] if prior else 0
    if (
        version == expected_version + 1
        and prior["document"] == document
        and prior["actor"] == actor
    ):
        return prior
    if type(expected_version) is not int or version != expected_version:
        raise RuntimeErrorCode("VERSION_CONFLICT")
    row = {
        "scope": scope,
        "kind": kind,
        "id": record_id,
        "version": version + 1,
        "document": document,
        "actor": actor,
        "created_at": await db.now(c),
    }
    await c.execute(records.insert().values(**row))
    return row


async def page(c, scope, kind, after="", limit=30):
    await require(c)
    if kind not in KINDS or type(limit) is not int or not 1 <= limit <= 50:
        raise RuntimeErrorCode("INVALID_TECHNICAL_QUERY")
    latest = (
        select(records.c.id, func.max(records.c.version).label("version"))
        .where(records.c.scope == scope, records.c.kind == kind, records.c.id > after)
        .group_by(records.c.id)
        .subquery()
    )
    rows = (
        (
            await c.execute(
                select(records)
                .join(
                    latest,
                    (records.c.id == latest.c.id)
                    & (records.c.version == latest.c.version),
                )
                .where(records.c.scope == scope, records.c.kind == kind)
                .order_by(records.c.id)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def reference(row):
    return {
        "id": row["id"],
        "version": row["version"],
        "digest": digest(row["document"]),
    }


async def resolve(c, scope, kind, ref):
    if not isinstance(ref, dict) or set(ref) != {"id", "version", "digest"}:
        raise RuntimeErrorCode("INVALID_TECHNICAL_REFERENCE")
    if type(ref["version"]) is not int or ref["version"] < 1:
        raise RuntimeErrorCode("INVALID_TECHNICAL_REFERENCE")
    row = await get(c, scope, kind, ref["id"], ref["version"])
    if reference(row) != ref:
        raise RuntimeErrorCode("TECHNICAL_REFERENCE_CHANGED")
    return row
