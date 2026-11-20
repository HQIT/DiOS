"""校验 Agent 的工具名单，并为常驻请求解析兼容默认值。"""

from __future__ import annotations

from typing import Literal

from app.config import settings
from app.models.tables import Agent

_SERVICE_TOOL_IDS = {"shell", "publish_event", "skill", "mcp"}
_TASK_TOOL_IDS = {"publish_event", "skill", "mcp"}
_TASK_WORKSPACE_TOOLS = {"shell", "read", "write", "edit"}


def configured_agent_tools(
    capabilities: dict | None, *, mode: str | None,
    skills: list[str] | None, mcp_server_ids: list[str] | None,
) -> list[str] | None:
    """校验显式配置及资源前提。缺省返回 None，空名单则保留。"""
    if not isinstance(capabilities, dict) or "diagent_tools" not in capabilities:
        return None

    if mode == "service" and settings.diagent_service_model_proxy:
        supported = _SERVICE_TOOL_IDS
    elif mode == "task":
        supported = _TASK_TOOL_IDS
        if settings.diagent_task_model_proxy and settings.diagent_task_model_refresh:
            supported = supported | _TASK_WORKSPACE_TOOLS
    else:
        raise ValueError("capabilities.diagent_tools requires task mode or service mode with managed inference")

    tools = capabilities["diagent_tools"]
    if not isinstance(tools, list) or any(
        not isinstance(tool, str) or tool not in supported for tool in tools
    ):
        raise ValueError("capabilities.diagent_tools must be an array of: " + ", ".join(sorted(supported)))
    if "skill" in tools and not skills:
        raise ValueError("diagent_tools skill requires assigned Skills")
    if "mcp" in tools:
        if mode == "task" and settings.e2ag_mode.lower() != "enforce":
            raise ValueError("diagent_tools mcp requires E2AG enforce mode for one-shot tasks")
        governance = capabilities.get("governance")
        allowed_tools = governance.get("allowed_tools") if isinstance(governance, dict) else None
        if not mcp_server_ids or not isinstance(allowed_tools, list) or not allowed_tools or any(
            not isinstance(pattern, str) or not pattern.strip() for pattern in allowed_tools
        ):
            raise ValueError("diagent_tools mcp requires assigned MCP servers and capabilities.governance.allowed_tools")
    return list(dict.fromkeys(tools))


def select_service_tools(agent: Agent, *, entry: Literal["chat", "a2a"]) -> list[str] | None:
    """选择下发给 DiAgent 的工具，兼容未配置名单的存量 Agent。"""
    configured = configured_agent_tools(
        agent.capabilities, mode=agent.mode, skills=agent.skills, mcp_server_ids=agent.mcp_server_ids,
    )
    if configured is not None:
        return configured

    selected = ["shell", "publish_event"] if entry == "chat" and agent.skills else []
    if settings.diagent_service_model_proxy:
        if agent.skills:
            selected.append("skill")
        if agent.mcp_server_ids:
            selected.append("mcp")
    return selected or None
