"""Finite differential audit of the exact compatibility class used by smoke.

Uses Rule objects directly, not a claim that every event token is DSL-parseable.
No tools, model calls, policy extensions, or upstream source edits.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys


def observe(rule, action, value):
    try:
        return {"return": rule.triggered(action, value)}
    except Exception as error:
        return {"error_type": type(error).__name__, "message": str(error)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    upstream = args.upstream.resolve()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    if commit != "e6fa3902e2cfb9681f454b355691b771f70543f8":
        raise RuntimeError("Unexpected upstream version")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(upstream / "src"))
    from rule import Rule
    source = Path(__file__).with_name("run_agentspec_smoke.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "FinishCompatibleRule"]
    if len(classes) != 1:
        raise RuntimeError("Ambiguous compatibility class")
    # Load the exact locally authored class, rather than a second reimplementation.
    namespace = {"Rule": Rule}
    module = ast.fix_missing_locations(ast.Module(body=classes, type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    Compat = namespace["FinishCompatibleRule"]
    records = []
    for event, action, value in itertools.product(
        ["PutObject", "OtherAction", "finish", "any"],
        ["PutObject", "OtherAction", "finish"],
        [None, "", "PutObject payload", {"resource": "fixture"}],
    ):
        native = observe(Rule(id="audit", event=event, raw=""), action, value)
        adapted = observe(Compat(id="audit", event=event, raw=""), action, value)
        intended = action == "finish" and value is None and event not in {"finish", "any"}
        passed = (native.get("error_type") == "AttributeError" and adapted == {"return": False}) if intended else native == adapted
        records.append({"event": event, "action": action, "input": value,
                        "native": native, "adapted": adapted,
                        "intended_change": intended, "pass": passed})
    report = {
        "protocol": "agentspec-finish-compat-differential-v1", "upstream_commit": commit,
        "smoke_script_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "compat_class_sha256": hashlib.sha256(ast.unparse(classes[0]).encode()).hexdigest(),
        "audit_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cases": records, "total": len(records), "passed": sum(r["pass"] for r in records),
        "intended_changes": sum(r["intended_change"] for r in records),
        "limits": "Finite API-level boundary checks only, not exhaustive proof or security-effectiveness evidence.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("total", "passed", "intended_changes")}, indent=2))
    if report["passed"] != report["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
