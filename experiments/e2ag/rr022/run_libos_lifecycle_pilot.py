"""Native MCP-path lifecycle pairing: E2AG vs pinned Agent libOS.

Only external provider effects are inert. Native authorization is never replaced.
L03 trace mismatch is not mapped to libOS process identity and is not scored.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
from datetime import datetime, timedelta
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import traceback
from unittest.mock import patch

sys.dont_write_bytecode = True
from run_paired_tool_pilot import ROOT, HERE, CATALOG, digest, file_hash, receipt
from run_state_pilot import e2ag_lifecycle, NOW

LIBOS = ROOT / "tmp/rr022-baselines/Agent-libOS"
PIN = "85e8ff94c1e562bfb1f891714e3bf4bed9795690"


def run_libos(job):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=LIBOS, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=LIBOS, text=True)
    if sha != PIN or dirty:
        raise RuntimeError("Expected pinned clean libOS checkout")
    sys.path.insert(0, str(LIBOS))
    from agent_libos import Runtime
    from agent_libos.models import CapabilityRight, McpProviderTool
    from agent_libos.models.exceptions import CapabilityDenied
    from agent_libos.utils.serde import to_jsonable

    # Reuse pinned upstream protocol-fixture objects rather than rewrite them.
    fixture_path = LIBOS / "tests/providers/test_mcp_v2_contract.py"
    module_spec = importlib.util.spec_from_file_location("rr022_upstream_mcp_fixtures", fixture_path)
    fixtures = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(fixtures)
    receipts = []
    schema = {"type": "object", "properties": {"resource": {"type": "string"}, "rows": {"type": "integer"}},
              "required": ["resource", "rows"], "additionalProperties": False}

    class ReceiptProvider(fixtures._ModernFakeProvider):
        def __init__(self):
            super().__init__()
            self.tools = [McpProviderTool(name=name, description="In-memory receipt", input_schema=schema) for name in CATALOG]

        def validate_and_call(self, server, tool_spec, arguments, **kwargs):
            self.validate_calls += 1
            item = receipt(receipts, tool_spec.mcp_name, arguments)
            content = [{"type": "text", "text": "in-memory receipt"}]
            return fixtures._complete_call_result(
                connection=fixtures._connection(mode=server.protocol_mode),
                response_bytes=fixtures._provider_call_bytes(content, item),
                content=content, structured_content=item)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    # Freeze only evaluator clock input, NOT its comparison or decision function.
    clock = patch("agent_libos.capability.evaluator.datetime", FixedDateTime)
    work = Path(tempfile.mkdtemp(prefix="libos-lifecycle-", dir=LIBOS.parent))
    original = Path.cwd()
    runtime = None
    try:
        os.chdir(work)
        runtime = Runtime.open(":memory:")
        provider = ReceiptProvider()
        runtime.mcp.provider = provider
        manifest = fixtures._manifest(server_id="rr022-receipt", input_schema=schema)
        # Keep upstream fixture's numeric loopback URL: native preflight still
        # resolves its address even when the provider itself is an inert double.
        # No listener is started and the provider never opens that URL.
        manifest["tools"] = [{**manifest["tools"][0], "tool_id": alias, "mcp_name": name} for name, alias in CATALOG.items()]
        registered = runtime.mcp.register_server(manifest, actor="rr022-host", require_capability=False)
        pid = runtime.process.spawn(image="base-agent:v0", goal="inert report fixture")
        data = job["input"]
        expiry = NOW + timedelta(seconds=data["grant"]["expires_at"] - data["host"]["now"])
        issued = []
        with clock, patch("agent_libos.capability.mutation.utc_now", return_value=NOW.isoformat()):
            for tool in job["policy"]["allowed_tools"]:
                issued.append(runtime.capability.grant(
                    pid, f"mcp:rr022-receipt:{CATALOG[tool]}", [CapabilityRight.READ],
                    issued_by="rr022-host", expires_at=expiry.isoformat()))
            before_mutation = [to_jsonable(runtime.store.get_capability(c.cap_id)) for c in issued]
            if data["grant"]["status"] == "revoked":
                for cap in issued:
                    runtime.capability.revoke(cap.cap_id, revoked_by="rr022-host", reason="fixture revocation before call")
            before_call = [to_jsonable(runtime.store.get_capability(c.cap_id)) for c in issued]
            audit_before = runtime.store.list_audit()
            old_ids = {item.record_id for item in audit_before}
            native_result, rejection = None, None
            try:
                native_result = runtime.mcp.call_tool(pid, "rr022-receipt", CATALOG[data["call"]["tool"]], data["call"]["arguments"])
                decision = "allow" if native_result.ok else "execution_error"
            except CapabilityDenied as exc:
                rejection = {"type": type(exc).__name__, "message": str(exc)}
                decision = "deny"
            added = [to_jsonable(item) for item in runtime.store.list_audit() if item.record_id not in old_ids]
            caps_after = [to_jsonable(runtime.store.get_capability(c.cap_id)) for c in issued]
        return {"decision_sequence": [decision], "tool_receipts": receipts, "input_sha256": digest(job),
                "native_result": to_jsonable(native_result), "rejection": rejection,
                "provider_calls": {"call_tool": provider.call_calls, "validate_and_call": provider.validate_calls,
                                   "discover": provider.discover_calls, "list_tools": provider.list_calls},
                "native_manifest": registered, "native_process_id": pid,
                "capabilities_issued": before_mutation, "capabilities_before_call": before_call, "capabilities_after_call": caps_after,
                "audit_before_call_count": len(audit_before),
                "audit_after_call_count": len(runtime.store.list_audit()),
                "audit_added_by_call": added, "external_effects": to_jsonable(runtime.store.list_external_effects(pid=pid)),
                "upstream_commit": sha, "upstream_source_modified": False,
                "clock_adapter": "Evaluator datetime.now and capability issuance utc_now fixed to NOW; native comparison and state transitions unchanged. Audit wall-clock timestamps are separate.",
                "fixture_sha256": file_hash(fixture_path), "working_directory": str(work),
                "semantics": "Host bootstraps native process capability over a registered MCP tool; read-only inert tool, no external mutation",
                "source_sha256": {str(p.relative_to(LIBOS)).replace('\\', '/'): file_hash(p) for p in [
                    LIBOS / "agent_libos/primitives/mcp.py", LIBOS / "agent_libos/capability/manager.py",
                    LIBOS / "agent_libos/capability/evaluator.py", LIBOS / "agent_libos/capability/mutation.py"]}}
    finally:
        if runtime is not None:
            runtime.close()
        os.chdir(original)


def worker(method):
    job = json.load(sys.stdin)
    if set(job) != {"input", "policy"} or "answer" in job["input"]["approval"]:
        raise ValueError("Unexpected answer/oracle/case metadata in worker input")
    console = io.StringIO()
    try:
        with contextlib.redirect_stdout(console):
            result = run_libos(job) if method == "libos" else asyncio.run(e2ag_lifecycle(job))
        result["error"] = None
    except Exception:
        result = {"error": traceback.format_exc()}
    result.update({"stdout": console.getvalue(), "python": platform.python_version(),
                   "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}, "model_calls": 0})
    print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=["e2ag", "libos"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if args.output is None or args.output.exists():
        raise ValueError("Supply a new evidence filename")
    spec_path = HERE / "common-cases-v0.1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if c["id"] in {"T01", "L01", "L02"}]
    # One extra positive boundary control; not an independent business sample.
    cases.append({"id": "CTRL_BEFORE_EXPIRY", "overrides": {"grant.expires_at": 1001},
                  "oracle": {"decision_sequence": ["allow"], "receipt_count": 1}})
    methods = {"e2ag": ROOT / "backend/.venv/Scripts/python.exe", "libos": ROOT / "tmp/rr022-baselines/.venv-libos/Scripts/python.exe"}
    rows = []
    for case in cases:
        data = copy.deepcopy(spec["base_input"])
        for key, value in case["overrides"].items():
            parts, target = key.split("."), data
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        data["approval"].pop("answer")
        job = {"policy": spec["policy"], "input": data}
        for method, python in methods.items():
            proc = subprocess.run([str(python), str(Path(__file__).resolve()), "--worker", method],
                                  input=json.dumps(job), text=True, encoding="utf-8", capture_output=True, cwd=ROOT, timeout=90)
            try:
                observed = json.loads(proc.stdout)
            except ValueError:
                observed = {"error": "worker output not JSON", "stdout": proc.stdout}
            passed = (proc.returncode == 0 and observed.get("error") is None
                      and observed.get("decision_sequence") == case["oracle"]["decision_sequence"]
                      and len(observed.get("tool_receipts", [])) == case["oracle"]["receipt_count"]
                      and observed.get("input_sha256") == digest(job))
            rows.append({"case": case["id"], "method": method, "job": job, "expected": case["oracle"],
                         "observed": observed, "stderr": proc.stderr, "pass": passed})
            print(json.dumps({"case": case["id"], "method": method, "pass": passed,
                              "decision": observed.get("decision_sequence"), "error": observed.get("error")}, ensure_ascii=False), flush=True)
    report = {"protocol": "rr022-libos-lifecycle-pilot-v1", "harness_sha256": file_hash(Path(__file__)),
              "cases_sha256": file_hash(spec_path), "shared_harness_sha256": file_hash(HERE / "run_state_pilot.py"),
              "receipt_helper_sha256": file_hash(HERE / "run_paired_tool_pilot.py"),
              "e2ag_source_sha256": {str(p.relative_to(ROOT)).replace('\\', '/'): file_hash(p) for p in [
                  ROOT / "backend/app/services/e2ag_tool_gateway.py", ROOT / "backend/app/api/internal/e2ag_mcp.py"]},
              "rows": rows, "passed": sum(r["pass"] for r in rows), "total": len(rows),
              "coverage": "T01/L01/L02 paired, plus one pre-expiry fixture control; L03 not comparable without separate binding protocol",
              "limits": "No real network/model. Pinned upstream protocol fixture receipts contain synthetic byte/time values; never use for performance. Native authority and MCP entry points used; no lifecycle surrogate decision."}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    if not all(r["pass"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ["LANGSMITH_TRACING"] = "false"
    main()
