"""Read existing DB snapshots; test semantic edits only in memory. No services."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from app.services.e2ag_evidence import verify_execution_evidence
from test_e2ag_evidence import fixture, rechain, semantic_mutations


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_snapshots(path):
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row

        def rows(query, values=()):
            return [dict(row) for row in db.execute(query, values)]

        for event in rows("select id,trace_id,matched_agent_ids,contract_decision,policy_decision,audit_chain from event_logs order by id"):
            for key in ("matched_agent_ids", "contract_decision", "policy_decision", "audit_chain"):
                event[key] = json.loads(event[key])
            grants = rows("select id,trace_id,event_log_id,task_id,agent_id,mcp_server_id,allowed_tools,argument_constraints from e2ag_tool_grants where event_log_id=?", (event["id"],))
            for grant in grants:
                for key in ("allowed_tools", "argument_constraints"):
                    grant[key] = json.loads(grant[key]) if grant[key] is not None else None
            yield dict(event=event,
                       tasks=rows("select id,trace_id,context_id,agent_id,status from a2a_tasks where context_id=?", (event["id"],)),
                       grants=grants,
                       invocations=rows("select id,trace_id,event_log_id,task_id,grant_id,method,tool_name,request_sha256,state,upstream_status,response_sha256 from e2ag_tool_invocations where event_log_id=?", (event["id"],)),
                       approvals=rows("select id,event_log_id,trace_id,status from e2ag_approvals where event_log_id=?", (event["id"],)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    output = dict(scope="Offline enforced POST tools/call evidence relationships, not a complete I4 or physical-execution proof",
                  manifest_sha256=digest(args.manifest), snapshots=[], semantic_counterexamples=[])
    for row in manifest["rows"]:
        path = Path(row["cell"]["directory"]) / "state.db"
        before = digest(path)
        results = [dict(event_id=s["event"]["id"], result=verify_execution_evidence(s)) for s in read_snapshots(path)]
        output["snapshots"].append(dict(case=row["fault"], database=str(path.relative_to(ROOT)),
                                        sha256=before, unchanged=before == digest(path), results=results))
    for name, edit, expected in semantic_mutations():
        s = fixture()
        edit(s)
        result = verify_execution_evidence(rechain(s))
        passed = result["hash_valid"] and not result["valid"] and any(e.startswith(expected) for e in result["errors"])
        output["semantic_counterexamples"].append(dict(case=name, expected=expected, passed=passed, result=result))
    sources = [Path(__file__), ROOT / "backend/app/services/e2ag_evidence.py",
               ROOT / "backend/app/services/e2ag.py", ROOT / "backend/app/services/e2ag_arguments.py",
               ROOT / "backend/tests/test_e2ag_evidence.py"]
    output["source_sha256"] = {str(p.relative_to(ROOT)): digest(p) for p in sources}
    output["passed"] = (bool(output["snapshots"]) and
                        all(s["unchanged"] and s["results"] and all(r["result"]["valid"] for r in s["results"]) for s in output["snapshots"]) and
                        all(r["passed"] for r in output["semantic_counterexamples"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(output, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
