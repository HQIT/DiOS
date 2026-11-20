"""Master/管理能力相关的能力模型校验与任务下发提取。"""

from __future__ import annotations

import re
from typing import Any

from app.config import settings
from app.services.service_skills import is_skill_name


_RISK_POLICIES = {"allow", "hil_required"}
_SUBAGENT_FILE_TOOLS = {"read", "write", "edit"}
_SUBAGENT_TOOLS = _SUBAGENT_FILE_TOOLS | {"skill", "mcp"}


def normalize_capabilities(capabilities: dict | None) -> dict:
    """标准化并校验 Agent capabilities。"""
    raw = capabilities or {}
    if not isinstance(raw, dict):
        raise ValueError("capabilities must be an object")

    out = dict(raw)

    dios_admin = out.get("dios_admin")
    if dios_admin is not None:
        out["dios_admin"] = _normalize_dios_admin(dios_admin)

    reasoning = out.get("reasoning")
    if reasoning is not None:
        out["reasoning"] = _normalize_reasoning(reasoning)

    subagents = out.get("subagents")
    if subagents is not None:
        out["subagents"] = _normalize_subagents(subagents)

    return out


def _normalize_dios_admin(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("capabilities.dios_admin must be an object")

    enabled = bool(value.get("enabled", False))
    scopes = value.get("scopes", [])
    if not isinstance(scopes, list) or any(not isinstance(s, str) or not s.strip() for s in scopes):
        raise ValueError("capabilities.dios_admin.scopes must be string array")

    risk_policy = value.get("risk_policy", {}) or {}
    if not isinstance(risk_policy, dict):
        raise ValueError("capabilities.dios_admin.risk_policy must be an object")
    high_risk = str(risk_policy.get("high_risk", "hil_required"))
    if high_risk not in _RISK_POLICIES:
        raise ValueError(
            "capabilities.dios_admin.risk_policy.high_risk must be one of "
            f"{sorted(_RISK_POLICIES)}"
        )

    return {
        "enabled": enabled,
        "scopes": [s.strip() for s in scopes if s.strip()],
        "risk_policy": {"high_risk": high_risk},
    }


def _normalize_reasoning(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("capabilities.reasoning must be an object")
    out: dict[str, Any] = {}
    if "recursion_limit" in value and value["recursion_limit"] is not None:
        recursion_limit = int(value["recursion_limit"])
        if recursion_limit < 1:
            raise ValueError("capabilities.reasoning.recursion_limit must be >= 1")
        out["recursion_limit"] = recursion_limit
    if "max_tool_rounds" in value and value["max_tool_rounds"] is not None:
        max_tool_rounds = int(value["max_tool_rounds"])
        if max_tool_rounds < 1:
            raise ValueError("capabilities.reasoning.max_tool_rounds must be >= 1")
        out["max_tool_rounds"] = max_tool_rounds
    if "middleware" in value and value["middleware"] is not None:
        middleware = value["middleware"]
        if not isinstance(middleware, dict):
            raise ValueError("capabilities.reasoning.middleware must be an object")
        out["middleware"] = middleware
    return out


def _normalize_subagents(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("capabilities.subagents must be an array")
    if any(isinstance(item, dict) and "tool_selection" in item for item in value):
        return _normalize_dsh_subagents(value)
    normalized: list[dict] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"capabilities.subagents[{idx}] must be an object")
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        prompt = str(item.get("prompt") or item.get("system_prompt") or "").strip()
        if not name or not description or not prompt:
            raise ValueError(
                f"capabilities.subagents[{idx}] requires non-empty name/description/prompt"
            )
        tools = item.get("tools", [])
        if tools is None:
            tools = []
        if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
            raise ValueError(f"capabilities.subagents[{idx}].tools must be string array")
        skill_names = item.get("skill_names")
        if skill_names is not None:
            if not isinstance(skill_names, list) or any(not isinstance(s, str) for s in skill_names):
                raise ValueError(f"capabilities.subagents[{idx}].skill_names must be string array")
        normalized.append(
            {
                "name": name,
                "description": description,
                "prompt": prompt,
                "tools": tools,
                "model": item.get("model"),
                "task": item.get("task"),
                "mcp_config_path": item.get("mcp_config_path"),
                "skills_dir": item.get("skills_dir"),
                "skill_names": skill_names,
            }
        )
    return normalized


def _normalize_dsh_subagents(value: list) -> list[dict]:
    normalized = []
    names = set()
    for idx, item in enumerate(value):
        field = f"capabilities.subagents[{idx}]"
        if not isinstance(item, dict) or "tool_selection" not in item:
            raise ValueError(f"{field} requires tool_selection when configuring DSH subagents")
        name = item.get("name")
        description = item.get("description")
        prompt = item.get("prompt") or item.get("system_prompt")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name.strip()):
            raise ValueError(f"{field}.name must be a tool-safe identifier")
        name = name.strip()
        if name in names:
            raise ValueError(f"{field}.name must be unique")
        names.add(name)
        if not isinstance(description, str) or not description.strip() or not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"{field} requires non-empty description and prompt")
        if (item.get("tools") is not None
                or any(item.get(key) not in (None, "") for key in ("model", "task", "mcp_config_path", "skills_dir"))):
            raise ValueError(f"{field} must use tool_selection and inherit the task model; overrides are unsupported")
        selection = item["tool_selection"]
        if not isinstance(selection, dict) or selection.get("tool_config") is not None:
            raise ValueError(f"{field}.tool_selection requires tool_ids and does not accept tool_config")
        tools = selection.get("tool_ids")
        if not isinstance(tools, list) or any(
            not isinstance(tool, str) or tool not in _SUBAGENT_TOOLS for tool in tools
        ):
            raise ValueError(f"{field}.tool_selection.tool_ids must be an array of skill, mcp, read, write, or edit")
        skill_names = item.get("skill_names", [])
        if not isinstance(skill_names, list) or not all(is_skill_name(name) for name in skill_names):
            raise ValueError(f"{field}.skill_names must be an array of Skill names")
        if ("skill" in tools) != bool(skill_names):
            raise ValueError(f"{field} must configure skill_names together with the skill tool")
        mcp_server_ids = item.get("mcp_server_ids", [])
        mcp_tools = item.get("mcp_tools", [])
        if not isinstance(mcp_server_ids, list) or any(
            not isinstance(s, str) or not s.strip() for s in mcp_server_ids
        ):
            raise ValueError(f"{field}.mcp_server_ids must be an array of registered server IDs")
        if not isinstance(mcp_tools, list) or any(
            not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", t) for t in mcp_tools
        ):
            raise ValueError(f"{field}.mcp_tools must be an array of exact tool names")
        if bool(mcp_server_ids) != ("mcp" in tools) or bool(mcp_tools) != ("mcp" in tools):
            raise ValueError(f"{field} must configure mcp_server_ids and mcp_tools together with the mcp tool")
        role = {
            "name": name,
            "description": description.strip(),
            "prompt": prompt.strip(),
            "tool_selection": {"tool_ids": list(dict.fromkeys(tools))},
        }
        if skill_names:
            role["skill_names"] = list(dict.fromkeys(skill_names))
        if mcp_server_ids:
            role["mcp_server_ids"] = list(dict.fromkeys(mcp_server_ids))
            role["mcp_tools"] = list(dict.fromkeys(mcp_tools))
        normalized.append(role)
    return normalized


