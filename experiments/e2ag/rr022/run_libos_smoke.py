"""Run a small, explicit subset of unmodified Agent libOS native tests offline.

This checks installation and API feasibility, not comparative effectiveness.
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
import tempfile
import threading
from unittest.mock import patch

PIN = "85e8ff94c1e562bfb1f891714e3bf4bed9795690"
TESTS = [
    "test_typed_resource_matching_rejects_prefix_collision",
    "test_deny_dominates_matching_allow",
    "test_ask_dominates_allow_across_issuance_order",
    "test_exact_one_shot_approval_binding_resolves_ask_and_is_selected_for_consumption",
    "test_stale_or_other_operation_approval_binding_cannot_resolve_ask",
    "test_revoke_requires_holder_issuer_or_revoke_authority",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    upstream, output = args.upstream.resolve(), args.output.resolve()
    if output.exists():
        raise FileExistsError("Use a fresh output filename")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=upstream, text=True)
    if sha != PIN or dirty:
        raise RuntimeError("Require pinned clean upstream source")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(upstream))
    import pytest
    work = Path(tempfile.mkdtemp(prefix="libos-run-", dir=upstream.parent)).resolve()
    prefix = str(upstream / "tests/security/test_capability.py") + "::TestCapabilityManager::"
    test_args = [prefix + name for name in TESTS] + [
        "-q", "--skip-real-deno", "--keep-agent-outputs", "--fail-on-skip",
        f"--basetemp={work / 'pytest'}", "-o", f"cache_dir={work / 'pytest-cache'}",
        f"--junitxml={work / 'junit.xml'}",
    ]
    results = []

    class Recorder:
        def pytest_runtest_logreport(self, report):
            if report.when == "call" or report.failed or report.skipped:
                results.append({"nodeid": report.nodeid, "phase": report.when,
                                "outcome": report.outcome, "detail": str(report.longrepr) if report.longrepr else None})

    def no_network(*args, **kwargs):
        raise RuntimeError("Network disabled during native capability smoke tests")

    # Windows asyncio builds its wakeup channel through a loopback socketpair.
    # Permit only connections created within that standard-library operation.
    channel = threading.local()
    original_pair = socket.socketpair
    original_connect = socket.socket.connect

    def local_pair(*args, **kwargs):
        prior = getattr(channel, "creating_pair", False)
        channel.creating_pair = True
        try:
            return original_pair(*args, **kwargs)
        finally:
            channel.creating_pair = prior

    def guarded_connect(sock, address):
        if getattr(channel, "creating_pair", False) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return no_network()

    console = io.StringIO()
    original = Path.cwd()
    try:
        os.chdir(work)
        with patch.object(socket.socket, "connect", guarded_connect), patch.object(socket, "create_connection", no_network), patch.object(socket, "socketpair", local_pair):
            with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
                code = int(pytest.main(test_args, plugins=[Recorder()]))
    finally:
        os.chdir(original)
    text = console.getvalue()
    report = {
        "protocol": "rr022-libos-native-feasibility-v2", "upstream_commit": sha,
        "python": platform.python_version(), "upstream_source_modified": False,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "test_source_sha256": hashlib.sha256((upstream / "tests/security/test_capability.py").read_bytes()).hexdigest(),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "dependency_scope": "Declared core ranges resolved via China mirror; not upstream uv.lock reproduction",
        "network": "socket connects blocked except Windows asyncio internal loopback socketpair", "model_calls": 0,
        "test_selectors": TESTS, "exit_code": code, "test_results": results,
        "working_directory": str(work), "console": text,
        "limits": "Selected native tests only; no E2AG comparison, throughput, or broad generality claim.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text[-6000:])
    print(json.dumps({"exit_code": code, "recorded_tests": len(results), "output": str(output)}, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
