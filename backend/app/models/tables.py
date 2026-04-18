import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, String, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LLMModel(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(32), default="openai")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key: Mapped[str] = mapped_column(String(512), default="")
    display_name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    context_length: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="service")  # service (常驻可聊天) | task (一次性任务)
    group: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="agent")
    description: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    mcp_config_path: Mapped[str] = mapped_column(String(512), default="")
    mcp_server_ids: Mapped[list] = mapped_column(JSON, default=list)  # 选中的 McpServer id 列表，下发时生成 config
    workspace_path: Mapped[str] = mapped_column(String(512), default="")
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)  # A2A AgentCapabilities: {streaming, push_notifications, ...}
    env: Mapped[dict] = mapped_column(JSON, default=dict)  # 业务凭据/环境变量（注入任务容器），含 TOKEN/KEY/SECRET 的 key 在 API 返回时脱敏
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Connectors (事件源) ──


class Connector(Base):
    __tablename__ = "connectors"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # github, gitlab, gitea, imap, generic
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # type 相关: webhook secret, IMAP 参数等
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── MCP（供 DiAgent 使用） ──


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    command: Mapped[str] = mapped_column(String(512), nullable=False)
    args: Mapped[list] = mapped_column(JSON, default=list)
    env: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Skills（OS 严选的 Skills 仓库） ──


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    source_url: Mapped[str] = mapped_column(String(512), default="")  # git repo URL
    content: Mapped[str] = mapped_column(Text, default="")  # SKILL.md 内容
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Event Gateway ──


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    source_pattern: Mapped[str] = mapped_column(String(256), nullable=False)
    event_types: Mapped[list] = mapped_column(JSON, nullable=False)
    filter_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    cron_expression: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Agent Runtime（OS 管理的 DiAgent 服务实例） ──


class AgentRuntime(Base):
    __tablename__ = "agent_runtimes"

    agent_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    container_id: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="starting")  # starting | running | stopped | error
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Chat App（会话 + 消息持久化） ──


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[str] = mapped_column(String(12), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(256), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), default="")
    cloud_event: Mapped[dict] = mapped_column(JSON, nullable=False)
    matched_agent_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received, dispatching, dispatched, failed, dead_letter
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    
    # 重试机制字段
    retry_count: Mapped[int] = mapped_column(default=0)
    max_retries: Mapped[int] = mapped_column(default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="")
    
    # 幂等性去重字段
    dedup_hash: Mapped[str] = mapped_column(String(64), index=True, default="")


# ── A2A Task（Agent-to-Agent 协议任务） ──


class A2ATask(Base):
    """A2A 协议 Task 实体。
    Task 是 A2A 中 send_message 产生的一次 Agent 调用单元。
    - service 模式 Agent：DiOS 转发 HTTP，Task 跟踪调用状态
    - task 模式 Agent：DiOS 作为 Proxy，启动一次性容器，Task 生命周期等于容器生命周期
    """
    __tablename__ = "a2a_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    agent_id: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    context_id: Mapped[str] = mapped_column(String(64), default="", index=True)  # 关联源（EventLog.id / chat_session_id 等）
    status: Mapped[str] = mapped_column(String(16), default="submitted")  # submitted | working | completed | failed | canceled
    message: Mapped[dict] = mapped_column(JSON, nullable=False)  # A2A Message 入参
    artifacts: Mapped[list] = mapped_column(JSON, default=list)  # A2A Artifact[] 输出
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
