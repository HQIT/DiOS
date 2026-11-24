"""Managed task/service launch configuration, independent of lifecycle backend."""

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from app.config import settings
from app.runtime.adapter import LaunchSpec, Mount
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import RuntimeErrorCode
from app.runtime.providers import managed_adapter


def handle(kind: str, identity: str) -> str:
    if kind not in {"task", "service"} or not identity:
        raise RuntimeErrorCode("INVALID_MANAGED_IDENTITY")
    digest = hashlib.sha256(
        (settings.runtime_scope + ":" + identity).encode()
    ).hexdigest()[:40]
    return f"managed-{kind}-{digest}"


@asynccontextmanager
async def exclusive(runtime_handle: str):
    root = (
        Path(settings.managed_runtime_lock_dir)
        if settings.managed_runtime_lock_dir
        else settings.workspace_root / "managed-runtime-locks"
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = FileLock(
        root / (hashlib.sha256(runtime_handle.encode()).hexdigest() + ".lock"),
        timeout=0,
        mode=0o600,
        fallback_to_soft=False,
        preserve_lock_file=True,
    )
    try:
        lock.acquire()
    except Timeout:
        raise RuntimeErrorCode("RUNTIME_OPERATION_IN_PROGRESS") from None
    try:
        yield
    finally:
        lock.release()


def host_workspace(path: Path) -> str:
    path = path.resolve()
    if settings.host_workspace_root:
        relative = path.relative_to(settings.workspace_root.resolve())
        return str(Path(settings.host_workspace_root) / relative)
    return str(path)


def launch_spec(kind: str, workspace: Path, environment: dict[str, str]) -> LaunchSpec:
    cli = settings.host_shared_cli_root or str(settings.shared_cli_root.resolve())
    return LaunchSpec(
        image=settings.diagent_image
        if kind == "task"
        else settings.diagent_service_image,
        command=None,  # Resolve the selected image's native command, pinned by image ID.
        kind=kind,
        environment={"HOME": "/tmp", **environment},
        mounts=(
            Mount(host_workspace(workspace), "/workspace", False),
            Mount(
                host_workspace(settings.workspace_root / "skills"), "/workspace/skills"
            ),
            Mount(cli, "/workspace/cli"),
        ),
        working_dir="/app",
        user=settings.managed_runtime_user,
        memory_bytes=settings.managed_runtime_memory_bytes,
        network=settings.docker_network or "bridge",
        host_aliases={"host.docker.internal": "host-gateway"},
    )


async def start_task(
    task_id: str, run_id: str, workspace: Path, extra_env: dict
) -> str:
    runtime_handle = handle("task", task_id)
    env = {str(k): str(v) for k, v in extra_env.items() if v is not None}
    # System-owned input cannot be replaced by Agent.env.
    env["TASK_CONFIG"] = f"/workspace/agent-task-{run_id}.json"
    await managed_adapter().start(runtime_handle, launch_spec("task", workspace, env))
    return runtime_handle


def log_archive(runtime_handle: str) -> LocalArtifacts:
    root = settings.workspace_root / "managed-runtime-artifacts"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep retained evidence outside the Agent's writable workspace mount.
    key = hashlib.sha256(runtime_handle.encode()).hexdigest()
    return LocalArtifacts(root / key, max_bytes=512 * 1024)


async def retain_logs(
    runtime_handle: str, workspace: Path, process_instance: str | None
):
    snapshot = await managed_adapter().logs(runtime_handle, 64 * 1024)
    record = {
        "runtime_handle": runtime_handle,
        "process_instance": process_instance,
        "workspace": str(workspace),
        "stdout": snapshot.stdout.decode("utf-8", errors="replace"),
        "stderr": snapshot.stderr.decode("utf-8", errors="replace"),
        "truncated": snapshot.truncated,
    }
    log_archive(runtime_handle).put(
        [json.dumps(record, ensure_ascii=False).encode("utf-8")]
    )
    return snapshot
