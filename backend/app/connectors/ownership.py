"""Local process ownership using filelock's native operating-system locks."""

from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Protocol

from filelock import FileLock, Timeout


class PollOwnership(Protocol):
    def hold(self, instance_id: str) -> AbstractAsyncContextManager[bool]:
        """Exclusively own the instance until exit; False means another owner.

        A future remote provider must fence emit/ack before allowing takeover.
        A TTL alone does not satisfy this contract.
        """
        ...


class LocalPollOwnership:
    def __init__(self, directory: Path):
        self.directory = directory

    @asynccontextmanager
    async def hold(self, instance_id: str):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = hashlib.sha256(instance_id.encode()).hexdigest() + ".lock"
        lock = FileLock(
            self.directory / name,
            timeout=0,
            mode=0o600,
            fallback_to_soft=False,
            preserve_lock_file=True,
        )
        try:
            lock.acquire()
        except Timeout:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
