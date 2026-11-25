"""Exact-version retention and invalidation; bytes remain until safe collection."""

import math
from sqlalchemy import select
from .database import RuntimeErrorCode
from .object_lifetime_schema import states, receipts, require
from .object_schema import links
from .objects import Objects, validate_ref
from .store import digest, identifier


def ref_key(ref):
    return (ref["kind"], ref["id"], ref["version"])


class ObjectLifetime:
    def __init__(self, db, scope):
        self.db, self.scope = db, scope

    def where(self, ref):
        validate_ref(ref)
        return (
            states.c.scope == self.scope,
            states.c.kind == ref["kind"],
            states.c.object_id == ref["id"],
            states.c.object_version == ref["version"],
        )

    async def row(self, c, ref):
        return (
            (await c.execute(select(states).where(*self.where(ref)))).mappings().first()
        )

    @staticmethod
    def direct(row, now):
        if row and row["deleted_at"] is not None:
            return "deleted"
        if row and row["expires_at"] is not None and row["expires_at"] <= now:
            return "expired"
        return "active"

    async def effective(self, c, ref, *, forbidden=None):
        """Inspect only explicit derived_from ancestry, bounded and cycle safe."""
        await require(c)
        validate_ref(ref)
        now = await self.db.now(c)
        pending = [(ref, ())]
        seen = set()
        while pending:
            current, path = pending.pop()
            key = ref_key(current)
            if key in path or forbidden == key:
                raise RuntimeErrorCode("OBJECT_DERIVATION_CYCLE")
            if key in seen:
                continue
            seen.add(key)
            if len(path) > 64 or len(seen) > 4096:
                raise RuntimeErrorCode("OBJECT_DERIVATION_LIMIT")
            state = self.direct(await self.row(c, current), now)
            if state != "active":
                return {
                    "state": state if current == ref else "source_unavailable",
                    "cause": current,
                    "cause_state": state,
                }
            targets = (
                (
                    await c.execute(
                        select(links.c.target)
                        .where(
                            links.c.scope == self.scope,
                            links.c.source_kind == current["kind"],
                            links.c.source_id == current["id"],
                            links.c.source_version == current["version"],
                            links.c.relation == "derived_from",
                        )
                        .limit(4097)
                    )
                )
                .scalars()
                .all()
            )
            if len(targets) > 4096:
                raise RuntimeErrorCode("OBJECT_DERIVATION_LIMIT")
            pending.extend(
                (target, path + (key,)) for target in targets if "kind" in target
            )
        return {"state": "active", "cause": None, "cause_state": None}

    async def available(self, c, ref):
        value = await self.effective(c, ref)
        if value["state"] != "active":
            raise RuntimeErrorCode("NOT_FOUND")
        return value

    async def show(self, ref):
        async with self.db.read(self.scope) as c:
            await require(c)
            value = await Objects(self.db, self.scope)._get(c, ref)
            row = await self.row(c, ref)
            return {
                "ref": ref,
                "revision": row["revision"] if row else 0,
                "retain_until": row["retain_until"] if row else 0,
                "expires_at": row["expires_at"] if row else None,
                "deleted_at": row["deleted_at"] if row else None,
                "reason": row["reason"] if row else None,
                "descriptor": value["descriptor"],
                "binding": value["binding"],
                **await self.effective(c, ref),
                "content_collection": "retained",
            }

    async def change(
        self,
        ref,
        *,
        expected_revision,
        key,
        actor,
        retain_until=0,
        expires_at=None,
        delete=False,
        reason="operator",
    ):
        validate_ref(ref)
        identifier(key)
        identifier(actor)
        identifier(reason)
        if type(expected_revision) is not int or expected_revision < 0:
            raise RuntimeErrorCode("INVALID_EXPECTED_VERSION")
        for number in [retain_until] + ([] if expires_at is None else [expires_at]):
            if (
                type(number) not in (int, float)
                or not math.isfinite(number)
                or number < 0
            ):
                raise RuntimeErrorCode("INVALID_OBJECT_RETENTION")
        body = {
            "ref": ref,
            "expected_revision": expected_revision,
            "actor": actor,
            "retain_until": retain_until,
            "expires_at": expires_at,
            "delete": delete,
            "reason": reason,
        }
        hashed = digest(body)
        async with self.db.transaction(self.scope) as c:
            await require(c)
            old = (
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
            if old:
                if old["digest"] != hashed:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return old["response"]
            value = await Objects(self.db, self.scope)._get(c, ref)
            row = await self.row(c, ref)
            now = await self.db.now(c)
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            if self.direct(row, now) != "active":
                raise RuntimeErrorCode("OBJECT_ALREADY_UNAVAILABLE")
            minimum = row["retain_until"] if row else 0
            if delete:
                if minimum > now:
                    raise RuntimeErrorCode("OBJECT_RETENTION_ACTIVE")
                # A running task may still publish/reconcile its session and artifacts.
                if value["binding"]:
                    from .schema import runs

                    state = (
                        await c.execute(
                            select(runs.c.state).where(
                                runs.c.id == value["binding"]["run_id"]
                            )
                        )
                    ).scalar_one()
                    if state not in {"succeeded", "failed", "cancelled"}:
                        raise RuntimeErrorCode("OBJECT_RUN_ACTIVE")
                values = {
                    "retain_until": minimum,
                    "expires_at": row["expires_at"] if row else None,
                    "deleted_at": now,
                    "reason": reason,
                }
            else:
                if retain_until < minimum:
                    raise RuntimeErrorCode("OBJECT_RETENTION_SHORTENED")
                if expires_at is not None and (
                    expires_at <= now or expires_at < retain_until
                ):
                    raise RuntimeErrorCode("INVALID_OBJECT_EXPIRY")
                values = {
                    "retain_until": retain_until,
                    "expires_at": expires_at,
                    "deleted_at": None,
                    "reason": None,
                }
            values.update(revision=revision + 1, updated_at=now, updated_by=actor)
            if row:
                await c.execute(
                    states.update().where(*self.where(ref)).values(**values)
                )
            else:
                await c.execute(
                    states.insert().values(
                        scope=self.scope,
                        kind=ref["kind"],
                        object_id=ref["id"],
                        object_version=ref["version"],
                        **values,
                    )
                )
            result = {
                "ref": ref,
                **values,
                "state": self.direct(values, now),
                "content_collection": "retained",
            }
            await c.execute(
                receipts.insert().values(
                    scope=self.scope, key=key, digest=hashed, response=result
                )
            )
            return result
