"""根据 Team + Agents + 系统模型配置生成 DiAgent agent-task.json。"""

import json
from pathlib import Path
from typing import Any

from app.models.tables import Team, Agent, LLMModel


def build_task_config(
    team: Team,
    agents: list[Agent],
    llm_models: list[LLMModel],
    task_text: str,
    run_id: str,
    *,
    model_override: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    model_map = {m.name: m for m in llm_models}

    main_agent = next((a for a in agents if a.role == "main"), None)
    sub_agents = [a for a in agents if a.role == "sub"]

    used_model = model_override or (main_agent.model if main_agent and main_agent.model else team.default_model)

    # 收集所有用到的模型名
    all_model_names = {used_model} | {a.model for a in agents if a.model}
    all_model_names.discard("")

    models_section: dict[str, Any] = {
        "default_model": used_model,
        "models": {},
    }
    for name in all_model_names:
        if name and name in model_map:
            m = model_map[name]
            entry: dict[str, Any] = {
                "provider": m.provider,
                "model": m.model,
                "base_url": m.base_url,
            }
            if m.api_key:
                entry["api_key"] = m.api_key
            if m.display_name:
                entry["display_name"] = m.display_name
            if m.context_length:
                entry["context_length"] = m.context_length
            models_section["models"][name] = entry

    task_section: dict[str, Any] = {
        "task": task_text,
        "model": used_model,
        "temperature": temperature or 0.7,
        "workspace": "/workspace",
        "output": {
            "log_file": "task.log",
            "result_file": "task_result.md",
        },
        "output_dir": f"output/runs/{run_id}",
        "trigger": {"mode": "once"},
        "recursion_limit": 100,
    }

    if main_agent and main_agent.system_prompt:
        task_section["system_prompt"] = main_agent.system_prompt
    if main_agent and main_agent.skills:
        task_section["skill_names"] = main_agent.skills
    if main_agent and main_agent.mcp_config_path:
        task_section["mcp_config_path"] = main_agent.mcp_config_path

    subagents_list = []
    for sa in sub_agents:
        sa_entry: dict[str, Any] = {
            "name": sa.name,
            "description": sa.description,
            "prompt": sa.system_prompt or f"你是 {sa.name}",
        }
        if sa.model:
            sa_entry["model"] = sa.model
        if sa.skills:
            sa_entry["skill_names"] = sa.skills
        if sa.mcp_config_path:
            sa_entry["mcp_config_path"] = sa.mcp_config_path
        subagents_list.append(sa_entry)

    if subagents_list:
        task_section["subagents"] = subagents_list

    return {"models": models_section, "task": task_section}


def write_task_config(workspace: Path, run_id: str, config: dict) -> Path:
    config_path = workspace / f"agent-task-{run_id}.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path
