"""Isolated Docker/Worker deadline and rolling-window restart qualification."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.runtime.capacity import Capacity
from app.runtime.database import Database
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.store import RuntimeStore
from app.runtime.work_policy import FileWorkPolicy
from app.runtime.worker import read_profile


async def qualify(output, url=None):
    output.mkdir(parents=True, exist_ok=False)
    url = url or "sqlite+aiosqlite:///" + str(output / "state.sqlite")
    scope = "budget-qualification-" + str(uuid4())
    db = Database(url)
    await db.migrate()
    store = RuntimeStore(db, scope)
    capacity = Capacity(store)
    adapter = DockerRuntimeAdapter(scope)
    children, logs, all_pids = [], [], []
    profile = json.loads((ROOT / "examples/runtime/fixture-profile.json").read_text())
    profile["governance"] = {"mode": "e2ag"}
    profile["launch"]["image"] = adapter.client.images.get(
        profile["launch"]["image"]
    ).id
    profile_path = output / "profile.json"
    profile_path.write_text(json.dumps(profile))
    profile_id, spec = read_profile(profile_path)
    permissions = [
        {
            "action": "runtime." + action,
            "resource_kind": "runtime_profile",
            "resource_id": profile_id,
            "resource_version": spec.fingerprint(),
            "constraint_revision": "budget-qualification",
        }
        for action in ("execute", "accept_result")
    ]
    policy_path = output / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "policy_id": "budget-qualification",
                "subject": "qualification:operator",
                "actor": "qualification:worker",
                "profiles": {
                    profile_id: {
                        "profile_digest": spec.fingerprint(),
                        "secret_refs": [],
                        "work_ttl_seconds": 3600,
                        "permissions": {
                            layer: permissions
                            for layer in ("caller", "service", "workload", "resource")
                        },
                    }
                },
            }
        )
    )
    authorization = FileWorkPolicy(policy_path).admission(
        profile_id, spec.fingerprint(), []
    )

    async def start():
        for _ in range(2):
            log = (output / f"worker-{len(all_pids)}.log").open("w")
            logs.append(log)
            p = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.runtime.cli",
                "--scope",
                scope,
                "worker",
                "--profile",
                str(profile_path),
                "--policy-file",
                str(policy_path),
                "--artifacts",
                str(output / "artifacts"),
                "--poll-interval",
                ".1",
                cwd=ROOT / "backend",
                env=dict(os.environ, DIOS_RUNTIME_DATABASE_URL=url),
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
            )
            children.append(p)
            all_pids.append(p.pid)

    async def stop():
        for p in children:
            if p.returncode is None:
                p.terminate()
        for p in children:
            try:
                await asyncio.wait_for(p.wait(), 15)
            except TimeoutError:
                p.kill()
                await p.wait()
        children.clear()

    async def until(check, seconds=40):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            assert all(p.returncode is None for p in children), "Worker exited"
            value = await check()
            if value:
                return value
            await asyncio.sleep(0.15)
        raise RuntimeError("BUDGET_QUALIFICATION_DEADLINE")

    async def terminal(run_id, state):
        run = await store.get(run_id)
        return run if run["state"] == state else None

    try:
        await capacity.put(
            "shared",
            {"kind": "scope"},
            2,
            0,
            budget={
                "max_runtime_seconds": 4,
                "max_dispatches": 1,
                "dispatch_window_seconds": 12,
            },
        )
        slow = await store.submit(
            "slow",
            {
                "runtime_profile": profile_id,
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"delay": 30},
            },
            authorization=authorization,
        )
        fast = await store.submit(
            "fast",
            {
                "runtime_profile": profile_id,
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
                "payload": {"delay": 0.1},
            },
            authorization=authorization,
        )
        await start()

        async def running():
            status = await store.runtime_status()
            return status if status["actual_concurrency"] == 1 else None

        observed = await until(running)
        assert (await store.get(fast["id"]))["state"] == "queued"
        failed = await until(lambda: terminal(slow["id"], "failed"))
        assert failed["attempts"][0]["error"]["code"] == "RUN_BUDGET_EXHAUSTED"
        assert failed["attempts"][0]["remote"]["runtime"]["alive"] is False
        assert (await capacity.status())["items"][0]["reserved_attempts"] == 0
        await stop()
        before = await store.get(fast["id"])
        assert before["state"] == "queued" and not before["attempts"]
        saved = await capacity.status()
        assert saved["items"][0]["dispatch_rate"]["used"] == 1
        await start()
        completed = await until(lambda: terminal(fast["id"], "succeeded"))
        a, b = failed["attempts"][0], completed["attempts"][0]
        assert b["started_at"] - a["started_at"] >= 12
        assert len(failed["attempts"]) == len(completed["attempts"]) == 1
        assert len(set(all_pids)) == 4
        result = {
            "provider": db.engine.dialect.name,
            "scope": scope,
            "worker_process_ids": all_pids,
            "observed": observed,
            "deadline_failure": {
                "run_id": slow["id"],
                "attempt_id": a["id"],
                "error": a["error"],
                "budget": failed["execution_budget"],
                "confirmed_exit": True,
            },
            "successful_run": fast["id"],
            "claim_interval_seconds": b["started_at"] - a["started_at"],
            "restart_preserved_window": True,
            "final_reserved_attempts": (await capacity.status())["items"][0][
                "reserved_attempts"
            ],
        }
        assert result["final_reserved_attempts"] == 0
        (output / "evidence.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    finally:
        await stop()
        for log in logs:
            log.close()
        for container in adapter.client.containers.list(
            all=True, filters={"label": "dios.runtime.scope=" + scope}
        ):
            container.remove(force=True)
        adapter.client.close()
        await db.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--postgres", action="store_true")
    args = p.parse_args()
    report = asyncio.run(
        qualify(
            args.output.resolve(),
            os.environ["DIOS_TEST_POSTGRES_URL"] if args.postgres else None,
        )
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "provider",
                    "claim_interval_seconds",
                    "restart_preserved_window",
                    "final_reserved_attempts",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
