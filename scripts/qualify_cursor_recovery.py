"""Recover a deliberate qualification crash using the original durable output.

Only accepts a recovery-target created by qualify_harness_governance.py in an
isolated workspace. Never submits or starts an Agent; waits for the actual lease.
"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "sdk/python/src")]

from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database, RuntimeErrorCode
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.reconciliation import ExitReceipts
from app.runtime.schema import artifacts
from app.runtime.store import RuntimeStore, encoded
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import Worker, read_profile
from sqlalchemy import select


async def recover(directory, verify_completed=False):
    directory = directory.resolve()
    if not directory.is_relative_to(ROOT / "workspace"):
        raise ValueError("Qualification output must be in repository workspace")
    target = json.loads((directory / "recovery-target.json").read_text())
    before, scope = target["run"], target["scope"]
    assert scope.startswith("harness-") and target["worker_pid"] != os.getpid()
    assert not Path(f'/proc/{target["worker_pid"]}').exists(), "Original Worker still present"
    db = Database("sqlite+aiosqlite:///" + str(directory / "runtime.sqlite"))
    store = RuntimeStore(db, scope)
    profile_id, spec = read_profile(directory / "profile.json")
    adapter = DockerRuntimeAdapter(scope)
    provider = LocalArtifacts(directory / "objects")
    starts = 0

    async def no_start(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        raise AssertionError("Recovery attempted a new model launch")

    adapter.start = no_start
    worker = Worker(store, adapter, profile_id, spec, provider,
        authorization_verifier=WorkAuthorizationVerifier(store, FileWorkPolicy(directory / "policy.json")))
    attempt = before["attempts"][0]
    binding = (before["id"], attempt["id"], attempt["worker_id"], attempt["worker_instance"], attempt["epoch"])
    try:
        first = None
        # An observer failure must preserve the original task and retained bytes.
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if not verify_completed:
                await store.expire()
            current = await store.get(before["id"])
            if verify_completed:
                assert current["state"] == "succeeded", "Readback requires completed recovery"
                break
            if current["state"] == "reconciliation_required":
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("Original lease has not expired; no replacement launched")
        if not verify_completed:
            original_observe = adapter.observe
            async def timeout(_handle):
                raise TimeoutError("Injected observation timeout against exited native workload")
            adapter.observe = timeout
            first = await worker.recover()
            assert first[0]["error"] == "OBSERVATION_UNAVAILABLE"
            unchanged = await store.get(before["id"])
            assert unchanged["attempt_id"] == attempt["id"] and len(unchanged["attempts"]) == 1
            adapter.observe = original_observe
            recovered = await worker.recover()
            assert recovered[0]["state"] == "succeeded", recovered
        final = await store.get(before["id"])
        assert final["attempt_id"] == attempt["id"] and len(final["attempts"]) == 1
        assert final["attempts"][0]["session_id"] == attempt["session_id"]
        try:
            await store.heartbeat(*binding)
        except RuntimeErrorCode as exc:
            assert exc.code == "STALE_OWNER", exc.code
        else:
            raise AssertionError("Old Worker authority accepted")
        receipt = ExitReceipts(provider, scope).load(final["attempts"][0])
        raw = b"".join(provider.read(receipt["stdout"]))
        frames = [json.loads(line) for line in raw.splitlines() if line.startswith(b"{")]
        assert sum(frame.get("type") == "result" for frame in frames) == 1
        async with db.read(scope) as connection:
            linked = (await connection.execute(select(artifacts).where(artifacts.c.run_id == before["id"]))).mappings().all()
        result_frame = next(frame["result"] for frame in frames if frame.get("type") == "result")
        expected_result = hashlib.sha256(encoded(result_frame)).hexdigest()
        assert {receipt["stdout"], receipt["stderr"], expected_result} <= {item["storage_key"] for item in linked}
        for item in linked:
            provider.verify(item["storage_key"])
            assert item["attempt_id"] == attempt["id"]
        assert starts == 0 and target["tool_calls"] == 1
        result = {"passed": True, "run_id": final["id"], "attempt_id": attempt["id"],
            "session_id": attempt["session_id"], "state": final["state"],
            "old_worker_pid": target["worker_pid"], "verification_pid": os.getpid(),
            "verification_only": verify_completed,
            "model_launches_during_recovery": starts, "tool_calls": target["tool_calls"],
            "original_task_attempt_session_preserved": True, "observation_timeout": first,
            "old_owner_error": "STALE_OWNER", "artifact_count": len(linked),
            "stdout_sha256": hashlib.sha256(raw).hexdigest(), "image": spec.image}
        (directory / "recovery-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--verify-completed", action="store_true")
    args = parser.parse_args()
    asyncio.run(recover(args.directory, args.verify_completed))
