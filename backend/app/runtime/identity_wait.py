"""Bounded identity/authorization revalidation; never replays workload actions."""

import asyncio
import time

from .database import RuntimeErrorCode


async def identity_wait(check, *, maintain=None, suspend=None, resume=None, timeout=60):
    deadline = time.monotonic() + max(0, timeout)
    waiting, delay = False, 1
    while True:
        if waiting and time.monotonic() >= deadline:
            raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
        if waiting and maintain:
            await maintain()
        try:
            result = await check()
        except RuntimeErrorCode as exc:
            if exc.code != "AUTH_PROVIDER_UNAVAILABLE" or time.monotonic() >= deadline:
                raise
            if not waiting and suspend:
                await suspend()
            waiting = True
            await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
            delay = min(5, delay * 2)
        else:
            if waiting and time.monotonic() >= deadline:
                raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
            if waiting and resume:
                if maintain:
                    await maintain()
                await resume()
            return result
