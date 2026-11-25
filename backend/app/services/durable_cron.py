"""Durable UTC timers: transactional outbox, BUFFER_ONE and original-task recovery."""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from croniter import croniter
from sqlalchemy import select

from app.config import settings
from app.models.tables import (
    A2ATask,
    CronCursor,
    CronOccurrence,
    EventLog,
    Subscription,
)
from app.runtime.database import RuntimeErrorCode
from app.services import managed_runtime
from app.services.event_normalizer import _make_event

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 30
CATCHUP_SECONDS = 86400
FINISHED = {"succeeded", "denied", "discarded", "cancelled", "expired"}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def validate_expression(expression):
    """Five-field UTC cron, bounded calendar search, no random/hash expressions."""
    expression = expression.strip()
    if not expression:
        return ""
    if len(expression) > 64 or len(expression.split()) != 5 or "@" in expression:
        raise ValueError("INVALID_CRON_EXPRESSION")
    try:
        croniter(expression, datetime.now(UTC), max_years_between_matches=8).get_next(
            datetime
        )
    except (ValueError, KeyError, OverflowError) as exc:
        raise ValueError("INVALID_CRON_EXPRESSION") from exc
    # Randomized and hashed expressions cannot identify a stable scheduled slot.
    if re.search(r"\b[hr]\b", expression, re.IGNORECASE):
        raise ValueError("INVALID_CRON_EXPRESSION")
    return expression


def next_due(expression, base):
    return croniter(expression, utc(base), max_years_between_matches=8).get_next(
        datetime
    )


def configuration(sub):
    return {
        "enabled": sub.enabled,
        "expression": sub.cron_expression.strip(),
        "agent_id": sub.agent_id,
    }


def ownership():
    return managed_runtime.serialized("cron:" + settings.runtime_scope, timeout=0)


def occurrence(cursor, first, last, state, count):
    return CronOccurrence(
        id=uuid4().hex,
        scope=cursor.scope,
        subscription_id=cursor.subscription_id,
        revision=cursor.revision,
        first_due=first,
        last_due=last,
        trigger_count=count,
        event_id=uuid4().hex[:12],
        event={},
        state=state,
    )


async def synchronize(db, sub, now):
    """Called under timer ownership, including subscription mutations."""
    cursor = await db.get(CronCursor, sub.id)
    config = configuration(sub)
    if cursor and cursor.scope != settings.runtime_scope:
        raise RuntimeErrorCode("SCHEDULE_SCOPE_MISMATCH")
    if cursor is None:
        cursor = CronCursor(
            subscription_id=sub.id,
            scope=settings.runtime_scope,
            revision=uuid4().hex,
            configuration={},
            next_due=None,
        )
        db.add(cursor)
    if cursor.configuration != config:
        if cursor.active_id:
            active = await db.get(CronOccurrence, cursor.active_id)
            if active.state == "pending" and not await db.get(
                EventLog, active.event_id
            ):
                active.state, active.error = "cancelled", "SCHEDULE_CHANGED"
                cursor.active_id = None
        if cursor.buffered_id:
            buffered = await db.get(CronOccurrence, cursor.buffered_id)
            buffered.state, buffered.error = "cancelled", "SCHEDULE_CHANGED"
            cursor.buffered_id = None
        cursor.configuration, cursor.revision = config, uuid4().hex
        cursor.next_due, cursor.error = None, ""
        try:
            expr = validate_expression(config.get("expression", ""))
            if config["enabled"] and expr:
                cursor.next_due = next_due(expr, now)
        except ValueError:
            cursor.error = "INVALID_CRON_EXPRESSION"
        cursor.updated_at = now
    return cursor


async def refresh(db, item):
    log = await db.get(EventLog, item.event_id)
    if not log:
        return
    tasks = list(
        (await db.scalars(select(A2ATask).where(A2ATask.context_id == log.id))).all()
    )
    if item.state == "dead_letter" and not tasks:
        return
    # A task's existence is evidence of dispatch. Observation timeouts never
    # authorize a replacement or clear the overlap barrier.
    if any(
        t.status not in {"completed", "failed", "canceled", "rejected"} for t in tasks
    ):
        item.state = "running"
    elif tasks:
        item.state = (
            "succeeded"
            if all(t.status == "completed" for t in tasks)
            else "dead_letter"
        )
        if item.state == "dead_letter":
            item.error = "ORIGINAL_TASK_FAILED"
    elif log.status in {"denied", "approval_rejected", "approval_expired"}:
        item.state = "denied"
    elif log.status == "approval_required":
        from app.models.tables import E2AGApproval

        approval = await db.scalar(
            select(E2AGApproval.id).where(E2AGApproval.event_log_id == log.id)
        )
        item.state = "approval_required" if approval else "pending"
    elif log.status == "approval_approved":
        item.state = "pending"
    elif log.status in {"failed", "dead_letter"}:
        item.state, item.error = "dead_letter", "EVENT_DISPATCH_FAILED"
    elif log.status == "dispatched":
        item.state, item.error = "dead_letter", "TASK_EVIDENCE_MISSING"
    if item.state != "pending":
        item.next_retry_at = None


