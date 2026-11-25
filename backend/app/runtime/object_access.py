"""Exact-version ACL repository; current principal validation belongs to the service.

ACL changes and decisions use the existing database serialization boundary.
An allow records a decision at that boundary, not a reservation of future bytes.
"""

import math
from uuid import uuid4

from sqlalchemy import select

from app.services.e2ag import Decision
from .database import RuntimeErrorCode
from .object_access_schema import decisions, policies, receipts, require
from .objects import Objects, validate_ref
from .store import bounded, digest, identifier

SUBJECT_FIELDS = ("issuer", "audience", "scope", "id", "subject", "actor")
ACTIONS = {"object.read", "object.download"}


def subject(binding):
    try:
        value = {key: binding[key] for key in SUBJECT_FIELDS}
        for item in value.values():
            identifier(item)
        return value
    except (KeyError, TypeError):
        raise RuntimeErrorCode("INVALID_OBJECT_SUBJECT") from None


class ObjectPolicies:
    def __init__(self, db, scope):
        self.db, self.scope = db, scope

    def where(self, ref):
        validate_ref(ref)
        return (
            policies.c.scope == self.scope,
            policies.c.kind == ref["kind"],
            policies.c.object_id == ref["id"],
            policies.c.object_version == ref["version"],
        )

    async def row(self, c, ref):
        return (
            (await c.execute(select(policies).where(*self.where(ref))))
            .mappings()
            .first()
        )

    async def show(self, ref):
        async with self.db.read(self.scope) as c:
            await require(c)
            await Objects(self.db, self.scope)._get(c, ref)
            row = await self.row(c, ref)
            return {
                "ref": ref,
                "revision": row["revision"] if row else 0,
                "grants": row["grants"] if row else [],
                "mode": "restricted" if row or ref["kind"] == "file" else "inherited",
            }

    async def change(self, ref, grants, expected_revision, key, actor):
        validate_ref(ref)
        identifier(key)
        identifier(actor)
        if type(expected_revision) is not int or expected_revision < 0:
            raise RuntimeErrorCode("INVALID_EXPECTED_VERSION")
        if not isinstance(grants, list) or len(grants) > 128:
            raise RuntimeErrorCode("INVALID_OBJECT_GRANTS")
        normalized = []
        seen = set()
        for grant in grants:
            if not isinstance(grant, dict) or set(grant) != {
                "principal",
                "actions",
                "expires_at",
            }:
                raise RuntimeErrorCode("INVALID_OBJECT_GRANTS")
            principal = subject(grant["principal"])
            if (
                set(grant["principal"]) != set(SUBJECT_FIELDS)
                or principal["scope"] != self.scope
            ):
                raise RuntimeErrorCode("INVALID_OBJECT_SUBJECT")
            actions = grant["actions"]
            expiry = grant["expires_at"]
            if (
                not isinstance(actions, list)
                or not actions
                or any(not isinstance(a, str) for a in actions)
                or not set(actions) <= ACTIONS
            ):
                raise RuntimeErrorCode("INVALID_OBJECT_ACTION")
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                raise RuntimeErrorCode("INVALID_OBJECT_EXPIRY")
            identity = digest(principal)
            if identity in seen:
                raise RuntimeErrorCode("INVALID_OBJECT_GRANTS")
            seen.add(identity)
            normalized.append(
                {
                    "principal": principal,
                    "actions": sorted(set(actions)),
                    "expires_at": expiry,
                }
            )
        normalized.sort(key=lambda g: digest(g["principal"]))
        body = {
            "ref": ref,
            "grants": normalized,
            "expected_revision": expected_revision,
            "actor": actor,
        }
        bounded(body, 65536)
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
            await Objects(self.db, self.scope)._get(c, ref)
            row = await self.row(c, ref)
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            now = await self.db.now(c)
            if any(g["expires_at"] <= now for g in normalized):
                raise RuntimeErrorCode("INVALID_OBJECT_EXPIRY")
            values = {
                "revision": revision + 1,
                "grants": normalized,
                "updated_at": now,
                "updated_by": actor,
            }
            if row:
                await c.execute(
                    policies.update().where(*self.where(ref)).values(**values)
                )
            else:
                await c.execute(
                    policies.insert().values(
                        scope=self.scope,
                        kind=ref["kind"],
                        object_id=ref["id"],
                        object_version=ref["version"],
                        **values,
                    )
                )
            result = {"ref": ref, "mode": "restricted", **values}
            await c.execute(
                receipts.insert().values(
                    scope=self.scope, key=key, digest=hashed, response=result
                )
            )
            return result

    async def check(self, ref, principal, action, *, audit=True):
        if action not in ACTIONS:
            raise RuntimeErrorCode("INVALID_OBJECT_ACTION")
        identity = subject(principal) if principal is not None else {"operator": True}
        async with self.db.transaction(self.scope) as c:
            await require(c)
            row = await self.row(c, ref)
            now = await self.db.now(c)
            revision = row["revision"] if row else 0
            if principal is None:
                allowed = True
                reason = "OBJECT_OPERATOR_ACCESS"
            elif row is None:
                allowed = ref["kind"] != "file"
                reason = "OBJECT_INHERITED_ACCESS" if allowed else "OBJECT_NOT_GRANTED"
            else:
                allowed = any(
                    g["principal"] == identity
                    and g["expires_at"] > now
                    and action in g["actions"]
                    for g in row["grants"]
                )
                reason = "OBJECT_ACCESS_GRANTED" if allowed else "OBJECT_NOT_GRANTED"
            decision = Decision(
                "object-access",
                "allow" if allowed else "deny",
                (reason,),
                evidence={
                    "object_ref": ref,
                    "policy_revision": revision,
                    "action": action,
                },
            )
            if audit or not allowed:
                await c.execute(
                    decisions.insert().values(
                        id=str(uuid4()),
                        scope=self.scope,
                        object_ref=ref,
                        principal_ref=identity,
                        action=action,
                        revision=revision,
                        decision=decision.as_dict(),
                        created_at=now,
                    )
                )
        if not allowed:
            raise RuntimeErrorCode("NOT_FOUND")
        return decision
