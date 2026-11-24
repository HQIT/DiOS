"""Durable Profile/Run timers with database-fenced service-policy admission."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import and_, or_, select

from app.runtime.database import RuntimeErrorCode
from app.runtime.principals import admit
from app.runtime.schedule_schema import events, occurrences, require, schedules
from app.runtime.schema import runs
from app.runtime.store import TERMINAL, bounded, digest, identifier
from app.services.durable_cron import next_due, validate_expression


def next_stamp(expression, stamp):
    return next_due(expression, datetime.fromtimestamp(stamp, UTC)).timestamp()


class RunSchedules:
    def __init__(self, console):
        if console.principal is not None:
            raise RuntimeErrorCode("AUTH_ACTION_DENIED")
        self.console, self.db, self.scope = console, console.db, console.scope
        self.owner = uuid4().hex

    async def binding(self, template):
        prepared = await self.console.prepare_submission(**template)
        if self.console.policy is None:
            raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
        descriptor = await asyncio.to_thread(
            admit,
            self.console.policy,
            template["profile_id"],
            prepared["profile"]["profile_digest"],
            prepared["registered"]["refs"],
        )
        return self.console.schedule_binding(prepared, descriptor)

    async def _row(self, c, identity):
        await require(c)
        row = (
            (
                await c.execute(
                    select(schedules).where(
                        schedules.c.id == identity, schedules.c.scope == self.scope
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeErrorCode("NOT_FOUND")
        return dict(row)

    async def _audit(self, c, identity, kind, detail, now):
        bounded(detail)
        await c.execute(
            events.insert().values(
                id=uuid4().hex,
                scope=self.scope,
                schedule_id=identity,
                kind=kind,
                detail=detail,
                digest=digest(detail),
                created_at=now,
            )
        )

    async def create(self, template, expression, key):
        identifier(key)
        if (
            not isinstance(template, dict)
            or set(template) != {"profile_id", "sample_id", "input_payload", "goal"}
            or not isinstance(template["goal"], str)
            or not 1 <= len(template["goal"]) <= 2000
        ):
            raise RuntimeErrorCode("INVALID_SCHEDULE_TEMPLATE")
        bounded(template)
        try:
            expression = validate_expression(expression)
            if not expression:
                raise ValueError()
        except (ValueError, AttributeError):
            raise RuntimeErrorCode("INVALID_CRON_EXPRESSION") from None
        request_digest = digest([template, expression])
        # An accepted creation receipt is independent of later policy changes.
        async with self.db.read(self.scope) as c:
            await require(c)
            existing = (
                (
                    await c.execute(
                        select(schedules).where(
                            schedules.c.scope == self.scope, schedules.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_digest"] != request_digest:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return await self._view(c, dict(existing))
        binding = await self.binding(template)
        async with self.db.transaction(self.scope) as c:
            await require(c)
            existing = (
                (
                    await c.execute(
                        select(schedules).where(
                            schedules.c.scope == self.scope, schedules.c.key == key
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_digest"] != request_digest:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return await self._view(c, dict(existing))
            now = await self.db.now(c)
            row = {
                "id": uuid4().hex,
                "scope": self.scope,
                "key": key,
                "request_digest": request_digest,
                "template": template,
                "binding": binding,
                "expression": expression,
                "enabled": True,
                "version": 1,
                "next_due": next_stamp(expression, now),
                "active_id": None,
                "buffered_id": None,
                "lease_owner": None,
                "lease_epoch": 0,
                "lease_until": 0,
                "created_at": now,
                "updated_at": now,
            }
            await c.execute(schedules.insert().values(**row))
            await self._audit(
                c,
                row["id"],
                "schedule.created",
                {"request_digest": request_digest, "binding": binding},
                now,
            )
            return await self._view(c, row)

    async def _item(self, c, identity):
        if not identity:
            return None
        row = (
            (
                await c.execute(
                    select(occurrences).where(
                        occurrences.c.id == identity, occurrences.c.scope == self.scope
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeErrorCode("SCHEDULE_OCCURRENCE_MISSING")
        return dict(row)

    @staticmethod
    def item_view(row):
        return {
            k: row[k]
            for k in (
                "id",
                "schedule_id",
                "revision",
                "first_due",
                "last_due",
                "trigger_count",
                "state",
                "run_id",
                "failures",
                "retry_at",
                "error",
                "created_at",
                "updated_at",
            )
        }

    async def _view(self, c, row):
        active, buffered = (
            await self._item(c, row["active_id"]),
            await self._item(c, row["buffered_id"]),
        )
        return {
            k: row[k]
            for k in (
                "id",
                "template",
                "binding",
                "expression",
                "enabled",
                "version",
                "next_due",
                "created_at",
                "updated_at",
            )
        } | {
            "timezone": "UTC",
            "overlap": "buffer_one",
            "catchup_seconds": 86400,
            "active": self.item_view(active) if active else None,
            "buffered": self.item_view(buffered) if buffered else None,
        }

    async def list(self, limit=50, before=None):
        if not 1 <= limit <= 100:
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await require(c)
            query = select(schedules).where(schedules.c.scope == self.scope)
            if before:
                cursor = await self._row(c, before)
                query = query.where(
                    or_(
                        schedules.c.created_at < cursor["created_at"],
                        and_(
                            schedules.c.created_at == cursor["created_at"],
                            schedules.c.id < before,
                        ),
                    )
                )
            rows = (
                (
                    await c.execute(
                        query.order_by(
                            schedules.c.created_at.desc(), schedules.c.id.desc()
                        ).limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "items": [await self._view(c, dict(r)) for r in rows],
                "next_cursor": rows[-1]["id"] if len(rows) == limit else None,
                "observed_at": await self.db.now(c),
            }

    async def history(self, identity, limit=50, before=None):
        if not 1 <= limit <= 100:
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await self._row(c, identity)
            query = select(occurrences).where(
                occurrences.c.scope == self.scope, occurrences.c.schedule_id == identity
            )
            if before:
                cursor = await self._item(c, before)
                if cursor["schedule_id"] != identity:
                    raise RuntimeErrorCode("INVALID_CURSOR")
                query = query.where(
                    or_(
                        occurrences.c.first_due < cursor["first_due"],
                        and_(
                            occurrences.c.first_due == cursor["first_due"],
                            occurrences.c.id < before,
                        ),
                    )
                )
            rows = (
                (
                    await c.execute(
                        query.order_by(
                            occurrences.c.first_due.desc(), occurrences.c.id.desc()
                        ).limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            return {
                "items": [self.item_view(r) for r in rows],
                "next_cursor": rows[-1]["id"] if len(rows) == limit else None,
            }

    async def control(self, identity, version, enabled, actor, reason):
        identifier(actor)
        if (
            type(enabled) is not bool
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 2000
        ):
            raise RuntimeErrorCode("INVALID_SCHEDULE_CONTROL")
        async with self.db.transaction(self.scope) as c:
            row = await self._row(c, identity)
            if row["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            now = await self.db.now(c)
            changes = {"enabled": enabled, "version": version + 1, "updated_at": now}
            if enabled and not row["enabled"]:
                changes["next_due"] = next_stamp(row["expression"], now)
            await c.execute(
                schedules.update().where(schedules.c.id == identity).values(**changes)
            )
            await self._audit(
                c,
                identity,
                "schedule.resumed" if enabled else "schedule.paused",
                {"actor": actor, "reason": reason, "version": version + 1},
                now,
            )
            return await self._view(c, row | changes)

    async def _receipt(self, c, item):
        row = (
            (
                await c.execute(
                    select(runs).where(
                        runs.c.scope == self.scope,
                        runs.c.idempotency_key == item["key"],
                    )
                )
            )
            .mappings()
            .first()
        )
        if row:
            if (
                row["input"].get("submission_digest")
                != item["binding"]["submission_digest"]
                or row["input"].get("principal") is not None
            ):
                raise RuntimeErrorCode("SCHEDULE_RECEIPT_MISMATCH")
            return dict(row)
        return None

    async def reauthorize(self, identity, version, actor, reason):
        identifier(actor)
        if not isinstance(reason, str) or not 1 <= len(reason) <= 2000:
            raise RuntimeErrorCode("INVALID_SCHEDULE_CONTROL")
        async with self.db.read(self.scope) as c:
            original = await self._row(c, identity)
        binding = await self.binding(original["template"])
        async with self.db.transaction(self.scope) as c:
            row = await self._row(c, identity)
            now = await self.db.now(c)
            if row["version"] != version:
                raise RuntimeErrorCode("VERSION_CONFLICT")
            active = row["active_id"]
            for item_id in (row["active_id"], row["buffered_id"]):
                if not item_id:
                    continue
                item = await self._item(c, item_id)
                receipt = await self._receipt(c, item)
                if receipt:
                    await self._observe(c, item, now)
                    continue
                await c.execute(
                    occurrences.update()
                    .where(occurrences.c.id == item_id)
                    .values(
                        state="discarded",
                        error="SCHEDULE_REAUTHORIZED",
                        updated_at=now,
                        retry_at=None,
                    )
                )
                if item_id == active:
                    active = None
            changes = {
                "binding": binding,
                "version": version + 1,
                "active_id": active,
                "buffered_id": None,
                "next_due": next_stamp(row["expression"], now),
                "updated_at": now,
            }
            await c.execute(
                schedules.update().where(schedules.c.id == identity).values(**changes)
            )
            await self._audit(
                c,
                identity,
                "schedule.reauthorized",
                {
                    "actor": actor,
                    "reason": reason,
                    "previous_binding": row["binding"],
                    "binding": binding,
                    "version": version + 1,
                },
                now,
            )
            return await self._view(c, row | changes)

    async def resolve(self, identity, item_id, action, actor, reason, key):
        for value in (actor, key):
            identifier(value)
        if (
            action not in {"retry", "discard"}
            or not isinstance(reason, str)
            or not 1 <= len(reason) <= 2000
        ):
            raise RuntimeErrorCode("INVALID_RESOLUTION")
        detail = {
            "occurrence_id": item_id,
            "action": action,
            "actor": actor,
            "reason": reason,
            "idempotency_key": key,
        }
        async with self.db.transaction(self.scope) as c:
            row = await self._row(c, identity)
            now = await self.db.now(c)
            previous = (
                (
                    await c.execute(
                        select(events).where(
                            events.c.scope == self.scope,
                            events.c.schedule_id == identity,
                            events.c.kind == "schedule.resolved",
                            events.c.detail["idempotency_key"].as_string() == key,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if previous["digest"] != digest(detail):
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return await self._view(c, row)
            item = await self._item(c, item_id)
            if item["schedule_id"] != identity or row["active_id"] != item_id:
                raise RuntimeErrorCode("NOT_FOUND")
            item = await self._observe(c, item, now)
            if item["state"] != "dead_letter":
                raise RuntimeErrorCode("SCHEDULE_NOT_DEAD_LETTER")
            if action == "retry" and item["run_id"]:
                raise RuntimeErrorCode("SCHEDULE_ORIGINAL_RUN_REQUIRES_RECONCILIATION")
            await c.execute(
                occurrences.update()
                .where(occurrences.c.id == item_id)
                .values(
                    state="pending" if action == "retry" else "discarded",
                    failures=0,
                    retry_at=None,
                    updated_at=now,
                )
            )
            await self._audit(c, identity, "schedule.resolved", detail, now)
            return await self._view(c, row)

    async def _observe(self, c, item, now):
        if item["state"] == "discarded":
            return item
        run = await self._receipt(c, item)
        if run:
            state = (
                "succeeded"
                if run["state"] == "succeeded"
                else "dead_letter"
                if run["state"] in TERMINAL
                else "running"
            )
            changes = {
                "state": state,
                "run_id": run["id"],
                "retry_at": None,
                "updated_at": now,
                "error": "ORIGINAL_RUN_" + run["state"].upper()
                if state == "dead_letter"
                else "",
            }
            if item["run_id"] is None:
                await self._audit(
                    c,
                    item["schedule_id"],
                    "schedule.run_linked",
                    {
                        "occurrence_id": item["id"],
                        "run_id": run["id"],
                        "key": item["key"],
                    },
                    now,
                )
            await c.execute(
                occurrences.update()
                .where(occurrences.c.id == item["id"])
                .values(**changes)
            )
            item.update(changes)
        return item

    async def claim(self, identity):
        async with self.db.transaction(self.scope) as c:
            row = await self._row(c, identity)
            now = await self.db.now(c)
            if row["lease_until"] > now:
                return None
            claim = {
                "lease_owner": self.owner,
                "lease_epoch": row["lease_epoch"] + 1,
                "lease_until": now + 30,
                "updated_at": now,
            }
            await c.execute(
                schedules.update().where(schedules.c.id == identity).values(**claim)
            )
            return row | claim

    async def _fence(self, c, claim, *, dispatch=False):
        row = await self._row(c, claim["id"])
        now = await self.db.now(c)
        if (
            row["lease_owner"] != self.owner
            or row["lease_epoch"] != claim["lease_epoch"]
            or row["lease_until"] <= now
        ):
            raise RuntimeErrorCode("SCHEDULE_LEASE_LOST")
        if dispatch and (not row["enabled"] or row["version"] != claim["version"]):
            raise RuntimeErrorCode("SCHEDULE_PAUSED")
        return row, now

    async def _new(self, c, row, first, last, count, state, now):
        identity = uuid4().hex
        item = {
            "id": identity,
            "scope": self.scope,
            "schedule_id": row["id"],
            "revision": row["version"],
            "first_due": first,
            "last_due": last,
            "trigger_count": count,
            "key": "scheduled-" + digest([self.scope, row["id"], identity]),
            "template": row["template"],
            "binding": row["binding"],
            "state": state,
            "run_id": None,
            "failures": 0,
            "retry_at": None,
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        await c.execute(occurrences.insert().values(**item))
        await self._audit(
            c,
            row["id"],
            "schedule." + state,
            {
                "occurrence_id": identity,
                "first_due": first,
                "last_due": last,
                "trigger_count": count,
            },
            now,
        )
        return item

    async def plan(self, claim):
        async with self.db.transaction(self.scope) as c:
            row, now = await self._fence(c, claim)
            if row["active_id"]:
                item = await self._item(c, row["active_id"])
                if item["state"] != "discarded":
                    item = await self._observe(c, item, now)
                if item["state"] in {"succeeded", "discarded"}:
                    row["active_id"] = None
            if row["enabled"] and row["next_due"] <= now:
                due = row["next_due"]
                cutoff = now - 86400
                if due < cutoff:
                    await self._new(c, row, due, cutoff, None, "expired", now)
                    due = next_stamp(row["expression"], cutoff)
                count, first, last = 0, due, due
                while due <= now:
                    count, last = count + 1, due
                    due = next_stamp(row["expression"], due)
                if count:
                    if row["buffered_id"]:
                        buffered = await self._item(c, row["buffered_id"])
                        await c.execute(
                            occurrences.update()
                            .where(occurrences.c.id == buffered["id"])
                            .values(
                                last_due=last,
                                trigger_count=buffered["trigger_count"] + count,
                                updated_at=now,
                            )
                        )
                    else:
                        buffered = await self._new(
                            c, row, first, last, count, "buffered", now
                        )
                        row["buffered_id"] = buffered["id"]
                row["next_due"] = due
            if row["enabled"] and not row["active_id"] and row["buffered_id"]:
                row["active_id"], row["buffered_id"] = row["buffered_id"], None
                await c.execute(
                    occurrences.update()
                    .where(occurrences.c.id == row["active_id"])
                    .values(state="pending")
                )
            await c.execute(
                schedules.update()
                .where(schedules.c.id == row["id"])
                .values(
                    active_id=row["active_id"],
                    buffered_id=row["buffered_id"],
                    next_due=row["next_due"],
                    updated_at=now,
                )
            )
            return await self._item(c, row["active_id"])

    async def deliver(self, claim, item):
        if not item or item["state"] != "pending":
            return
        async with self.db.read(self.scope) as c:
            now = await self.db.now(c)
            from app.runtime.lifecycle import admission

            row = await self._row(c, claim["id"])
            if (
                not row["enabled"]
                or row["version"] != claim["version"]
                or not await admission(c, self.scope)
            ):
                return
        if item["retry_at"] and item["retry_at"] > now:
            return

        async def guard(c):
            row, _ = await self._fence(c, claim, dispatch=True)
            current = await self._item(c, item["id"])
            if (
                row["active_id"] != item["id"]
                or current["state"] != "pending"
                or row["binding"] != item["binding"]
            ):
                raise RuntimeErrorCode("SCHEDULE_OCCURRENCE_CHANGED")

        try:
            await self.console.submit(
                **item["template"],
                key=item["key"],
                expected_schedule_binding=item["binding"],
                admission_guard=guard,
            )
            async with self.db.transaction(self.scope) as c:
                _, now = await self._fence(c, claim)
                await self._observe(c, await self._item(c, item["id"]), now)
        except RuntimeErrorCode as exc:
            if exc.code in {
                "SCHEDULE_LEASE_LOST",
                "SCHEDULE_PAUSED",
                "RUNTIME_DRAINING",
                "SCHEDULE_OCCURRENCE_CHANGED",
            }:
                return
            await self.failure(claim, item, exc.code)
        except (OSError, ValueError):
            await self.failure(claim, item, "SCHEDULE_SOURCE_UNAVAILABLE")

    async def failure(self, claim, item, code):
        async with self.db.transaction(self.scope) as c:
            row, now = await self._fence(c, claim)
            if row["version"] != claim["version"] or row["active_id"] != item["id"]:
                return
            current = await self._observe(c, await self._item(c, item["id"]), now)
            if current["run_id"] or current["state"] != "pending":
                return
            count = current["failures"] + 1
            transient = code in {
                "SCHEDULE_SOURCE_UNAVAILABLE",
                "AUTH_PROVIDER_UNAVAILABLE",
                "WRITE_QUEUE_FULL",
                "WRITE_QUEUE_TIMEOUT",
            }
            state = "pending" if transient and count < 5 else "dead_letter"
            await c.execute(
                occurrences.update()
                .where(occurrences.c.id == item["id"])
                .values(
                    failures=count,
                    state=state,
                    error=code,
                    retry_at=now + min(900, 30 * 2**count)
                    if state == "pending"
                    else None,
                    updated_at=now,
                )
            )
            await self._audit(
                c,
                claim["id"],
                "schedule.delivery_failed",
                {
                    "occurrence_id": item["id"],
                    "error": code,
                    "state": state,
                    "failures": count,
                },
                now,
            )

    async def tick(self, limit=20):
        if not 1 <= limit <= 100:
            raise RuntimeErrorCode("INVALID_LIMIT")
        async with self.db.read(self.scope) as c:
            await require(c)
            ids = (
                (
                    await c.execute(
                        select(schedules.c.id)
                        .where(
                            schedules.c.scope == self.scope,
                            or_(
                                schedules.c.enabled == True,
                                schedules.c.active_id.is_not(None),
                            ),
                        )
                        .order_by(schedules.c.updated_at, schedules.c.id)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        count = 0
        for identity in ids:
            claim = await self.claim(identity)
            if claim is None:
                continue
            try:
                item = await self.plan(claim)
                await self.deliver(claim, item)
                count += 1
            finally:
                async with self.db.transaction(self.scope) as c:
                    await c.execute(
                        schedules.update()
                        .where(
                            schedules.c.id == identity,
                            schedules.c.lease_owner == self.owner,
                            schedules.c.lease_epoch == claim["lease_epoch"],
                        )
                        .values(
                            lease_owner=None,
                            lease_until=0,
                            updated_at=await self.db.now(c),
                        )
                    )
        return {"schedules_observed": count}
