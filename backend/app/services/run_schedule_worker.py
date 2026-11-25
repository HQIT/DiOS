"""Bounded scheduler observer; only the existing Worker executes accepted Runs."""

import asyncio
import logging

from app.runtime.database import RuntimeErrorCode
from app.runtime.monitor import LoopObservation
from app.services.run_schedules import RunSchedules

logger = logging.getLogger(__name__)


class ScheduleLoop:
    def __init__(self, console, *, enabled=True):
        self.service = RunSchedules(console)
        self.observer = LoopObservation(console.db, console.scope, "run_scheduler")
        self.enabled = enabled
        self.task = None

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def run(self):
        try:
            await self._run()
        finally:
            await self.observer.record("stopped")

    async def _run(self):
        previous = None
        while True:
            if not self.enabled:
                await self.observer.idle(5, disabled=True)
                continue
            try:
                await self.observer.cycle(self.service.tick)
                previous = None
            except RuntimeErrorCode as exc:
                if previous != exc.code:
                    logger.warning("Run scheduler: %s", exc.code)
                previous = exc.code
            except Exception:
                logger.exception("Run scheduler observation failed")
            await self.observer.idle(5)
