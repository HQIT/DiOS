"""One read contract for CLI, Console and upstream application catalogs."""

from packaging.version import InvalidVersion, Version
from sqlalchemy import func, select

from .database import RuntimeErrorCode
from .package_admission import evidence, latest, statement
from .package_admission_schema import require as require_admission
from .package_admission_schema import reviews
from .package_lock import SHA
from .package_registry import PackageRegistry, version_key
from .package_registry_schema import packages, require
from .store import identifier

KINDS = {"harness", "skill", "tool", "prompt", "policy"}
DECISIONS = {"pending", "admitted", "rejected", "revoked"}


def filters(q="", kind="", version="", admission="", after="", limit=50):
    if (
        not all(isinstance(x, str) for x in (q, kind, version, admission, after))
        or len(q) > 80
        or len(version) > 160
        or any(ord(c) < 32 for c in q)
        or kind
        and kind not in KINDS
        or admission
        and admission not in DECISIONS
        or after
        and not SHA.fullmatch(after)
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise RuntimeErrorCode("INVALID_PACKAGE_FILTER")
    if version:
        try:
            Version(version)
        except InvalidVersion:
            raise RuntimeErrorCode("INVALID_PACKAGE_FILTER") from None
        version = version_key(version)
    return {
        "q": q.strip().lower(),
        "kind": kind,
        "version": version,
        "admission": admission,
        "after": after,
        "limit": limit,
    }


class PackageCatalog:
    def __init__(self, db, scope):
        identifier(scope)
        self.db, self.scope = db, scope

    async def project(self, c, row):
        package = row["manifest"]
        review = await latest(c, self.scope, row["digest"])
        decision = review["decision"] if review else "pending"
        limitations = ["E2AG_REQUIRED", "DEPENDENCY_CLOSURE_REVIEW_REQUIRED"]
        current = {
            "state": decision,
            "version": review["version"] if review else 0,
            "evidence_valid": None,
            "reason": None,
            "reviewed_at": review["created_at"] if review else None,
        }
        if "declaration" not in package:
            limitations.append("PACKAGE_DECLARATION_REQUIRED")
        if decision == "admitted":
            try:
                statement(review["statement"], package)
                await evidence(c, self.scope, review["statement"])
                current["evidence_valid"] = True
            except RuntimeErrorCode as exc:
                current.update(evidence_valid=False, reason=exc.code)
                limitations.append("PACKAGE_EVIDENCE_UNAVAILABLE")
        else:
            limitations.append("PACKAGE_NOT_ADMITTED")
        return {
            "digest": row["digest"],
            "name": package["name"],
            "version": package["version"],
            "kind": package["kind"],
            "dependencies": package["dependencies"],
            "declaration": package.get("declaration"),
            "admission": current,
            "limitations": limitations,
            "file_count": len(package["files"]),
            "size": sum(f["size"] for f in package["files"]),
            "registered_at": row["created_at"],
        }

    async def list(self, **options):
        f = filters(**options)
        decision = func.coalesce(
            select(reviews.c.decision)
            .where(
                reviews.c.scope == packages.c.scope,
                reviews.c.package_digest == packages.c.digest,
            )
            .order_by(reviews.c.version.desc())
            .limit(1)
            .scalar_subquery(),
            "pending",
        )
        query = select(packages).where(
            packages.c.scope == self.scope, packages.c.digest > f["after"]
        )
        if f["q"]:
            query = query.where(packages.c.name.contains(f["q"], autoescape=True))
        if f["kind"]:
            query = query.where(packages.c.manifest["kind"].as_string() == f["kind"])
        if f["version"]:
            query = query.where(packages.c.version == f["version"])
        if f["admission"]:
            query = query.where(decision == f["admission"])
        async with self.db.read(self.scope) as c:
            await require(c)
            await require_admission(c)
            rows = (
                (
                    await c.execute(
                        query.order_by(packages.c.digest).limit(f["limit"] + 1)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "schema_version": 1,
                "filters": f,
                "items": [await self.project(c, r) for r in rows[: f["limit"]]],
                "next_after": rows[f["limit"] - 1]["digest"]
                if len(rows) > f["limit"]
                else None,
            }

    async def detail(self, checksum):
        async with self.db.read(self.scope) as c:
            await require(c)
            await require_admission(c)
            try:
                row = await PackageRegistry(self.db, self.scope)._get(c, checksum)
            except RuntimeErrorCode as exc:
                if exc.code == "PACKAGE_NOT_REGISTERED":
                    raise RuntimeErrorCode("NOT_FOUND") from None
                raise
            value = await self.project(c, row)
            value["files"] = row["manifest"]["files"]
            # Evidence references retain Object permissions; no storage keys or bytes.
            review = await latest(c, self.scope, checksum)
            value["checks"] = (
                review["statement"]["predicate"]["checks"] if review else {}
            )
            return value

    async def releases(self, checksum, *, after="", limit=50):
        from .revision_schema import revisions

        if (
            not isinstance(after, str)
            or len(after) > 80
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise RuntimeErrorCode("INVALID_PACKAGE_FILTER")
        # Bounded candidate scan keeps the released Revision schema unchanged.
        # A continuation may exist even when this candidate page has no matches.
        await self.detail(checksum)
        async with self.db.read(self.scope) as c:
            rows = (
                (
                    await c.execute(
                        select(revisions)
                        .where(revisions.c.scope == self.scope, revisions.c.id > after)
                        .order_by(revisions.c.id)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            items = []
            for row in rows[:limit]:
                lock = (
                    row["manifest"]["configuration"]
                    .get("execution_profile", {})
                    .get("package_lock", {})
                )
                if any(item["digest"] == checksum for item in lock.get("packages", [])):
                    items.append(
                        {
                            k: row[k]
                            for k in (
                                "id",
                                "definition_id",
                                "sequence",
                                "content_digest",
                                "created_at",
                            )
                        }
                    )
            return {
                "items": items,
                "next_after": rows[limit - 1]["id"] if len(rows) > limit else None,
            }
