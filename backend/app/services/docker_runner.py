"""通过 Docker SDK 管理 DiAgent 容器。"""

import io
import json
import logging
import tarfile
from pathlib import Path

import docker
from docker.errors import NotFound, APIError, ImageNotFound
from docker.models.containers import Container
from docker.types import Mount

from app.config import settings

logger = logging.getLogger(__name__)

_client: docker.DockerClient | None = None
PRIVATE_TASK_DIR = "/run/diagent"
SUBAGENT_SKILLS_DIR = f"{PRIVATE_TASK_DIR}/subagent-skills"
TASK_AGENT_LABEL = "dios.task-agent"


def _task_container_name(run_id: str) -> str:
    return f"dios-run-{run_id}"


def get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env(timeout=30)
    return _client


def _host_path(workspace: Path) -> str:
    """返回宿主机上的 workspace 路径（Docker Socket 兄弟容器场景）。"""
    if settings.host_workspace_root:
        # 容器内路径到宿主机路径的映射
        relative = workspace.resolve().relative_to(settings.workspace_root.resolve())
        return str(Path(settings.host_workspace_root) / relative)
    return str(workspace.resolve())


def start_container(
    run_id: str,
    workspace: Path,
    extra_env: dict[str, str] | None = None,
    *,
    task_files: dict[str, dict] | None = None,
    skill_sources: dict[str, Path] | None = None,
    subagent_skill_sources: dict[str, Path] | None = None,
    managed_agent_id: str | None = None,
) -> str:
    """创建并启动一个 DiAgent 容器，返回 container_id。

    Args:
        extra_env: 业务凭据/自定义环境变量（来源于 Agent.env），会与内置 env 合并注入容器。
        task_files: 显式工具策略的配置文件，启动前写入目标容器的私有文件系统。
        managed_agent_id: 受管任务的挂载归属，随容器存续。
    """
    client = get_client()
    try:
        image = client.images.get(settings.diagent_image)
    except ImageNotFound as exc:
        raise RuntimeError(
            f"DIAGENT_IMAGE_NOT_PRESENT:{settings.diagent_image}"
        ) from exc
    if task_files is not None and image.labels.get("org.hqit.diagent.task-tool-selection") != "1":
        raise RuntimeError("The configured task image does not support tool_selection")
    if task_files and "model-refresh.json" in task_files and image.labels.get("org.hqit.diagent.task-model-refresh") != "1":
        raise RuntimeError("The configured task image does not support model grant refresh")
    subagents = (task_files or {}).get("task.json", {}).get("task", {}).get("subagents", [])
    if any("tool_selection" in role for role in subagents) and image.labels.get("org.hqit.diagent.task-subagents") != "1":
        raise RuntimeError("The configured task image does not support DSH subagents")
    uses_subagent_resources = any(
        "tool_selection" in role and (role.get("skills_dir") or role.get("mcp_config_path")) for role in subagents
    )
    if uses_subagent_resources and image.labels.get("org.hqit.diagent.task-subagent-resources") != "1":
        raise RuntimeError("The configured task image does not support DSH subagent resources")
    host_ws = _host_path(workspace)
    env: dict[str, str] = {"TASK_CONFIG": f"/workspace/agent-task-{run_id}.json"}
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                continue
            env[str(k)] = str(v)
    if task_files is not None:
        env["TASK_CONFIG"] = f"{PRIVATE_TASK_DIR}/task.json"
        env.pop("DIAGENT_TASK_MODEL_REFRESH_PATH", None)
        if "model-refresh.json" in task_files:
            env["DIAGENT_TASK_MODEL_REFRESH_PATH"] = f"{PRIVATE_TASK_DIR}/model-refresh.json"
    host_shared_cli = settings.host_shared_cli_root or _host_path(settings.shared_cli_root)
    volumes = {
        host_ws: {"bind": "/workspace", "mode": "rw"},
        host_shared_cli: {"bind": "/workspace/cli", "mode": "ro"},
    }
    if skill_sources is None:
        volumes[_host_path(settings.workspace_root / "skills")] = {"bind": "/workspace/skills", "mode": "ro"}
    else:
        for name, source in skill_sources.items():
            volumes[_host_path(source)] = {"bind": f"{PRIVATE_TASK_DIR}/skills/{name}", "mode": "ro"}
    run_options = {}
    if subagent_skill_sources:
        # 主子会话可能使用同一个宿主目录，Docker volumes 字典无法表达两个目标。
        run_options["mounts"] = [
            Mount(f"{SUBAGENT_SKILLS_DIR}/{name}", _host_path(source), type="bind", read_only=True)
            for name, source in subagent_skill_sources.items()
        ]
    if settings.docker_network:
        run_options["network"] = settings.docker_network
    labels = {"dios.run_id": run_id}
    if managed_agent_id is not None:
        labels[TASK_AGENT_LABEL] = managed_agent_id
    options = dict(
        name=_task_container_name(run_id),
        labels=labels,
        environment=env,
        volumes=volumes,
        extra_hosts={"host.docker.internal": "host-gateway"},
        auto_remove=False,
        **run_options,
    )
    if task_files is None:
        container = client.containers.run(image=settings.diagent_image, detach=True, **options)
    else:
        container = client.containers.create(image.id, **options)
        try:
            _install_task_files(container, task_files)
            container.start()
        except Exception:
            try:
                container.remove(force=True)
            except (APIError, OSError):
                logger.warning("Failed to remove unstarted task container %s", container.id, exc_info=True)
            raise
    logger.info("Started container %s for run %s", container.short_id, run_id)
    return container.id


