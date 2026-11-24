from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./dios.db"
    workspace_root: Path = Path(__file__).resolve().parent.parent.parent / "workspace"
    shared_cli_root: Path = Path(__file__).resolve().parent.parent.parent / "cli"
    # Docker Socket 模式下，宿主机上 workspace 的真实路径
    # 容器内 /workspace 和宿主机路径不同，挂载给兄弟容器时需要宿主机路径
    host_workspace_root: str = ""
    host_shared_cli_root: str = ""
    diagent_image: str = "ghcr.io/hqit/diagent/agent-task:latest"
    diagent_service_image: str = "dios-diagent-service:latest"
    diagent_service_url: str = "http://localhost:8001"  # fallback, runtime manager 会动态获取
    docker_network: str = ""
    max_concurrent_runs: int = 5
    # All poll owners for one installation share this local filesystem directory.
    connector_poll_enabled: bool = True
    connector_poll_lock_dir: str = ""
    managed_runtime_user: str = "1000:1000"
    managed_runtime_memory_bytes: int = 512 * 1024 * 1024
    managed_runtime_lock_dir: str = ""

    # Webhook 签名验证密钥，按平台名存储
    # 如 {"github": "xxx", "gitlab": "yyy", "gitea": "zzz"}
    webhook_secrets: dict[str, str] = {}
    
    # 事件重试配置
    event_max_retries: int = 3
    
    # 事件去重配置
    event_dedup_enabled: bool = True
    event_dedup_window_hours: int = 1
    event_dedup_exclude_types: list[str] = ["cron.tick", "manual.trigger"]

    # E2AG enforcement: off | contract | enforce
    e2ag_mode: str = "enforce"
    e2ag_tool_grant_ttl_seconds: int = 900
    e2ag_approval_ttl_seconds: int = 3600
    # One-shot DiAgent containers are Docker siblings, not Compose services.
    # The managed lifecycle launch profile supplies the host-gateway alias.
    e2ag_internal_base_url: str = "http://host.docker.internal:8000"

    # 非空时启用 API Access Token（Authorization: Bearer 或 X-DiOS-Access-Token）
    access_token: str = ""

    # Explicit trusted-operator runtime console. Workers remain separate processes.
    runtime_console_enabled: bool = False
    runtime_database_url: str = ""
    runtime_scope: str = "local"
    runtime_profiles_dir: str = ""
    runtime_artifacts_root: str = ""
    runtime_policy_file: str = ""
    runtime_secrets_file: str = ""
    runtime_tools_file: str = ""
    runtime_approvers_file: str = ""

    model_config = {"env_prefix": "DIOS_"}


settings = Settings()
