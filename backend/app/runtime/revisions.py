"""Trusted version intake and scoped reads; does not authorize or launch work."""

import json
import re
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .revision_schema import receipts, require, revisions
from .store import digest, encoded, identifier

MAX_MANIFEST_BYTES = 256 * 1024
DEPENDENCY_KINDS = {
    "prompt",
    "model",
    "skill",
    "tool",
    "policy",
    "image",
    "adapter",
    "harness",
}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SENSITIVE = re.compile(
    r"(?:^|_)(?:password|passwd|token|credentials?|private_key|api_key|apikey|authorization|secrets?)$",
    re.IGNORECASE,
)


def invalid():
    raise RuntimeErrorCode("REVISION_MANIFEST_INVALID")


def safe_configuration(value, depth=0):
    """Reject credential-bearing structure; the trusted publisher owns input provenance."""
    if depth > 32:
        invalid()
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or SENSITIVE.search(
                re.sub(r"([a-z])([A-Z])", r"\1_\2", key)
            ):
                raise RuntimeErrorCode("REVISION_SECRET_REFERENCE_REQUIRED")
            safe_configuration(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            safe_configuration(child, depth + 1)
    elif isinstance(value, str):
        if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            raise RuntimeErrorCode("REVISION_SECRET_REFERENCE_REQUIRED")
        if "://" in value:
            try:
                url = urlsplit(value)
                if (
                    url.username
                    or url.password
                    or any(SENSITIVE.search(k) for k, _ in parse_qsl(url.query))
                ):
                    raise RuntimeErrorCode("REVISION_SECRET_REFERENCE_REQUIRED")
            except RuntimeErrorCode:
                raise
            except ValueError:
                invalid()
    elif value is not None and type(value) not in (bool, int, float):
        invalid()


def manifest(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "definition_id",
        "implementation_version",
        "configuration",
        "dependencies",
        "secret_refs",
        "artifact_refs",
    }:
        invalid()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        invalid()
    if not isinstance(value["definition_id"], str) or not ID.fullmatch(
        value["definition_id"]
    ):
        invalid()
    if (
        type(value["implementation_version"]) is not int
        or value["implementation_version"] < 1
    ):
        invalid()
    if not isinstance(value["configuration"], dict):
        invalid()
    safe_configuration(value["configuration"])
    if (
        not isinstance(value["dependencies"], dict)
        or set(value["dependencies"]) != DEPENDENCY_KINDS
    ):
        invalid()
    for kind, entries in value["dependencies"].items():
        if not isinstance(entries, list) or len(entries) > 128:
            invalid()
        names = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {
                "name",
                "reference",
                "version",
                "digest",
                "visibility",
            }:
                invalid()
            for key in ("name", "reference"):
                if (
                    not isinstance(entry[key], str)
                    or not entry[key]
                    or len(entry[key]) > 2048
                ):
                    invalid()
            safe_configuration(entry["reference"])
            if entry["name"] in names:
                invalid()
            names.add(entry["name"])
            visibility = entry["visibility"]
            version, checksum = entry["version"], entry["digest"]
            if version is not None and (
                not isinstance(version, str) or not version or len(version) > 256
            ):
                invalid()
            if visibility == "pinned":
                if not isinstance(checksum, str) or not DIGEST.fullmatch(checksum):
                    invalid()
            elif visibility == "declared":
                if not version or checksum is not None:
                    invalid()
            elif visibility == "opaque":
                if version is not None or checksum is not None:
                    invalid()
            else:
                invalid()
            # Controlled executable/config dependencies must be content-addressed.
            if (
                kind in {"prompt", "skill", "tool", "policy", "image", "adapter"}
                and visibility != "pinned"
            ):
                invalid()
    if not value["dependencies"]["adapter"]:
        invalid()
    if not value["dependencies"]["image"] and not value["dependencies"]["harness"]:
        invalid()
    for name in ("secret_refs", "artifact_refs"):
        refs = value[name]
        if (
            not isinstance(refs, list)
            or len(refs) > 128
            or any(not isinstance(ref, str) or not ID.fullmatch(ref) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            invalid()
    try:
        raw = encoded(value)
    except (ValueError, TypeError, RecursionError):
        invalid()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RuntimeErrorCode("REVISION_MANIFEST_TOO_LARGE")
    # Own the nested data; callers cannot change a snapshot after validation.
    return json.loads(raw)


def hashes(value):
    return "sha256:" + digest(value), "sha256:" + digest(value["configuration"])


def representation(row, include_manifest=False):
    value = manifest(row["manifest"])
    content, config = hashes(value)
    if (
        content != row["content_digest"]
        or config != row["config_digest"]
        or value["definition_id"] != row["definition_id"]
        or row["id"] != "rev-" + digest([row["scope"], value["definition_id"], content])
    ):
        raise RuntimeErrorCode("REVISION_INTEGRITY_FAILED")
    result = {
        "revision": {
            "id": row["id"],
            "version": 1,
            "definition_id": row["definition_id"],
            "content_digest": content,
            "config_digest": config,
            "implementation_version": value["implementation_version"],
            "artifact_refs": value["artifact_refs"],
        },
        "sequence": row["sequence"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "dependencies": value["dependencies"],
        "secret_refs": value["secret_refs"],
    }
    if include_manifest:
        result["manifest"] = value
    return result


class Revisions:
    def __init__(self, database, scope):
        identifier(scope)
        self.db, self.scope = database, scope

    async def _row(self, c, revision_id):
        row = (
            (
                await c.execute(
                    select(revisions).where(
                        revisions.c.scope == self.scope, revisions.c.id == revision_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeErrorCode("NOT_FOUND")
        return row

    async def create(self, key, value, *, actor):
        identifier(key)
        identifier(actor)
        value = manifest(value)
        content, config = hashes(value)
        revision_id = "rev-" + digest([self.scope, value["definition_id"], content])
        request = "sha256:" + digest([content, actor])
        async with self.db.transaction(self.scope) as c:
            await require(c)
            receipt = (
                (
                    await c.execute(
                        select(receipts).where(
                            receipts.c.scope == self.scope, receipts.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if receipt:
                if receipt["request_digest"] != request:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return representation(await self._row(c, receipt["revision_id"]))
            now = await self.db.now(c)
            existing = (
                (
                    await c.execute(
                        select(revisions).where(revisions.c.id == revision_id)
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                sequence = (
                    await c.execute(
                        select(func.max(revisions.c.sequence)).where(
                            revisions.c.scope == self.scope,
                            revisions.c.definition_id == value["definition_id"],
                        )
                    )
                ).scalar_one() or 0
                await c.execute(
                    revisions.insert().values(
                        id=revision_id,
                        scope=self.scope,
                        definition_id=value["definition_id"],
                        sequence=sequence + 1,
                        content_digest=content,
                        config_digest=config,
                        manifest=value,
                        created_at=now,
                        created_by=actor,
                    )
                )
            await c.execute(
                receipts.insert().values(
                    scope=self.scope,
                    key=key,
                    request_digest=request,
                    revision_id=revision_id,
                    actor=actor,
                    created_at=now,
                )
            )
            return representation(await self._row(c, revision_id))

    async def show(self, revision_id, *, include_manifest=False):
        identifier(revision_id)
        async with self.db.read(self.scope) as c:
            await require(c)
            return representation(await self._row(c, revision_id), include_manifest)

    async def list(self, definition_id, *, after=0, limit=50):
        if not isinstance(definition_id, str) or not ID.fullmatch(definition_id):
            invalid()
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await require(c)
            rows = (
                (
                    await c.execute(
                        select(revisions)
                        .where(
                            revisions.c.scope == self.scope,
                            revisions.c.definition_id == definition_id,
                            revisions.c.sequence > after,
                        )
                        .order_by(revisions.c.sequence)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            page = rows[:limit]
            return {
                "items": [representation(row) for row in page],
                "next_after": page[-1]["sequence"] if len(rows) > limit else None,
            }
