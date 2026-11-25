"""Declared compatibility is reviewed separately from live E2AG authority."""

import re

from sqlalchemy import select

from .compatibility_schema import endpoints, events, require
from .database import RuntimeErrorCode
from .store import bounded, digest, identifier

FRESH_SECONDS = 60


def a2a_snapshot(card, spec):
    """Retain public contract facts; unknown metadata is never a credential source."""
    raw = card.model_dump(mode="json", by_alias=True, exclude_none=True)
    contract = {
        key: raw.get(key)
        for key in (
            "protocolVersion",
            "version",
            "url",
            "preferredTransport",
            "additionalInterfaces",
            "capabilities",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
            "securitySchemes",
            "security",
            "supportsAuthenticatedExtendedCard",
        )
    }
    value = {
        "protocol": "a2a",
        "protocol_version": card.protocol_version,
        "reported_version": card.version[:160],
        "endpoint": spec.endpoint,
        "skill_id": spec.skill_id,
        "contract_digest": digest(contract),
        "capabilities": {
            "streaming": bool(card.capabilities.streaming),
            "push_notifications": bool(card.capabilities.push_notifications),
        },
        "implementation_visibility": "declared",
        "implementation_digest": None,
        "remote_implementation_rollback": "unsupported",
    }
    bounded(value, 8192)
    return value


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeErrorCode("INVALID_PROFILE_DIGEST")
    return value