def subagents_for_task(capabilities: dict | None) -> list[dict]:
    """生成下发给任务执行器的角色配置，保留存量格式的工具语义。"""
    value = capabilities.get("subagents") if isinstance(capabilities, dict) else None
    if not isinstance(value, list):
        return []
    if any(isinstance(item, dict) and "tool_selection" in item for item in value):
        return _normalize_dsh_subagents(value)

    # 存量任务沿用 Python 执行器的字段和缺省值。
    converted = []
    for item in value:
        if not isinstance(item, dict):
            continue
        prompt = item.get("prompt") or item.get("system_prompt")
        if item.get("name") and item.get("description") and prompt:
            converted.append({
                "name": item["name"], "description": item["description"], "prompt": prompt,
                "tools": item.get("tools", []), "model": item.get("model"), "task": item.get("task"),
                "mcp_config_path": item.get("mcp_config_path"), "skills_dir": item.get("skills_dir"),
                "skill_names": item.get("skill_names"),
            })
    return converted


def validate_subagent_runtime(subagents: list[dict], *, mode: str | None) -> None:
    """在保存及发放任务授权前，确认当前部署能运行已验证的角色配置。"""
    if not subagents or "tool_selection" not in subagents[0]:
        return
    if mode != "task":
        raise ValueError("DSH subagents require task mode")
    if not settings.diagent_task_model_proxy:
        raise ValueError("DSH subagents require managed task inference")
    if not settings.diagent_task_model_refresh and any(
        _SUBAGENT_FILE_TOOLS.intersection(role["tool_selection"]["tool_ids"]) for role in subagents
    ):
        raise ValueError("DSH subagent file tools require managed task inference with grant refresh")


def selected_subagent_skills(subagents: Any) -> list[str]:
    """返回具名 DSH 子 Agent 请求的 Skill，供校验和只读挂载共用。"""
    if not isinstance(subagents, list):
        return []
    return list(dict.fromkeys(
        name for role in subagents
        if isinstance(role, dict) and "tool_selection" in role and isinstance(role.get("skill_names"), list)
        for name in role["skill_names"] if is_skill_name(name)
    ))

