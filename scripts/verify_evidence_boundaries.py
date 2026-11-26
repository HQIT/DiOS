"""Recalculate published synthetic boundary measurements without running Workers."""

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.runtime.evidence_report import distribution, summarize


def verify(samples, expected):
    metrics = {}
    for name, data in samples.items():
        report = summarize(data["capture"])
        target = expected["arms"][name]
        assert report == target["report"], name
        assert len({x["pid"] for x in data["workers"]}) == 2
        assert len({x["process_id"] for x in data["process_identities"]}) == 2
        assert len({x["session_id"] for x in data["process_identities"]}) == 2
        assert report["throughput"]["counts"] == {"succeeded": 2}
        assert max(x["actual_concurrency"] or 0 for x in data["observations"]) == 2
        metrics[name] = {
            "policy_refresh": report["governance_seconds"]["policy_refresh_seconds"],
            "protocol_read": distribution(
                [v for w in data["workers"] for v in w["protocol_read_seconds"]]
            ),
            "storage_wait": distribution(
                [t["wait_seconds"] for w in data["workers"] for t in w["transactions"]]
            ),
            "execution": distribution(
                [w["execution_seconds"] for w in data["workers"]]
            ),
        }
        for metric, value in metrics[name].items():
            assert value == target[metric], (name, metric)
    for name, contrast in expected["contrasts"].items():
        key = contrast["metric"]
        difference = (
            metrics[name][key]["p50_seconds"] - metrics["baseline"][key]["p50_seconds"]
        )
        assert difference == contrast["p50_increase_seconds"]
        assert difference >= contrast["minimum"] and contrast["passed"]
    return {"passed": True, "arms": len(metrics), "completed_runs": 10, "contrasts": 4}


if __name__ == "__main__":
    evidence = ROOT / "docs/design/evidence"
    samples = json.loads(
        (evidence / "evidence-boundary-samples-20261126.json").read_text()
    )
    expected = json.loads(
        (evidence / "evidence-completeness-20261126.json").read_text()
    )["native"]
    print(json.dumps(verify(samples, expected)))
