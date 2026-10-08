"""Read native DB/receipts plus subprocess protocol/lifecycle independently."""
import argparse
import hashlib
import json
from pathlib import Path

from check_file_recovery import check, lines, load

MODES = {"normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"}


def audit(directory):
    result = check(directory, expected_names=MODES)
    expected_reasons = {"tool": "MCP_TOOL_NOT_GRANTED", "resource": "MCP_ARGUMENT_ENUM_DENIED",
                        "revoked": "TOOL_GRANT_NOT_ACTIVE", "expired": "TOOL_GRANT_EXPIRED"}
    for mode in sorted(MODES):
        folder = directory / mode
        row = load(directory / (mode + ".json"))
        if "snapshot" not in row:
            continue
        messages = lines(folder / "stdio-messages.jsonl")
        processes = lines(folder / "stdio-processes.jsonl")
        methods = [m["payload"]["method"] for m in messages]
        expected = [] if mode in {"deny", "pending"} else ["initialize", "notifications/initialized"]
        if mode in {"normal", "approved"}: expected += ["tools/call"]
        if methods != expected: result["failures"].append(mode + ":pipe_method_sequence")
        if expected:
            pids = {m["pid"] for m in messages}
            if len(pids) != 1: result["failures"].append(mode + ":session_child_binding")
            if [p["phase"] for p in processes] != ["started", "exited"]:
                result["failures"].append(mode + ":child_lifecycle")
        elif processes:
            result["failures"].append(mode + ":unexpected_child")
        if mode in expected_reasons:
            reason = row["snapshot"]["tasks"][0]["artifacts"][0]["response"]["error"]["data"]["reason"]
            if reason != expected_reasons[mode]: result["failures"].append(mode + ":denial_reason")
    result["status"] = "passed" if not result["failures"] else "failed"
    result["scope"] += "; real stdio frames, child PID and lifecycle records"
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit("Refusing overwrite")
    result = audit(args.directory.resolve())
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "failures": result["failures"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)