async def plan(db, cursor, now):
    if cursor.active_id:
        active = await db.get(CronOccurrence, cursor.active_id)
        if active.state not in FINISHED:
            await refresh(db, active)
        if active.state in FINISHED:
            cursor.active_id = None
    if cursor.next_due and utc(cursor.next_due) <= now:
        expr, due = cursor.configuration["expression"], utc(cursor.next_due)
        cutoff = now - timedelta(seconds=CATCHUP_SECONDS)
        if due < cutoff:
            db.add(occurrence(cursor, due, cutoff, "expired", None))
            due = next_due(expr, cutoff)
        count, first, last = 0, due, due
        while due <= now:
            count, last = count + 1, due
            due = next_due(expr, due)
        if count:
            buffered = (
                await db.get(CronOccurrence, cursor.buffered_id)
                if cursor.buffered_id
                else None
            )
            if buffered:
                buffered.last_due, buffered.trigger_count = (
                    last,
                    buffered.trigger_count + count,
                )
            else:
                buffered = occurrence(cursor, first, last, "buffered", count)
                db.add(buffered)
                cursor.buffered_id = buffered.id
        cursor.next_due = due
    if not cursor.active_id and cursor.buffered_id:
        await db.flush()
        item = await db.get(CronOccurrence, cursor.buffered_id)
        item.state = "pending"
        cursor.active_id, cursor.buffered_id = item.id, None
        event = _make_event(
            source="cron/" + cursor.configuration["agent_id"],
            event_type="cron.tick",
            subject=cursor.configuration["expression"],
            data={
                "agent_id": cursor.configuration["agent_id"],
                "subscription_id": cursor.subscription_id,
                "occurrence_id": item.id,
                "scheduled_at": utc(item.first_due).isoformat(),
                "latest_scheduled_at": utc(item.last_due).isoformat(),
                "trigger_count": item.trigger_count,
            },
        )
        event["id"] = item.event_id
        item.event = event
    cursor.updated_at = now
    # Watermark, coalescing and outbox become durable in one transaction.
    await db.commit()


async def deliver(db, cursor, now):
    if not cursor.active_id:
        return
    item = await db.get(CronOccurrence, cursor.active_id)
    if item.state != "pending" or (
        item.next_retry_at and utc(item.next_retry_at) > now
    ):
        return
    from app.services.event_dispatcher import dispatch_event

    identity = item.id
    try:
        await dispatch_event(
            item.event,
            [item.event["data"]["agent_id"]],
            db,
            scheduled_log_id=item.event_id,
        )
        await db.refresh(item)
        await refresh(db, item)
        await db.commit()
    except RuntimeErrorCode as exc:
        if exc.code in {"RUNTIME_DRAINING", "RUNTIME_OPERATION_IN_PROGRESS"}:
            await db.rollback()
            return
        raise
    except Exception as exc:  # noqa: BLE001 - persist failure without replaying an unknown delivery
        # Inspect any original receipt next time. Preserve partial output.
        await db.rollback()
        item = await db.get(CronOccurrence, identity)
        item.attempts += 1
        item.error = "DELIVERY_" + type(exc).__name__[:80]
        if item.attempts >= 3:
            await refresh(db, item)
            if item.state == "pending":
                item.state = "dead_letter"
        item.next_retry_at = (
            now + timedelta(seconds=60 * 2 ** min(item.attempts, 6))
            if item.state == "pending"
            else None
        )
        await db.commit()


class CronScheduler:
    def __init__(self):
        self._task = None
        self.observer = None

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self):
        try:
            while True:
                try:
                    if self.observer:
                        await self.observer.cycle(self._tick)
                    else:
                        await self._tick()
                except Exception:
                    logger.exception("Durable timer tick failed")
                if self.observer:
                    await self.observer.idle(CHECK_INTERVAL)
                else:
                    await asyncio.sleep(CHECK_INTERVAL)
        finally:
            if self.observer:
                await self.observer.record("stopped")

    async def _tick(self, now=None):
        from app.db.database import async_session

        now = utc(now or datetime.now(UTC))
        try:
            async with ownership(), async_session() as db:
                ids = set((await db.scalars(select(Subscription.id))).all())
                ids.update(
                    (
                        await db.scalars(
                            select(CronCursor.subscription_id).where(
                                CronCursor.scope == settings.runtime_scope
                            )
                        )
                    ).all()
                )
                for sub_id in sorted(ids):
                    sub = await db.get(Subscription, sub_id)
                    if sub is None:
                        old = await db.get(CronCursor, sub_id)
                        sub = SimpleNamespace(
                            id=sub_id,
                            enabled=False,
                            cron_expression=old.configuration.get("expression", ""),
                            agent_id=old.configuration.get("agent_id", ""),
                        )
                    if not sub.cron_expression and not await db.get(CronCursor, sub.id):
                        continue
                    cursor = await synchronize(db, sub, now)
                    await plan(db, cursor, now)
                    await deliver(db, cursor, now)
        except RuntimeErrorCode as exc:
            if exc.code != "RUNTIME_OPERATION_IN_PROGRESS":
                raise
