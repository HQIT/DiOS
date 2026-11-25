"""Observe scheduler cycles without acquiring work or cancelling slow operations."""

import asyncio
import logging
import re
import time
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .database import RuntimeErrorCode
from .monitor_schema import loops, require

logger = logging.getLogger(__name__)
SERVICES = ("run_scheduler", "subscription_scheduler")
STATES = ("healthy", "degraded", "slow", "stale", "stopped", "unobserved", "disabled")
FRESH_SECONDS = 20
SLOW_SECONDS = 120


class LoopObservation:
    def __init__(self, db, scope, service, *, interval=5):
        if service not in SERVICES:
            raise RuntimeErrorCode("INVALID_MONITOR_SERVICE")
        self.db, self.scope, self.service = db, scope, service
        self.instance = str(uuid4())
        self.interval = interval
        self.cycle_started_at = self.completed_at = self.succeeded_at = None
        self.duration_seconds = None
        self.cycles = self.errors = 0
        self.error = None
        self.last_write_error = None

    async def record(self, state, *, begin=False, completed=False):
        # Telemetry failure never retries the operation or reports it successful.
        try:
            async with self.db.transaction(self.scope) as c:
                await require(c)
                now = await self.db.now(c)
                if begin:
                    self.cycle_started_at = now
                if completed:
                    self.completed_at = now
                    if self.error is None:
                        self.succeeded_at = now
                values = {
                    "state": state,
                    "heartbeat_at": now,
                    "cycle_started_at": self.cycle_started_at,
                    "completed_at": self.completed_at,
                    "succeeded_at": self.succeeded_at,
                    "duration_seconds": self.duration_seconds,
                    "cycles": self.cycles,
                    "consecutive_errors": self.errors,
                    "error": self.error,
                }
                key = {
                    "scope": self.scope,
                    "service": self.service,
                    "instance": self.instance,
                }
                condition = (
                    (loops.c.scope == self.scope)
                    & (loops.c.service == self.service)
                    & (loops.c.instance == self.instance)
                )
                exists = (
                    await c.execute(select(loops.c.instance).where(condition))
                ).first()
                if exists:
                    await c.execute(loops.update().where(condition).values(**values))
                else:
                    await c.execute(loops.insert().values(**key, **values))
            self.last_write_error = None
        except (RuntimeErrorCode, SQLAlchemyError, OSError) as exc:
            code = type(exc).__name__
            if code != self.last_write_error:
                logger.warning("Scheduler observation unavailable: %s", code)
            self.last_write_error = code

    async def cycle(self, operation):
        await self.record("running", begin=True)
        started = time.monotonic()
        task = asyncio.create_task(operation())
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=self.interval)
                if not done:
                    await self.record("running")
            try:
                result = task.result()
            except asyncio.CancelledError:
                self.error = "SCHEDULER_CANCELLED"
                self.errors += 1
                raise
            except Exception as exc:
                raw = (
                    exc.code if isinstance(exc, RuntimeErrorCode) else "SCHEDULER_ERROR"
                )
                self.error = (
                    raw
                    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", raw)
                    else "SCHEDULER_ERROR"
                )
                self.errors += 1
                raise
            else:
                self.errors, self.error = 0, None
                return result
            finally:
                self.cycles += 1
                self.duration_seconds = time.monotonic() - started
                await self.record("idle", completed=True)
        finally:
            if not task.done():
                # Only lifecycle cancellation cancels the operation; elapsed time does not.
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def idle(self, seconds, *, disabled=False):
        end = time.monotonic() + seconds
        while True:
            await self.record("disabled" if disabled else "idle")
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.interval, remaining))


def classify(row, now):
    if row["state"] == "stopped":
        return "stopped"
    if not now - FRESH_SECONDS <= row["heartbeat_at"] <= now + 1:
        return "stale"
    if row["state"] == "disabled":
        return "disabled"
    if (
        row["state"] == "running"
        and row["cycle_started_at"] is not None
        and now - row["cycle_started_at"] >= SLOW_SECONDS
    ):
        return "slow"
    if row["consecutive_errors"]:
        return "degraded"
    return "healthy" if row["succeeded_at"] is not None else "unobserved"


async def snapshot(db, scope):
    services = []
    async with db.read(scope) as c:
        await require(c)
        now = await db.now(c)
        for service in SERVICES:
            rows = (
                (
                    await c.execute(
                        select(loops)
                        .where(loops.c.scope == scope, loops.c.service == service)
                        .order_by(loops.c.heartbeat_at.desc(), loops.c.instance)
                        .limit(101)
                    )
                )
                .mappings()
                .all()
            )
            items = [dict(r) | {"diagnosis": classify(r, now)} for r in rows[:100]]
            fresh = [
                r
                for r in items
                if r["diagnosis"] not in {"stale", "stopped", "disabled"}
            ]
            enabled = fresh or [r for r in items if r["diagnosis"] == "disabled"]
            latest = items[0] if items else None
            state = "unobserved"
            if enabled:
                state = next(
                    s
                    for s in (
                        "slow",
                        "degraded",
                        "unobserved",
                        "healthy",
                        "disabled",
                    )
                    if any(r["diagnosis"] == s for r in enabled)
                )
            elif latest:
                state = latest["diagnosis"]
            if len(rows) > 100 and items[-1]["heartbeat_at"] >= now - FRESH_SECONDS:
                state = "unobserved"
            services.append(
                {
                    "service": service,
                    "state": state,
                    "fresh_instances": len(fresh),
                    "latest": latest,
                    "evidence": next(
                        (r for r in items if r["diagnosis"] == state), latest
                    ),
                    "instances": items,
                    "at_limit": len(rows) > 100,
                }
            )
    return {
        "observed_at": now,
        "freshness_seconds": FRESH_SECONDS,
        "slow_seconds": SLOW_SECONDS,
        "services": services,
    }


def prometheus(value):
    # Fixed allowlisted labels and numeric values; never include IDs, errors or user data.
    lines = [
        "# HELP dios_scheduler_state Current scheduler observation state.",
        "# TYPE dios_scheduler_state gauge",
    ]
    for service in value["services"]:
        for state in STATES:
            lines.append(
                f'dios_scheduler_state{{service="{service["service"]}",state="{state}"}} {int(service["state"] == state)}'
            )
    lines += [
        "# HELP dios_scheduler_fresh_instances Fresh enabled scheduler instances.",
        "# TYPE dios_scheduler_fresh_instances gauge",
    ]
    for service in value["services"]:
        lines.append(
            f'dios_scheduler_fresh_instances{{service="{service["service"]}"}} {service["fresh_instances"]}'
        )
    return "\n".join(lines) + "\n"
