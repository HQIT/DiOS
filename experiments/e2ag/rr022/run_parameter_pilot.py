"""Resource/parameter pilot through real execution gates; no model or network.

AgentSpec uses its documented grammar + predicate_table extension, generated in
an isolated directory. E2AG receives a policy snapshot at issuance. The two rule
implementations share policy data, NOT decision code. Oracles stay in the driver.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import copy
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, UPSTREAM, PIN, CATALOG, digest, file_hash
from run_paired_tool_pilot import receipt
from run_state_pilot import e2ag_fixture, grant_and_connection, invoke_proxy


def prepare_parser():
    """Mechanical grammar regeneration; no upstream checkout mutation."""
    folder = Path(tempfile.mkdtemp(prefix="agentspec-params-", dir=ROOT / "tmp/rr022-baselines"))
    target = folder / "spec_lang"
    target.mkdir()
    original = (UPSTREAM / "src/spec_lang/AgentSpec.g4").read_text(encoding="utf-8")
    assert original.count("PREDICATE: ") == 1
    grammar = original.replace("PREDICATE: ", "PREDICATE: 'rr022_argument_violation' | ", 1)
    # Generated mechanical variant only; authored source remains in this script.
    (target / "AgentSpec.g4").write_text(grammar, encoding="utf-8")
    jar = UPSTREAM / "src/spec_lang/antlr-4.13.2-complete.jar"
    def wsl_path(path):
        return subprocess.check_output(["wsl.exe", "-e", "wslpath", "-a", str(path)], text=True).strip()
    command = ["wsl.exe", "-e", "java", "-jar", wsl_path(jar), "-Dlanguage=Python3", wsl_path(target / "AgentSpec.g4")]
    run = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if run.returncode:
        raise RuntimeError(run.stdout + run.stderr)
    return folder, {"command": command, "stderr": run.stderr, "stdout": run.stdout,
                    "java": subprocess.run(["wsl.exe", "-e", "java", "-version"], capture_output=True, text=True).stderr,
                    "jar_sha256": file_hash(jar), "original_grammar_sha256": file_hash(UPSTREAM / "src/spec_lang/AgentSpec.g4"),
                    "generated_sha256": {p.name: file_hash(p) for p in sorted(target.iterdir()) if p.is_file()}}


def agentspec(job, parser_folder):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=UPSTREAM, text=True).strip()
    changes = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=UPSTREAM, text=True)
    assert sha == PIN and not [x for x in changes.splitlines() if not ("__pycache__/" in x and x.endswith(".pyc"))]
    sys.path[:0] = [str(parser_folder), str(UPSTREAM / "src")]
    from langchain.agents.agent import BaseSingleActionAgent
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.tools import Tool
    from controlled_agent_excector import ControlledAgentExecutor
    from rule import Rule
    from rules.manual.table import predicate_table
    import spec_lang.AgentSpecLexer as lexer_module
    assert Path(lexer_module.__file__).resolve().is_relative_to(parser_folder.resolve())
    compat = HERE / "run_agentspec_smoke.py"
    node = next(n for n in ast.walk(ast.parse(compat.read_text(encoding="utf-8")))
                if isinstance(n, ast.ClassDef) and n.name == "FinishCompatibleRule")
    scope = {"Rule": Rule}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(compat), "exec"), scope)
    checks = []
    policy = copy.deepcopy(job["policy"])

    def argument_violation(user_input, tool_input, intermediate_steps):
        # Independent business predicate, called ONLY by the native interpreter.
        try:
            args = json.loads(tool_input)
        except (ValueError, TypeError):
            args = None
        bad = not isinstance(args, dict) or set(args) != {"resource", "rows"}
        if not bad:
            bad = (not isinstance(args["resource"], str)
                   or args["resource"] not in policy["allowed_resources"]
                   or type(args["rows"]) is not int
                   or not 1 <= args["rows"] <= policy["max_rows"])
        checks.append({"actual_arguments": args, "violation": bad})
        return bad

    predicate_table["rr022_argument_violation"] = argument_violation

    class FixedPlanner(BaseSingleActionAgent):
        @property
        def input_keys(self):
            return ["input"]

        def plan(self, intermediate_steps, callbacks=None, **kwargs):
            if intermediate_steps:
                return AgentFinish({"output": "done"}, "fixed completion")
            call = kwargs["input"]["call"]
            return AgentAction(CATALOG[call["tool"]], json.dumps(call["arguments"]), "fixed call")

        async def aplan(self, intermediate_steps, callbacks=None, **kwargs):
            return self.plan(intermediate_steps, callbacks, **kwargs)

    receipts = []
    tools = [Tool(name=alias, description="Inert receipt",
                  func=lambda value, name=name: json.dumps(receipt(receipts, name, json.loads(value))))
             for name, alias in CATALOG.items()]
    texts = []
    for name, alias in CATALOG.items():
        texts.append(f"rule @catalog_{alias}\ntrigger {alias}\ncheck "
                     f"{'false' if name in policy['allowed_tools'] else 'true'}\nenforce stop\nend")
        texts.append(f"rule @arguments_{alias}\ntrigger {alias}\ncheck rr022_argument_violation\nenforce stop\nend")
    rules = [scope["FinishCompatibleRule"](**Rule.from_text(text).model_dump()) for text in texts]
    result = ControlledAgentExecutor.from_agent_and_tools(FixedPlanner(), tools, rules, max_iterations=2, verbose=False).invoke({"input": job["input"]})
    output = result.get("output", "")
    decision = "allow" if output == "done" else "deny" if output.startswith("action stopped by rule ") else "unclassified"
    return {"decision": decision, "tool_receipts": receipts, "predicate_evaluations": checks,
            "native_output": output, "native_rule_texts": texts, "upstream_commit": sha,
            "compat_source_sha256": file_hash(compat), "input_sha256": digest(job),
            "source_sha256": {str(p.relative_to(UPSTREAM)): file_hash(p) for p in (UPSTREAM / 'src').rglob('*.py')},
            "coverage": "documented custom predicate + native interpreter/enforcement/executor; fixed finite catalog"}


def constraints(policy):
    # Data translation only; no execution decision here.
    return {"version": 1, "tools": {name: {
        "resource": {"type": "string", "enum": policy["allowed_resources"]},
        "rows": {"type": "integer", "minimum": 1, "maximum": policy["max_rows"]},
    } for name in policy["allowed_tools"]}}


async def e2ag(job):
    async with e2ag_fixture(job) as (db, agent, server, receipts):
        from app.models.tables import A2ATask, EventLog
        from app.services.e2ag import verify_audit_chain
        data = job["input"]
        agent.capabilities = {"governance": {"allowed_tools": job["policy"]["allowed_tools"],
                                             "argument_constraints": constraints(job["policy"])}}
        event = EventLog(id=data["event"]["id"], source=data["event"]["source"], event_type=data["event"]["type"],
                         cloud_event=data["event"], trace_id=data["task"]["trace_id"], status="dispatched")
        task = A2ATask(**data["task"], context_id=event.id, message={"fixture": True})
        db.add_all([event, task])
        await db.commit()
        grant, conn = await grant_and_connection(db, agent, server, task)
        response = await invoke_proxy(db, grant, conn, data["call"])
        await db.refresh(event)
        await db.refresh(grant)
        last = event.audit_chain[-1]
        return {"decision": last["outcome"], "tool_receipts": receipts, "response": response,
                "audit_chain": event.audit_chain, "audit_verification": verify_audit_chain(event.audit_chain),
                "configured_constraints": constraints(job["policy"]),
                "stored_constraints": getattr(grant, "argument_constraints", None),
                "input_sha256": digest(job),
                "source_sha256": {str(p.relative_to(ROOT)): file_hash(p) for p in [
                    ROOT / 'backend/app/services/e2ag_tool_gateway.py', ROOT / 'backend/app/api/internal/e2ag_mcp.py',
                    ROOT / 'backend/app/models/tables.py']},
                "coverage": "native issuance/DB/proxy; preloaded task/event; inert downstream test double"}


def jobs(spec):
    for case in spec["cases"]:
        if case["id"].startswith("T"):
            data = copy.deepcopy(spec["base_input"])
            data["approval"].pop("answer", None)
            for key, value in case["overrides"].items():
                target = data
                parts = key.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
            yield case["id"], {"policy": spec["policy"], "input": data}, case["oracle"]["receipt_count"]
    base = spec["base_input"]["call"]["arguments"]
    # Diagnostic controls, NOT new independent workload samples.
    controls = [
        ("C_LOWER", {**base, "rows": 1}, 1), ("C_ZERO", {**base, "rows": 0}, 0),
        ("C_BOOL", {**base, "rows": True}, 0), ("C_FLOAT", {**base, "rows": 10.0}, 0),
        ("C_MISSING", {"resource": base["resource"]}, 0),
        ("C_UNKNOWN", {**base, "extra": 1}, 0), ("C_NULL", None, 0),
        ("C_REVERSE_ALLOW", {**base, "resource": "repo/other/report/b", "rows": 101}, 1),
        ("C_REVERSE_DENY", base, 0),
    ]
    for name, args, count in controls:
        data, policy = copy.deepcopy(spec["base_input"]), copy.deepcopy(spec["policy"])
        data["approval"].pop("answer", None)
        data["call"]["arguments"] = args
        if name.startswith("C_REVERSE"):
            policy.update(allowed_resources=["repo/other/report/b"], max_rows=101)
        yield name, {"policy": policy, "input": data}, count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "agentspec"])
    parser.add_argument("--parser-folder", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        job = json.load(sys.stdin)
        assert set(job) == {"policy", "input"}
        console = io.StringIO()
        try:
            with contextlib.redirect_stdout(console):
                result = asyncio.run(e2ag(job)) if args.worker == "e2ag" else agentspec(job, args.parser_folder)
            result["error"] = None
        except Exception:
            result = {"error": traceback.format_exc()}
        result.update(stdout=console.getvalue(), model_calls=0,
                      dependencies={d.metadata["Name"]: d.version for d in importlib.metadata.distributions()})
        print(json.dumps(result, ensure_ascii=False))
        return
    if not args.output or args.output.exists():
        raise ValueError("New output path required")
    folder, generation = prepare_parser()
    case_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(case_path.read_text(encoding="utf-8"))
    methods = {"e2ag": ROOT / "backend/.venv/Scripts/python.exe",
               "agentspec": ROOT / "tmp/rr022-baselines/.venv-agentspec/Scripts/python.exe"}
    rows = []
    for case_id, job, count in jobs(spec):
        for method, executable in methods.items():
            proc = subprocess.run([str(executable), str(Path(__file__).resolve()), "--worker", method,
                                   "--parser-folder", str(folder)], input=json.dumps(job),
                                  capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=ROOT)
            try:
                observed = json.loads(proc.stdout)
            except ValueError:
                observed = {"error": "Invalid worker output", "stdout": proc.stdout}
            passed = (proc.returncode == 0 and not observed.get("error") and observed.get("input_sha256") == digest(job)
                      and observed.get("decision") == ("allow" if count else "deny")
                      and len(observed.get("tool_receipts", [])) == count)
            rows.append({"case": case_id, "control": case_id.startswith("C_"), "method": method, "job": job,
                         "expected_receipts": count, "observed": observed, "pass": passed,
                         "exit_code": proc.returncode, "stderr": proc.stderr})
            print(json.dumps({"case": case_id, "method": method, "pass": passed,
                              "decision": observed.get("decision"), "error": observed.get("error")}, ensure_ascii=False), flush=True)
    output = {"protocol": "rr022-parameter-pilot-v1", "rows": rows, "parser_generation": generation,
              "harness_sha256": file_hash(Path(__file__)), "cases_sha256": file_hash(case_path),
              "helper_sha256": {p.name: file_hash(p) for p in [HERE / 'run_state_pilot.py', HERE / 'run_paired_tool_pilot.py']},
              "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "limits": ["Six handcrafted cases plus nine diagnostic controls; not independent workload scale.",
                         "Closed top-level string-enum/integer-range rules only; no dataflow or path resolution.",
                         "No LLM, real network endpoint, or latency measurement."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(f"{output['passed']}/{output['total']} -> {args.output}")
    if output["passed"] != output["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    main()
