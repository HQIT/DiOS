"""Bounded FIFO admission for a single control service's SQLite writer.

The queue is volatile admission, not a durable task queue. Only transaction
commit acknowledges persistence; retries retain their existing identity.
"""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import time


class WriteQueueError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class WriteGate:
    def __init__(self, capacity=64, timeout=5.0):
        if capacity < 0 or timeout <= 0:
            raise ValueError('invalid write admission limits')
        self.capacity, self.timeout = capacity, timeout
        self.waiters = deque()
        self.active = False
        self.accepting = True
        self.idle = asyncio.Event()
        self.idle.set()
        self.admitted = self.rejected = self.expired = 0
        self.max_wait_ms = 0.0

    def snapshot(self):
        return dict(capacity=self.capacity, queued=len(self.waiters), active=int(self.active),
                    accepting=self.accepting, admitted=self.admitted, rejected=self.rejected,
                    expired=self.expired, max_wait_ms=self.max_wait_ms)

    async def acquire(self):
        if not self.accepting:
            self.rejected += 1
            raise WriteQueueError('WRITE_DRAINING')
        if not self.active:
            self.active = True
            self.idle.clear()
            self.admitted += 1
            return
        if len(self.waiters) >= self.capacity:
            self.rejected += 1
            raise WriteQueueError('WRITE_QUEUE_FULL')
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        started = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except BaseException as exc:
            # Cancellation can race a grant. Release that reservation exactly once.
            if future.done() and not future.cancelled():
                self.release()
            else:
                self.waiters.remove(future)
                future.cancel()
            if isinstance(exc, TimeoutError):
                self.expired += 1
                raise WriteQueueError('WRITE_QUEUE_TIMEOUT') from exc
            raise
        self.admitted += 1
        self.max_wait_ms = max(self.max_wait_ms, (time.monotonic()-started)*1000)

    def release(self):
        if self.waiters:
            self.waiters.popleft().set_result(None)
        else:
            self.active = False
            self.idle.set()

    @asynccontextmanager
    async def slot(self):
        await self.acquire()
        try:
            yield
        finally:
            self.release()

    async def drain(self):
        self.accepting = False
        await self.idle.wait()