class Compatibility:
    def __init__(self, db, scope):
        self.db, self.scope = db, scope

    async def _row(self, c, profile_digest):
        await require(c)
        return (
            (
                await c.execute(
                    select(endpoints).where(
                        endpoints.c.scope == self.scope,
                        endpoints.c.profile_digest == key(profile_digest),
                    )
                )
            )
            .mappings()
            .first()
        )

    async def _event(self, c, value, kind, actor, evidence=None):
        await c.execute(
            events.insert().values(
                scope=self.scope,
                profile_digest=value["profile_digest"],
                version=value["version"],
                type=kind,
                actor=actor,
                snapshot_digest=value["snapshot_digest"],
                snapshot=value["snapshot"],
                evidence=evidence,
                created_at=value["updated_at"],
            )
        )

    async def observe(self, profile_digest, snapshot, *, expected_version=None):
        key(profile_digest)
        bounded(snapshot, 8192)
        hashed = digest(snapshot)
        async with self.db.transaction(self.scope) as c:
            prior = await self._row(c, profile_digest)
            if (
                expected_version is not None
                and (prior["version"] if prior else 0) != expected_version
            ):
                raise RuntimeErrorCode("VERSION_CONFLICT")
            now = await self.db.now(c)
            changed = prior is None or prior["snapshot_digest"] != hashed
            value = (
                dict(prior)
                if prior
                else {
                    "scope": self.scope,
                    "profile_digest": profile_digest,
                    "version": 0,
                    "accepted_digest": None,
                    "state": "review_required",
                }
            )
            if changed:
                # Seeing A again after A -> B is a new transition, requiring review.
                value.update(accepted_digest=None, state="review_required")
            value.update(
                snapshot=snapshot,
                snapshot_digest=hashed,
                error=None,
                observed_at=now,
                updated_at=now,
            )
            if changed or (prior and prior["error"]):
                value["version"] += 1
            if prior:
                await c.execute(
                    endpoints.update()
                    .where(
                        endpoints.c.scope == self.scope,
                        endpoints.c.profile_digest == profile_digest,
                    )
                    .values(**value)
                )
            else:
                await c.execute(endpoints.insert().values(**value))
            if changed or (prior and prior["error"]):
                await self._event(
                    c,
                    value,
                    "declaration.changed" if changed else "observation.recovered",
                    "runtime-worker",
                )
            return value

    async def unavailable(self, profile_digest, error, *, expected_version=None):
        key(profile_digest)
        identifier(error)
        async with self.db.transaction(self.scope) as c:
            prior = await self._row(c, profile_digest)
            if (
                expected_version is not None
                and (prior["version"] if prior else 0) != expected_version
            ):
                return
            if not prior or prior["error"] == error:
                return
            value = dict(
                prior,
                error=error,
                version=prior["version"] + 1,
                updated_at=await self.db.now(c),
            )
            if error in {
                "A2A_PROFILE_MISMATCH",
                "A2A_SKILL_NOT_FOUND",
                "A2A_EXTENSION_UNSUPPORTED",
                "A2A_DECLARATION_INVALID",
            }:
                value.update(state="review_required", accepted_digest=None)
            await c.execute(
                endpoints.update()
                .where(
                    endpoints.c.scope == self.scope,
                    endpoints.c.profile_digest == profile_digest,
                )
                .values(**value)
            )
            await self._event(c, value, "observation.unavailable", "runtime-worker")

    async def accept(
        self, profile_digest, expected_version, snapshot_digest, actor, evidence
    ):
        key(snapshot_digest)
        identifier(actor)
        if (
            type(expected_version) is not int
            or not isinstance(evidence, str)
            or not 1 <= len(evidence.strip()) <= 1000
        ):
            raise RuntimeErrorCode("COMPATIBILITY_REVIEW_REQUIRED")
        async with self.db.transaction(self.scope) as c:
            prior = await self._row(c, profile_digest)
            if not prior:
                raise RuntimeErrorCode("NOT_FOUND")
            if (
                prior["version"] != expected_version
                or prior["snapshot_digest"] != snapshot_digest
            ):
                raise RuntimeErrorCode("VERSION_CONFLICT")
            now = await self.db.now(c)
            if prior["error"] or now - prior["observed_at"] > FRESH_SECONDS:
                raise RuntimeErrorCode("COMPATIBILITY_OBSERVATION_STALE")
            value = dict(
                prior,
                state="accepted",
                accepted_digest=snapshot_digest,
                version=prior["version"] + 1,
                updated_at=now,
            )
            await c.execute(
                endpoints.update()
                .where(
                    endpoints.c.scope == self.scope,
                    endpoints.c.profile_digest == profile_digest,
                )
                .values(**value)
            )
            await self._event(c, value, "compatibility.accepted", actor, evidence)
            return value

    async def require_accepted(
        self, c, profile_digest, snapshot_digest=None, *, fresh=True
    ):
        value = await self._row(c, profile_digest)
        if (
            not value
            or value["state"] != "accepted"
            or value["accepted_digest"] != value["snapshot_digest"]
        ):
            raise RuntimeErrorCode("COMPATIBILITY_REVIEW_REQUIRED")
        if snapshot_digest is not None and value["snapshot_digest"] != snapshot_digest:
            raise RuntimeErrorCode("COMPATIBILITY_DECLARATION_CHANGED")
        if value["error"] or (
            fresh and await self.db.now(c) - value["observed_at"] > FRESH_SECONDS
        ):
            raise RuntimeErrorCode("COMPATIBILITY_OBSERVATION_STALE")
        return value

    async def show(self, profile_digest):
        async with self.db.read(self.scope) as c:
            row = await self._row(c, profile_digest)
            if not row:
                raise RuntimeErrorCode("NOT_FOUND")
            history = (
                (
                    await c.execute(
                        select(events)
                        .where(
                            events.c.scope == self.scope,
                            events.c.profile_digest == profile_digest,
                        )
                        .order_by(events.c.version.desc())
                        .limit(30)
                    )
                )
                .mappings()
                .all()
            )
            return dict(
                row,
                events=[dict(e) for e in history],
                observed_stale=bool(row["error"])
                or await self.db.now(c) - row["observed_at"] > FRESH_SECONDS,
            )

    async def discover(self, transport, spec):
        from pydantic import ValidationError

        from .a2a_faults import code

        async with self.db.read(self.scope) as c:
            current = await self._row(c, spec.fingerprint())
            version = current["version"] if current else 0
        try:
            card = await transport.discover()
        except Exception as exc:
            await self.unavailable(
                spec.fingerprint(),
                "A2A_DECLARATION_INVALID"
                if isinstance(exc, (ValidationError, ValueError))
                and not isinstance(exc, RuntimeErrorCode)
                else code(exc),
                expected_version=version,
            )
            raise
        return await self.observe(
            spec.fingerprint(), a2a_snapshot(card, spec), expected_version=version
        )
