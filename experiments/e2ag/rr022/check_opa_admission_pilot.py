"""Check native approval checkpoints, paired effects, and independent PDP data."""
import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, file_hash, digest
sys.path.insert(0, str(ROOT / "backend"))
from app.services.e2ag import verify_audit_chain


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--result", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    assert not a.output.exists()
    data = json.loads(a.result.read_text(encoding="utf-8"))
    rows = {(r["case"], r["method"]): r for r in data["rows"]}
    assert len(rows) == 12 and all(r["pass"] for r in rows.values())
    for case in {key[0] for key in rows}:
        native, opa = rows[case, "e2ag"], rows[case, "opa"]
        assert native["job"] == opa["job"]
        x, y = native["observed"], opa["observed"]
        assert x["decision_sequence"] == y["decision_sequence"]
        assert x["created_tasks"] == y["created_tasks"]
        assert x["tool_receipts"] == y["tool_receipts"]
        for row in (native, opa):
            out = row["observed"]
            assert out["input_sha256"] == digest(row["job"])
            assert "answer" not in row["job"]["input"]["approval"]
            if case.startswith("A"):
                assert out["pre_answer"]["created_tasks"] == out["pre_answer"]["receipt_count"] == 0
                assert verify_audit_chain(out["pre_answer"]["audit_chain"])
                events = [m["type"] for m in row["pipe_trace"]]
                assert events[0] == "approval_requested"
                if case == "A01":
                    assert events == ["approval_requested"] and out["pending_observed"]
                else:
                    assert events == ["approval_requested", "harness_answer_delivered", "result"]
                    assert out["duplicate_reply"] == "APPROVAL_ALREADY_DECIDED"
            else:
                assert not any(m["type"] == "approval_requested" for m in row["pipe_trace"])
            if case != "A01":
                assert verify_audit_chain(out["audit_chain"])
                for receipt in out["tool_receipts"]:
                    assert receipt["arguments"] == row["job"]["input"]["call"]["arguments"]
        if case != "A01":
            assert len(y["admission_evaluations"]) == 2
            for evaluation in y["admission_evaluations"]:
                ctx = evaluation["input"]
                assert ctx["event"] == y["native_event"]
                assert "expected" not in ctx and "oracle" not in ctx and "answer" not in ctx
            assert len(y["call_evaluations"]) == (2 if y["created_tasks"] else 0)
        if case in {"E02", "C_HARD_DENY_BEFORE_APPROVAL"}:
            assert x["approval_count"] == y["approval_count"] == 0
    sources = [HERE / "run_opa_admission_pilot.py", HERE / "opa_admission.rego", HERE / "run_opa_audited_pilot.py",
               HERE / "run_opa_context_pilot.py", HERE / "opa_call_context.rego", HERE / "run_state_pilot.py"]
    sources += [ROOT / "backend/app/services" / n for n in ["e2ag.py", "e2ag_approval.py", "event_dispatcher.py", "e2ag_tool_gateway.py", "e2ag_policy_errors.py"]]
    output = {"paired_checks_passed": True, "main_cases": 5, "diagnostic_controls": 1,
              "approval_answers_after_pending_only": True, "hard_deny_created_no_approval": True,
              "result_sha256": file_hash(a.result), "source_sha256": {str(f.relative_to(ROOT)): file_hash(f) for f in sources},
              "checker_sha256": file_hash(Path(__file__)), "not_covered": data["not_covered"]}
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps({"checked": len(rows), "pass": True}))


if __name__ == "__main__":
    main()
