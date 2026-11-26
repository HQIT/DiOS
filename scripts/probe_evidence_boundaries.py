"""Isolated, controlled boundary experiment with real Linux Workers and containers.

Each arm changes one delay. The workload emits synthetic protocol/usage records;
there is no model call. Production policy, database and Worker implementations
are exercised; measurement wrappers and fault injection live only in this probe.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "backend/tests")]
from sqlalchemy import text
from app.runtime.adapter import LaunchSpec
from app.runtime.artifacts import LocalArtifacts
from app.runtime.database import Database
from app.runtime.docker_adapter import DockerRuntimeAdapter
from app.runtime.evidence_report import capture, distribution, summarize
from app.runtime.overview import view as overview
from app.runtime.schema import runs
from app.runtime.store import RuntimeStore
from app.runtime.worker import Worker
from app.runtime.work_authorizations import WorkAuthorizationVerifier
from app.runtime.work_policy import FileWorkPolicy
from work_auth_fixtures import policy_fixture


def save(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


class MeasuredDatabase(Database):
    def __init__(self, url, delay):
        super().__init__(url)
        self.delay, self.samples = delay, []

    @asynccontextmanager
    async def transaction(self, scope, *, check=True):
        blocker, release = None, None
        if self.delay:
            blocker = await self.engine.connect()
            await blocker.execute(text("BEGIN IMMEDIATE"))

            async def unlock():
                try:
                    await asyncio.sleep(self.delay)
                    await blocker.rollback()
                finally:
                    await blocker.close()

            release = asyncio.create_task(unlock())
        start = time.monotonic()
        entered = body_done = None
        ok = False
        try:
            async with super().transaction(scope, check=check) as c:
                entered = time.monotonic()
                try:
                    yield c
                finally:
                    body_done = time.monotonic()
            ok = True
        finally:
            end = time.monotonic()
            if release:
                await release
            self.samples.append(
                {
                    "wait_seconds": entered - start if entered else None,
                    "body_seconds": body_done - entered
                    if body_done and entered
                    else None,
                    "commit_and_cleanup_seconds": end - body_done
                    if body_done
                    else None,
                    "succeeded": ok,
                }
            )


class MeasuredPolicy(FileWorkPolicy):
    def __init__(self, path, delay):
        super().__init__(path)
        self.delay = delay

    def admission(self, *args, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        return super().admission(*args, **kwargs)


class MeasuredAdapter(DockerRuntimeAdapter):
    def __init__(self, scope, delay):
        super().__init__(scope)
        self.delay, self.samples = delay, []
        self.execution_seconds = None

    async def logs(self, handle, max_bytes):
        start = time.monotonic()
        if self.delay:
            await asyncio.sleep(self.delay)
        result = await super().logs(handle, max_bytes)
        self.samples.append(time.monotonic() - start)
        # Probe-owned in-process span, separate from control-plane receipt time.
        # stderr is synthetic and never contains a real model response or secret.
        for line in result.stderr.splitlines():
            try:
                value = json.loads(line).get("probe_execution_seconds")
                if type(value) in (int, float) and 0 <= value <= 60:
                    self.execution_seconds = value
            except (ValueError, AttributeError):
                pass
        return result


async def child(config, slot):
    value = json.loads(config.read_text())
    directory = config.parent
    db = MeasuredDatabase(value["database_url"], value["storage_delay"])
    adapter = MeasuredAdapter(value["scope"], value["protocol_delay"])
    store = RuntimeStore(db, value["scope"])
    spec = LaunchSpec(
        **{**value["spec"], "mounts": (), "command": tuple(value["spec"]["command"])}
    )
    policy = MeasuredPolicy(directory / "work-policy.json", value["policy_delay"])
    worker = Worker(
        store,
        adapter,
        "evidence-probe",
        spec,
        LocalArtifacts(directory / "artifacts"),
        poll_interval=0.05,
        authorization_verifier=WorkAuthorizationVerifier(store, policy),
    )
    try:
        result = await worker.once()
        assert result["state"] == "succeeded", result.get("state")
        save(
            directory / f"worker-{slot}.json",
            {
                "pid": os.getpid(),
                "worker": worker.worker_id,
                "run": result["id"],
                "state": result["state"],
                "transactions": db.samples,
                "protocol_read_seconds": adapter.samples,
                "execution_seconds": adapter.execution_seconds,
            },
        )
    finally:
        adapter.client.close()
        await db.close()


async def arm(root, name, image):
    directory = root / name
    directory.mkdir(mode=0o700)
    delay = {
        "policy_delay": 0.15 if name == "policy" else 0,
        "protocol_delay": 0.15 if name == "protocol" else 0,
        "storage_delay": 0.15 if name == "storage" else 0,
    }
    execution_seconds = 5 if name == "execution" else 2
    program = "\n".join(
        [
            "import json,time,uuid,sys",
            "sid=str(uuid.uuid4())",
            "def emit(v): print(json.dumps(v),flush=True)",
            "emit({'type':'session','session_id':sid})",
            "emit({'type':'output','text':'synthetic boundary workload'})",
            "execution_start=time.monotonic()",
            f"time.sleep({execution_seconds})",
            "print(json.dumps({'probe_execution_seconds':time.monotonic()-execution_start}),file=sys.stderr,flush=True)",
            "emit({'type':'usage','session_id':sid,'sequence':1,'kind':'acp_prompt','quality':'reported','values':{'inputTokens':7,'outputTokens':2,'totalTokens':9}})",
            "emit({'type':'result','session_id':sid,'result':{'summary':'synthetic completed','evidence':['fixture:boundary']}})",
        ]
    )
    spec = LaunchSpec(image, ("python", "-u", "-c", program))
    scope = "evidence-" + uuid4().hex
    url = "sqlite+aiosqlite:///" + str(directory / "runtime.sqlite")
    policy, descriptor = policy_fixture(directory, "evidence-probe", spec)
    db = Database(url)
    await db.migrate()
    store = RuntimeStore(db, scope)
    for index in range(2):
        await store.submit(
            str(index),
            {
                "runtime_profile": "evidence-probe",
                "profile_digest": spec.fingerprint(),
                "secret_refs": [],
            },
            authorization=descriptor,
        )
    save(
        directory / "config.json",
        {"scope": scope, "database_url": url, "spec": asdict(spec), **delay},
    )
    children, logs, observations = [], [], []
    started = time.monotonic()
    try:
        for slot in range(2):
            log = (directory / f"worker-{slot}.log").open("wb")
            logs.append(log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker-config",
                        str(directory / "config.json"),
                        "--slot",
                        str(slot),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        while any(p.poll() is None for p in children):
            if time.monotonic() - started > 90:
                raise TimeoutError("bounded native experiment deadline")
            current = await overview(db, scope, runs.c.scope == scope)
            observations.append(
                {"observed_at": current["observed_at"], **current["active"]}
            )
            await asyncio.sleep(0.15)
        assert all(p.returncode == 0 for p in children), (
            f"{name}: inspect private worker logs"
        )
        elapsed = time.monotonic() - started
        data = await capture(db, scope)
        report = summarize(data)
        workers = [
            json.loads((directory / f"worker-{n}.json").read_text()) for n in range(2)
        ]
        assert len({w["pid"] for w in workers}) == 2
        assert report["throughput"]["counts"] == {"succeeded": 2}
        peak = max((o["actual_concurrency"] or 0) for o in observations)
        # Actual sample has two fresh process/session identities, not a slot setting.
        assert peak == 2, f"{name}: two independent sessions were not observed"
        summary = {
            "arm": name,
            "injected_delay_seconds": delay,
            "execution_fixture_sleep_seconds": execution_seconds,
            "worker_pids": [w["pid"] for w in workers],
            "observed_peak_concurrency": peak,
            "completed_runs": 2,
            "elapsed_seconds": elapsed,
            "completions_per_second": 2 / elapsed,
            "policy_refresh": report["governance_seconds"].get(
                "policy_refresh_seconds"
            ),
            "protocol_read": distribution(
                [t for w in workers for t in w["protocol_read_seconds"]]
            ),
            "storage_wait": distribution(
                [t["wait_seconds"] for w in workers for t in w["transactions"]]
            ),
            "storage_commit_cleanup": distribution(
                [
                    t["commit_and_cleanup_seconds"]
                    for w in workers
                    for t in w["transactions"]
                ]
            ),
            "streaming": report["stage_seconds"].get("streaming"),
            "execution": distribution([w["execution_seconds"] for w in workers]),
            "capture_sha256": report["capture_sha256"],
            "report": report,
        }
        save(directory / "capture.json", data)
        save(directory / "observations.json", observations)
        save(directory / "result.json", summary)
        return summary
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
                await asyncio.to_thread(p.wait, 10)
        for log in logs:
            log.close()
        # Remove only this probe's scoped containers if an assertion interrupted it.
        adapter = DockerRuntimeAdapter(scope)
        try:
            for c in adapter.client.containers.list(
                all=True, filters={"label": "dios.runtime.scope=" + adapter.scope}
            ):
                c.remove(force=True)
        finally:
            adapter.client.close()
            await db.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path)
    parser.add_argument("--image", default="dios-backend:monitoring-20261126")
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--slot", type=int)
    args = parser.parse_args()
    if args.worker_config:
        await child(args.worker_config, args.slot)
        return
    if not args.output_directory:
        parser.error("--output-directory required")
    import docker

    client = docker.from_env()
    try:
        assert client.api.base_url == "http+docker://localhost", (
            "local Linux daemon required"
        )
        image = client.images.get(args.image).id
    finally:
        client.close()
    root = args.output_directory.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    results = {}
    for name in ("baseline", "policy", "protocol", "execution", "storage"):
        results[name] = await arm(root, name, image)
        print(
            json.dumps(
                {
                    "arm": name,
                    "completed": 2,
                    "peak": results[name]["observed_peak_concurrency"],
                }
            ),
            flush=True,
        )
    baseline = results["baseline"]
    contrasts = {}
    for name, metric, minimum in (
        ("policy", "policy_refresh", 0.10),
        ("protocol", "protocol_read", 0.10),
        ("storage", "storage_wait", 0.10),
        ("execution", "execution", 2.0),
    ):
        difference = (
            results[name][metric]["p50_seconds"] - baseline[metric]["p50_seconds"]
        )
        contrasts[name] = {
            "metric": metric,
            "p50_increase_seconds": difference,
            "minimum": minimum,
            "passed": difference >= minimum,
        }
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for name in results
        for p in (root / name).glob("*.json")
    }
    save(
        root / "result.json",
        {
            "work_item": "K04-3.04",
            "passed": all(v["passed"] for v in contrasts.values()),
            "image": image,
            "contrasts": contrasts,
            "arms": results,
            "raw_sha256": hashes,
            "scope": "Single-host mechanism qualification; synthetic protocol and token values; no real model/provider cost benchmark",
            "clock": "Durations use per-process monotonic clocks; phase intervals use durable database receipt times",
            "overlap": "Boundary timings are nested/overlapping and must not be summed",
            "worker_scope": "Two separate Linux Worker processes and two distinct container sessions per arm",
        },
    )
    assert all(v["passed"] for v in contrasts.values()), contrasts


if __name__ == "__main__":
    asyncio.run(main())
