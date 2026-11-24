"""Registry-driven polling; isolated sessions, fixed delay, drain before release."""

from __future__ import annotations

import asyncio
import logging
import math
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.connectors import registry
from app.connectors.contracts import CAPABILITY_POLL, PollInstance
from app.connectors.ownership import LocalPollOwnership, PollOwnership
from app.db.database import async_session
from app.models.tables import Connector, EventLog, Subscription
from app.services.event_dispatcher import dispatch_event
from app.services.event_normalizer import compute_dedup_hash
from app.services.event_router import match_subscriptions

logger = logging.getLogger(__name__)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


class ConnectorPoller:
    def __init__(
        self,
        *,
        sessions=async_session,
        ownership: PollOwnership | None = None,
        scan_interval=1.0,
        concurrency=4,
        enabled=True,
    ):
        if not math.isfinite(scan_interval) or scan_interval <= 0 or concurrency < 1:
            raise ValueError("poll scan interval and concurrency must be positive")
        self.sessions = sessions
        self.ownership = ownership or LocalPollOwnership(
            Path(settings.connector_poll_lock_dir)
            if settings.connector_poll_lock_dir
            else settings.workspace_root / "connector-poll-locks"
        )
        self.scan_interval, self.enabled = scan_interval, enabled
        self._slots = asyncio.Semaphore(concurrency)
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._jobs: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    async def start(self):
        if not self.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="connector-poller")

    async def stop(self):
        self._stop.set()
        for _, stop in self._jobs.values():
            stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self):
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as exc:  # noqa: BLE001 - isolate plugin failures; redact external replies
                    logger.warning("Connector discovery failed: %s", type(exc).__name__)
                await _wait(self._stop, self.scan_interval)
        finally:
            for _, stop in self._jobs.values():
                stop.set()
            await asyncio.gather(
                *(task for task, _ in self._jobs.values()), return_exceptions=True
            )
            self._jobs.clear()

    async def _tick(self):
        async with self.sessions() as db:
            rows = (
                (await db.execute(select(Connector).where(Connector.enabled.is_(True))))
                .scalars()
                .all()
            )
            eligible = {
                row.id
                for row in rows
                if (manifest := registry.manifest_for(row.type))
                and manifest.has(CAPABILITY_POLL)
            }
        for key, (task, stop) in list(self._jobs.items()):
            if key not in eligible:
                stop.set()
            if task.done():
                if not task.cancelled() and (exc := task.exception()):
                    logger.warning(
                        "Connector %s owner failed: %s", key, type(exc).__name__
                    )
                del self._jobs[key]
        for key in eligible - self._jobs.keys():
            if self._stop.is_set():
                break
            stop = asyncio.Event()
            self._jobs[key] = (
                asyncio.create_task(self._run(key, stop), name=f"poll:{key}"),
                stop,
            )

    async def _run(self, key, stop):
        while not stop.is_set():
            async with self.ownership.hold(key) as owned:
                if owned:
                    # Keep ownership through cooldown as well as collection.
                    async with self._slots:
                        if stop.is_set():
                            return
                        async with self.sessions() as db:
                            row = await db.get(Connector, key)
                            manifest = registry.manifest_for(row.type) if row else None
                            if (
                                not row
                                or not row.enabled
                                or not manifest
                                or not manifest.has(CAPABILITY_POLL)
                            ):
                                return
                            instance = PollInstance(key, deepcopy(row.config or {}))
                        try:
                            await manifest.poll_adapter.poll(instance, self._emit)
                        except Exception as exc:  # noqa: BLE001 - isolate plugin failures; redact external replies
                            logger.warning(
                                "Connector %s poll failed: %s", key, type(exc).__name__
                            )
                    await _wait(stop, manifest.poll_interval_seconds)
            if not owned:
                await _wait(stop, self.scan_interval)

    async def _emit(self, event):
        # Errors in one event cannot poison another instance's transaction.
        async with self.sessions() as db:
            subscriptions = (
                (
                    await db.execute(
                        select(Subscription).where(Subscription.enabled.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            log, error = await dispatch_event(
                event, match_subscriptions(event, subscriptions), db
            )
            if log is None:
                prefix = "Duplicate of event "
                duplicate_id = (
                    error[len(prefix) :] if error and error.startswith(prefix) else ""
                )
                existing = (
                    await db.get(EventLog, duplicate_id) if duplicate_id else None
                )
                if existing is None or existing.dedup_hash != compute_dedup_hash(event):
                    raise RuntimeError("POLL_EVENT_NOT_PERSISTED")
            await db.commit()


connector_poller = ConnectorPoller(enabled=settings.connector_poll_enabled)
