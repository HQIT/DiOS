"""Trusted scoped package review, immutable evidence and live admission fencing."""

from sqlalchemy import select

from .database import Database, RuntimeErrorCode
from .objects import Objects, validate_ref
from .package_admission_schema import require, reviews
from .package_lock import SHA, fail, manifest, snapshot, verify
from .store import digest, identifier

STATEMENT = "https://in-toto.io/Statement/v1"
PREDICATE = "urn:dios:package-review:v1"


def statement(value, package):
    value = snapshot(value)
    declaration = package.get("declaration")
    if declaration is None:
        fail("PACKAGE_DECLARATION_REQUIRED")
    if (
        not isinstance(value, dict)
        or set(value) != {"_type", "subject", "predicateType", "predicate"}
        or value["_type"] != STATEMENT
        or value["predicateType"] != PREDICATE
        or value["subject"]
        != [{"name": package["name"], "digest": {"sha256": digest(package)}}]
    ):
        fail("PACKAGE_REVIEW_SUBJECT_MISMATCH")
    predicate = value["predicate"]
    if (
        not isinstance(predicate, dict)
        or set(predicate) != {"declaration", "checks"}
        or predicate["declaration"] != declaration
    ):
        fail("PACKAGE_REVIEW_DECLARATION_MISMATCH")
    checks = predicate["checks"]
    expected = {"source", "license"} | {
        "capability:" + c for c in declaration["capabilities"]
    }
    if not isinstance(checks, dict) or set(checks) != expected:
        fail("PACKAGE_VERIFICATION_INCOMPLETE")
    for entry in checks.values():
        if not isinstance(entry, dict) or set(entry) != {
            "result",
            "evidence",
            "digest",
        }:
            fail("PACKAGE_VERIFICATION_INCOMPLETE")
        if entry["result"] not in ("passed", "failed"):
            fail("PACKAGE_VERIFICATION_INCOMPLETE")
        ref = validate_ref(entry["evidence"])
        if (
            ref["kind"] not in ("file", "artifact")
            or not isinstance(entry["digest"], str)
            or not SHA.fullmatch(entry["digest"])
        ):
            fail("PACKAGE_EVIDENCE_INVALID")
    return value


async def evidence(c, scope, value):
    from .object_lifetime import ObjectLifetime

    for entry in value["predicate"]["checks"].values():
        ref = entry["evidence"]
        obj = await Objects(None, scope)._get(c, ref)
        if obj["descriptor"]["digest"] != entry["digest"]:
            fail("PACKAGE_EVIDENCE_DIGEST_MISMATCH")
        await ObjectLifetime(Database, scope).available(c, ref)


async def latest(c, scope, checksum):
    return (
        (
            await c.execute(
                select(reviews)
                .where(reviews.c.scope == scope, reviews.c.package_digest == checksum)
                .order_by(reviews.c.version.desc())
                .limit(1)
            )
        )
        .mappings()
        .first()
    )


def public(row):
    if row is None:
        return {"state": "pending", "version": 0}
    return {
        k: row[k]
        for k in (
            "package_digest",
            "version",
            "decision",
            "statement",
            "actor",
            "created_at",
        )
    }


class PackageAdmission:
    def __init__(self, db, scope):
        identifier(scope)
        self.db, self.scope = db, scope

    async def review(
        self, value, decision, attestation, *, expected_version, key, actor
    ):
        identifier(key)
        identifier(actor)
        package = manifest(value)
        attestation = statement(attestation, package)
        if decision not in ("admitted", "rejected", "revoked"):
            fail("PACKAGE_REVIEW_DECISION_INVALID")
        if type(expected_version) is not int or expected_version < 0:
            fail("INVALID_EXPECTED_VERSION")
        if decision == "admitted" and any(
            v["result"] != "passed" for v in attestation["predicate"]["checks"].values()
        ):
            fail("PACKAGE_VERIFICATION_FAILED")
        checksum = "sha256:" + digest(package)
        request_digest = digest(
            [package, decision, attestation, expected_version, actor]
        )
        async with self.db.transaction(self.scope) as c:
            await require(c)
            prior = (
                (
                    await c.execute(
                        select(reviews).where(
                            reviews.c.scope == self.scope, reviews.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["request_digest"] != request_digest:
                    fail("IDEMPOTENCY_CONFLICT")
                return public(prior)
            current = await latest(c, self.scope, checksum)
            if (current["version"] if current else 0) != expected_version:
                fail("VERSION_CONFLICT")
            # Withdrawal must remain possible even after evidence is no longer available.
            if decision == "admitted":
                await evidence(c, self.scope, attestation)
            row = {
                "scope": self.scope,
                "package_digest": checksum,
                "version": expected_version + 1,
                "key": key,
                "request_digest": request_digest,
                "decision": decision,
                "statement": attestation,
                "actor": actor,
                "created_at": await self.db.now(c),
            }
            await c.execute(reviews.insert().values(**row))
            return public(row)

    async def show(self, checksum, *, after=0, limit=50):
        if not isinstance(checksum, str) or not SHA.fullmatch(checksum):
            fail("PACKAGE_DIGEST_INVALID")
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            fail("INVALID_PAGE")
        async with self.db.read(self.scope) as c:
            await require(c)
            rows = (
                (
                    await c.execute(
                        select(reviews)
                        .where(
                            reviews.c.scope == self.scope,
                            reviews.c.package_digest == checksum,
                            reviews.c.version > after,
                        )
                        .order_by(reviews.c.version)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "current": public(await latest(c, self.scope, checksum)),
                "items": [public(row) for row in rows[:limit]],
                "next_after": rows[limit - 1]["version"] if len(rows) > limit else None,
            }


async def require_lock(c, scope, lock):
    lock = verify(lock)
    await require(c)
    result = []
    for package in lock["packages"]:
        current = await latest(c, scope, package["digest"])
        if current is None or current["decision"] != "admitted":
            fail("PACKAGE_NOT_ADMITTED")
        statement(current["statement"], package["manifest"])
        await evidence(c, scope, current["statement"])
        result.append({"digest": package["digest"], "version": current["version"]})
    return result


async def check_work(c, run):
    captured = run["input"].get("package_admissions")
    if captured is None:
        if run["input"].get("revision_ref"):
            from .revision_schema import revisions

            value = (
                await c.execute(
                    select(revisions.c.manifest).where(
                        revisions.c.scope == run["scope"],
                        revisions.c.id == run["input"]["revision_ref"],
                    )
                )
            ).scalar_one_or_none()
            if value and "package_lock" in value["configuration"].get(
                "execution_profile", {}
            ):
                fail("PACKAGE_ADMISSION_REQUIRED")
        return
    await require(c)
    for binding in captured:
        current = await latest(c, run["scope"], binding["digest"])
        if (
            current is None
            or current["decision"] != "admitted"
            or current["version"] != binding["version"]
        ):
            raise RuntimeErrorCode("PACKAGE_ADMISSION_CHANGED")
