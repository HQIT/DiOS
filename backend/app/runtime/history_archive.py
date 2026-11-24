"""Atomic history groups with immutable binary source evidence; trusted operator API."""

import re

from sqlalchemy import select

from .artifacts import LocalArtifacts
from .database import RuntimeErrorCode
from .history import RELATIONS, HistoryQueries, identity
from .history_archive_schema import check, objects
from .history_schema import check as check_history
from .store import RuntimeStore, bounded, digest, identifier
from .tool_receipts import durable_io


def validate_group(source, group):
    identifier(source)
    if not isinstance(group, dict) or set(group) != {"records", "links"}:
        raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
    records, relations = group["records"], group["links"]
    if (
        not isinstance(records, list)
        or not 1 <= len(records) <= 100
        or not isinstance(relations, list)
        or len(relations) > 200
    ):
        raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
    bounded(group, 1024 * 1024)
    seen = set()
    for row in records:
        if not isinstance(row, dict) or set(row) != {
            "kind",
            "source_id",
            "record",
            "blobs",
        }:
            raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
        identity(source, row["kind"], row["source_id"])
        if (
            row["kind"] not in {"task", "attempt", "session", "artifact"}
            or (row["kind"], row["source_id"]) in seen
        ):
            raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
        seen.add((row["kind"], row["source_id"]))
        if not isinstance(row["record"], dict):
            raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
        bounded(row["record"], 65536)
        if not isinstance(row["blobs"], list) or len(row["blobs"]) > 16:
            raise RuntimeErrorCode("INVALID_HISTORY_GROUP")
        blob_ids = set()
        for blob in row["blobs"]:
            if (
                not isinstance(blob, dict)
                or set(blob) != {"digest", "size"}
                or not isinstance(blob["digest"], str)
                or not re.fullmatch("[0-9a-f]{64}", blob["digest"])
                or type(blob["size"]) is not int
                or not 0 <= blob["size"] <= 128 * 1024 * 1024
                or blob["digest"] in blob_ids
            ):
                raise RuntimeErrorCode("INVALID_HISTORY_BLOB")
            blob_ids.add(blob["digest"])
    for link in relations:
        if not isinstance(link, dict) or set(link) != {
            "kind",
            "source_id",
            "relation",
            "target_kind",
            "target_source_id",
        }:
            raise RuntimeErrorCode("INVALID_HISTORY_RELATION")
        if (
            (link["kind"], link["source_id"]) not in seen
            or (link["target_kind"], link["target_source_id"]) not in seen
            or RELATIONS.get(link["relation"]) != (link["kind"], link["target_kind"])
        ):
            raise RuntimeErrorCode("INVALID_HISTORY_RELATION")


def copy_blobs(group, archive, destination):
    for row in group["records"]:
        for ref in row["blobs"]:
            verified = archive.verify(ref["digest"])
            if verified.size != ref["size"]:
                raise RuntimeErrorCode("HISTORY_BLOB_SIZE_MISMATCH")
            copied = destination.put(
                archive.read(ref["digest"]), expected_digest=ref["digest"]
            )
            if copied.size != ref["size"]:
                raise RuntimeErrorCode("HISTORY_BLOB_SIZE_MISMATCH")


async def import_group(database, scope, source, group, archive, destination):
    """Verify/copy blobs, then atomically register one bounded group and its links."""
    validate_group(source, group)
    store, query = RuntimeStore(database, scope), HistoryQueries(database, scope)
    await durable_io(copy_blobs, group, archive, destination)
    mapped = []
    async with database.transaction(scope) as c:
        await check(c)
        await check_history(c)
        for row in group["records"]:
            # The blob set is part of the immutable identity digest, including an empty set.
            record = {"metadata": row["record"], "archive_blobs": row["blobs"]}
            bounded(record, 65536)
            item = await store._import_history(
                c, source, row["kind"], row["source_id"], record, digest(record)
            )
            mapped.append(
                {
                    k: item[k]
                    for k in ("source", "kind", "source_id", "target_id", "acceptance")
                }
            )
            for ref in row["blobs"]:
                values = dict(
                    scope=scope,
                    source=source,
                    kind=row["kind"],
                    source_id=row["source_id"],
                    **ref,
                    provider=LocalArtifacts.name,
                )
                where = [
                    objects.c[k] == values[k]
                    for k in ("scope", "source", "kind", "source_id", "digest")
                ]
                old = (
                    (await c.execute(select(objects).where(*where))).mappings().first()
                )
                if old:
                    if dict(old) != values:
                        raise RuntimeErrorCode("IMPORT_CONFLICT")
                else:
                    await c.execute(objects.insert().values(**values))
        for link in group["links"]:
            values = dict(scope=scope, source=source, target_source=source, **link)
            await query._link(c, values, digest(values))
    return mapped
