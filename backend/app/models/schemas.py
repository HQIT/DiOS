from pydantic import BaseModel
from datetime import datetime
from typing import Optional


# ── LLMModel ──

class LLMModelCreate(BaseModel):
    name: str
    provider: str = "openai"
    model: str
    base_url: str
    api_key: str = ""
    display_name: str = ""
    description: str = ""
    context_length: Optional[int] = None


class LLMModelUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    context_length: Optional[int] = None


class LLMModelOut(BaseModel):
    id: str
    name: str
    provider: str
    model: str
    base_url: str
    api_key: str
    display_name: str
    description: str
    context_length: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Team ──

class TeamCreate(BaseModel):
    name: str
    description: str = ""
    default_model: str = ""


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    default_model: Optional[str] = None


class TeamOut(BaseModel):
    id: str
    name: str
    description: str
    workspace_path: str
    default_model: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Agent ──

class AgentCreate(BaseModel):
    name: str
    role: str = "sub"  # main / sub
    description: str = ""
    model: str = ""
    system_prompt: str = ""
    skills: list[str] = []
    mcp_config_path: str = ""


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    description: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    skills: Optional[list[str]] = None
    mcp_config_path: Optional[str] = None


class AgentOut(BaseModel):
    id: str
    team_id: str
    name: str
    role: str
    description: str
    model: str
    system_prompt: str
    skills: list[str]
    mcp_config_path: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Run ──

class RunCreate(BaseModel):
    task: str
    model: Optional[str] = None
    temperature: Optional[float] = None


class RunOut(BaseModel):
    id: str
    team_id: str
    task_text: str
    status: str
    container_id: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    log_path: Optional[str] = None
    result_path: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Subscription ──

class SubscriptionCreate(BaseModel):
    source_pattern: str
    event_types: list[str]
    filter_rules: dict = {}
    enabled: bool = True


class SubscriptionUpdate(BaseModel):
    source_pattern: Optional[str] = None
    event_types: Optional[list[str]] = None
    filter_rules: Optional[dict] = None
    enabled: Optional[bool] = None


class SubscriptionOut(BaseModel):
    id: str
    agent_id: str
    source_pattern: str
    event_types: list[str]
    filter_rules: dict
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── EventLog ──

class EventLogOut(BaseModel):
    id: str
    source: str
    event_type: str
    subject: str
    cloud_event: dict
    matched_agent_ids: list[str]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}
