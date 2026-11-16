from hashlib import sha256
from secrets import compare_digest
from pydantic_settings import BaseSettings
from pydantic import BaseModel, Field, model_validator
from pathlib import Path


class DiAgentEventGrant(BaseModel):
    """部署单独授予一个 Agent 发布事件所需的凭据与范围。"""

    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_patterns: list[str] = Field(min_length=1)
    event_types: list[str] = Field(min_length=1)
    model_config = {"hide_input_in_errors": True}


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
    diagent_service_model_proxy: bool = False
    diagent_event_grants: dict[str, DiAgentEventGrant] = Field(default_factory=dict)
    diagent_service_url: str = "http://localhost:8001"  # fallback, runtime manager 会动态获取
    docker_network: str = ""
    max_concurrent_runs: int = 5

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
    # The host-gateway mapping in docker_runner makes this URL portable.
    e2ag_internal_base_url: str = "http://host.docker.internal:8000"

    # 非空时启用 API Access Token（Authorization: Bearer 或 X-DiOS-Access-Token）
    access_token: str = ""

    model_config = {"env_prefix": "DIOS_", "hide_input_in_errors": True}

    @model_validator(mode="after")
    def require_event_grant_authentication(self):
        """只有控制面启用鉴权，事件发布凭据才构成独立权限。"""
        if not self.diagent_event_grants:
            return self
        if not self.access_token.strip():
            raise ValueError("DIAGENT event grants require DIOS_ACCESS_TOKEN")
        admin_digest = sha256(self.access_token.encode()).hexdigest()
        digests = [grant.token_sha256 for grant in self.diagent_event_grants.values()]
        if any(compare_digest(digest, admin_digest) for digest in digests):
            raise ValueError("DiAgent event tokens must differ from DIOS_ACCESS_TOKEN")
        if len(set(digests)) != len(digests):
            raise ValueError("Each DiAgent event grant must have a distinct token")
        return self


settings = Settings()
