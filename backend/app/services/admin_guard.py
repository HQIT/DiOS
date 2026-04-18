"""Master/管理能力相关的能力模型校验与任务下发提取。"""

from __future__ import annotations

from typing import Any


_RISK_POLICIES = {"allow", "hil_required"}


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

