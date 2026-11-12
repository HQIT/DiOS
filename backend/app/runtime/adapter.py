"""Lifecycle contract. Protocol/session semantics belong to a separate bridge."""
from dataclasses import dataclass, field, asdict
import hashlib
import json
from pathlib import Path
from typing import Protocol
from .database import RuntimeErrorCode


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class LaunchSpec:
    image: str
    command: tuple[str, ...]
    kind: str = 'task'
    environment: dict[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    network: str = 'none'
    working_dir: str = '/tmp'
    user: str = '65534:65534'
    memory_bytes: int = 256 * 1024 * 1024
    cpu_count: float = 1.0
    pids_limit: int = 64
    read_only_root: bool = True

    def validate(self):
        if (not self.image or not self.command or self.kind not in {'task', 'service'}
                or not all(isinstance(s, str) and s for s in self.command)
                or not self.network or not self.user or not self.working_dir.startswith('/')
                or type(self.memory_bytes) is not int or self.memory_bytes < 16 * 1024 * 1024
                or not 0 < self.cpu_count <= 128 or not 1 <= self.pids_limit <= 4096):
            raise RuntimeErrorCode('INVALID_LAUNCH_SPEC')
        for mount in self.mounts:
            if not Path(mount.source).is_absolute() or not mount.target.startswith('/'):
                raise RuntimeErrorCode('INVALID_MOUNT')
        if len({m.target for m in self.mounts}) != len(self.mounts):
            raise RuntimeErrorCode('DUPLICATE_MOUNT')
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in self.environment.items()):
            raise RuntimeErrorCode('INVALID_ENVIRONMENT')

    def fingerprint(self):
        self.validate()
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    handle: str
    state: str
    alive: bool
    pid: int | None
    process_instance: str | None
    exit_code: int | None
    health: str
    runtime_host: str


@dataclass(frozen=True)
class LogSnapshot:
    stdout: bytes
    stderr: bytes
    truncated: bool = False


class RuntimeAdapter(Protocol):
    """Handles are deterministic, scoped, and rediscoverable after controller loss."""
    name: str
    async def start(self, handle: str, spec: LaunchSpec) -> Observation: ...
    async def observe(self, handle: str) -> Observation: ...
    async def logs(self, handle: str, max_bytes: int) -> LogSnapshot: ...
    async def stop(self, handle: str, grace_seconds: int = 5) -> Observation: ...
    async def remove(self, handle: str) -> None: ...
