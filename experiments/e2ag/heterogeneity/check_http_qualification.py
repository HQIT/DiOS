"""Raw database and actual client/server records for HTTP qualification."""
import argparse
import hashlib
import json
from pathlib import Path

from check_file_recovery import check
from check_paired_matrix import tables, lines, load

COMMON = {"normal", "deny", "pending", "approved", "tool", "resource", "revoked", "expired"}
CUTS = {"before_receive_commit", "after_receive_commit", "after_handoff_commit"}
REJECTED = {"wrong_signature", "unconfigured_endpoint", "bad_json", "missing_field", "reserved_source"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    result = check(root, expected_names=COMMON | CUTS | REJECTED | {"identity_conflict"})
    for name in sorted(COMMON | CUTS | REJECTED | {"identity_conflict"}):
        folder = root / name
        raw = tables(folder, "sqlite")
        wire, inputs = lines(folder / "http-client.jsonl"), lines(folder / "http-inputs.jsonl")
        if name in REJECTED:
            status = 403 if name in {"wrong_signature", "unconfigured_endpoint"} else 400
            if len(wire) != 1 or wire[0].get("status") != status or inputs or any(raw.values()):
                result["failures"].append(name + ":rejection_did_not_precede_handoff")
            continue
        if len(raw["events"]) != 1:
            result["failures"].append(name + ":event_count")
            continue
        cloud = raw["events"][0]["cloud_event"]
        if cloud["source"] != "http/fixture" or cloud["type"] != "http.received":
            result["failures"].append(name + ":source_binding")
        for received in inputs:
            if not received["signature_verified"] or not any(w["request_sha256"] == received["request_sha256"] for w in wire):
                result["failures"].append(name + ":missing_wire_input")
        if name in CUTS:
            if len(wire) != 3 or "transport_error" not in wire[0] or wire[-1].get("response", {}).get("disposition") != "duplicate":
                result["failures"].append(name + ":lost_reply_recovery")
        elif name == "identity_conflict":
            if len(wire) != 3 or wire[-1].get("status") != 409 or len(raw["conflicts"]) != 1:
                result["failures"].append(name + ":conflict_not_explicit")
        elif len(wire) != 2 or wire[-1].get("response", {}).get("disposition") != "duplicate":
            result["failures"].append(name + ":repeat_not_suppressed")
    result["status"] = "passed" if not result["failures"] else "failed"
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"status": result["status"], "failures": result["failures"]}))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