def has_managed_task_mount(agent_id: str) -> bool:
    """容器停止前继续保护挂载，即使任务记录已经取消。"""
    containers = get_client().containers.list(all=True, filters={"label": f"{TASK_AGENT_LABEL}={agent_id}"})
    return any(container.status not in ("exited", "dead") for container in containers)


def _install_task_files(container: Container, files: dict[str, dict]) -> None:
    """配置与凭据只进入目标容器，避免同工作区的旧任务改写工具策略。"""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as bundle:
        for name, config in files.items():
            payload = json.dumps(config, ensure_ascii=False).encode("utf-8")
            entry = tarfile.TarInfo(name)
            entry.mode = 0o400
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
    if not container.put_archive(PRIVATE_TASK_DIR, archive.getvalue()):
        raise RuntimeError("Unable to install the private task configuration")


def find_task_container(run_id: str) -> str | None:
    """按任务标识找回已启动的容器，包含已经退出的容器。"""
    try:
        container = get_client().containers.get(_task_container_name(run_id))
    except NotFound:
        return None
    return container.id if container.labels.get("dios.run_id") == run_id else None


def get_container_status(container_id: str) -> str | None:
    """返回容器状态：running / exited / ... 或 None（不存在）。"""
    client = get_client()
    try:
        c = client.containers.get(container_id)
        c.reload()
        return c.status
    except NotFound:
        return None


def get_container_exit_code(container_id: str) -> int | None:
    client = get_client()
    try:
        c = client.containers.get(container_id)
        c.reload()
        return c.attrs.get("State", {}).get("ExitCode")
    except NotFound:
        return None


def get_container_logs(container_id: str, tail: int = 200) -> str:
    """Return a bounded combined stdout/stderr tail for post-mortem diagnosis."""
    client = get_client()
    try:
        c = client.containers.get(container_id)
        output = c.logs(stdout=True, stderr=True, tail=tail)
        return output.decode("utf-8", errors="replace")
    except (NotFound, APIError) as exc:
        logger.warning("Failed to read container logs %s: %s", container_id, exc)
        return ""


def stop_container(container_id: str) -> bool:
    client = get_client()
    try:
        c = client.containers.get(container_id)
        c.stop(timeout=10)
        return True
    except (NotFound, APIError) as e:
        logger.warning("Failed to stop container %s: %s", container_id, e)
        return False


def remove_container(container_id: str) -> bool:
    client = get_client()
    try:
        c = client.containers.get(container_id)
        c.remove(force=True)
        return True
    except (NotFound, APIError):
        return False
