"""Offline feasibility probe of a pinned, unmodified AgentSpec implementation.

Not a comparative benchmark. The sole tool appends to an in-memory list.
An original deterministic predicate is exercised on inert text, never executed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import traceback
from unittest.mock import patch

PIN = "e6fa3902e2cfb9681f454b355691b771f70543f8"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--finish-compat", action="store_true", help="Guard None at task finish, without changing upstream sources")
    args = parser.parse_args()
    source = args.upstream.resolve()
    if args.output.exists():
        raise FileExistsError("Use a new output filename; do not overwrite evidence")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True)
    bytecode = [line for line in dirty.splitlines() if line.endswith(".pyc") and "__pycache__/" in line]
    source_changes = [line for line in dirty.splitlines() if line not in bytecode]
    if sha != PIN or source_changes:
        raise RuntimeError("Expected pinned clean upstream checkout")
    sys.dont_write_bytecode = True
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    sys.path.insert(0, str(source / "src"))
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    from rule import Rule

    class FinishCompatibleRule(Rule):
        def triggered(self, action_name, input):
            if action_name == "finish" and input is None and self.event not in {"finish", "any"}:
                return False
            return super().triggered(action_name, input)

    def make_rule(text):
        native = Rule.from_text(text)
        return FinishCompatibleRule(**native.model_dump()) if args.finish_compat else native

    class FixedPlanner(BaseSingleActionAgent):
        stop_after_step: bool = True
        @property
        def input_keys(self):
            return ["input"]

        def plan(self, intermediate_steps, callbacks=None, **kwargs):
            if intermediate_steps and self.stop_after_step:
                return AgentFinish({"output": "done"}, "fixed completion")
            return AgentAction("PutObject", kwargs["input"], "fixed inert action")

        async def aplan(self, intermediate_steps, callbacks=None, **kwargs):
            return self.plan(intermediate_steps, callbacks, **kwargs)

    # Counts refer to this one-step tool boundary, not complete task success.
    cases = [
        ("false_predicate_allows", "false", "stop", "PutObject", "put book shelf", None, 1),
        ("true_predicate_stops", "true", "stop", "PutObject", "put book shelf", None, 0),
        ("true_predicate_skips", "true", "skip", "PutObject", "put book shelf", None, 0),
        ("approval_yes", "true", "user_inspection", "PutObject", "put book shelf", "yes", 1),
        ("approval_no", "true", "user_inspection", "PutObject", "put book shelf", "no", 0),
        ("unmatched_trigger", "true", "stop", "OtherAction", "put book shelf", None, 1),
        ("native_predicate_allowed", "is_unsafe_put", "stop", "PutObject", "put shelf", None, 1),
        ("native_predicate_denied", "is_unsafe_put", "stop", "PutObject", "put bathtub", None, 0),
    ]
    records = []
    for case_id, predicate, enforcement, trigger, payload, answer, expected in cases:
        calls, prompts = [], []

        def inert_tool(value):
            calls.append(value)
            return "in-memory receipt"

        def approval(prompt):
            prompts.append({"prompt": prompt, "tool_calls_at_prompt": len(calls)})
            if answer is None:
                raise AssertionError("Unexpected approval request")
            return answer

        rule_text = f"rule @probe\ntrigger {trigger}\ncheck {predicate}\nenforce {enforcement}\nend"
        tool = Tool(name="PutObject", description="In-memory probe only", func=inert_tool)
        executor = ControlledAgentExecutor.from_agent_and_tools(
            FixedPlanner(stop_after_step=False), [tool], [make_rule(rule_text)], max_iterations=2, verbose=False
        )
        history = []
        if predicate == "is_unsafe_put":
            held_object = "candle" if case_id.endswith("denied") else "book"
            history = [(AgentAction("PickObject", f"pick {held_object}", "fixed inert history"),
                        {"success": True, "action": f"pick {held_object}"})]
        console = io.StringIO()
        error = None
        yielded = []
        try:
            with patch("builtins.input", approval), contextlib.redirect_stdout(console):
                yielded = list(executor._iter_next_step(
                    {"PutObject": tool}, {"PutObject": "green"}, {"input": payload}, history
                ))
        except Exception:
            error = traceback.format_exc()
        prompt_ok = len(prompts) == (1 if answer is not None else 0)
        prompt_ok = prompt_ok and all(p["tool_calls_at_prompt"] == 0 for p in prompts)
        records.append({
            "id": case_id, "rule": rule_text, "input": payload,
            "history_observations": [item[1] for item in history],
            "approval_answer": answer, "approval_prompts": prompts,
            "expected_tool_calls": expected, "actual_tool_calls": len(calls),
            "tool_receipts": calls, "yielded_types": [type(item).__name__ for item in yielded],
            "pass": error is None and len(calls) == expected and prompt_ok,
            "stdout": console.getvalue(), "error": error,
        })

    # Probe full completion separately: one-step success must not hide finish errors.
    calls = []
    tool = Tool(name="PutObject", description="In-memory probe only", func=lambda v: calls.append(v) or "ok")
    executor = ControlledAgentExecutor.from_agent_and_tools(
        FixedPlanner(), [tool], [make_rule("rule @probe\ntrigger PutObject\ncheck false\nenforce stop\nend")],
        max_iterations=2, verbose=False,
    )
    completion = {"pass": False}
    try:
        result = executor.invoke({"input": "put book shelf"})
        completion.update({"pass": result.get("output") == "done", "output": result.get("output")})
    except Exception:
        completion["error"] = traceback.format_exc()
    completion["tool_calls"] = len(calls)
    report = {
        "protocol": "rr022-agentspec-feasibility-v2", "upstream_commit": sha,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "finish_compat_enabled": args.finish_compat,
        "compat_scope": "Non-finish/non-any rule returns false for finish with None; no action policy changes",
        "python": platform.python_version(), "upstream_source_modified": False,
        "preexisting_generated_bytecode": bytecode,
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "source_sha256": {
            str(p.relative_to(source)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((source / "src").rglob("*.py"))
        },
        "network": "socket connections blocked", "model_calls": 0,
        "approval": "scripted yes/no; not asynchronous durable approval",
        "tool": "in-memory append, no actual object operation",
        "cases": records, "one_step_passed": sum(r["pass"] for r in records),
        "one_step_total": len(records), "full_completion_probe": completion,
        "limits": "Feasibility only; not E2AG comparison, safety rate, performance, or full paper reproduction.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"one_step_passed": report["one_step_passed"], "one_step_total": len(records),
                      "full_completion": completion, "output": str(args.output)}, ensure_ascii=False, indent=2))
    if not all(r["pass"] for r in records):
        raise SystemExit(1)
    if not completion["pass"]:
        raise SystemExit(2)


if __name__ == "__main__":
    def no_network(*args, **kwargs):
        raise RuntimeError("Network disabled for this deterministic feasibility probe")

    with patch.object(socket.socket, "connect", no_network), patch.object(socket, "create_connection", no_network):
        main()
