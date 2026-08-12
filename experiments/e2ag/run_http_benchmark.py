"""FastAPI + middleware + SQLite HTTP benchmark for E2AG event admission."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

HERE = Path(__file__).resolve().parent


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=300)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="e2ag-http-") as temp:
        database = (Path(temp) / "bench.db").as_posix()
        os.environ["DIOS_DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
        os.environ["DIOS_EVENT_DEDUP_ENABLED"] = "false"

        warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient` is deprecated")
        from fastapi.testclient import TestClient
        import main as dios_main
        from app.config import settings
        from app.db.database import engine

        client_context = TestClient(dios_main.app)
        client = client_context.__enter__()
        try:
            results: dict[str, list[float]] = {name: [] for name in ("off", "contract", "enforce")}
            sequence = ("off", "contract", "enforce", "enforce", "contract", "off")
            per_round = max(1, args.repeats // 2)
            counter = 0
            for mode in sequence:
                settings.e2ag_mode = mode
                for _ in range(per_round):
                    counter += 1
                    started = time.perf_counter_ns()
                    response = client.post(
                        "/api/os/events/manual",
                        json={
                            "event_type": "manual.trigger",
                            "source": "manual/http-benchmark",
                            "subject": f"http-{counter}",
                            "data": {},
                        },
                    )
                    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                    if response.status_code != 200 or response.json()["status"] != "dispatched":
                        raise RuntimeError(f"unexpected response: {response.status_code} {response.text}")
                    results[mode].append(elapsed_ms)
        finally:
            client_context.__exit__(None, None, None)
            asyncio.run(engine.dispose())

    summary = []
    for mode, values in results.items():
        summary.append({
            "mode": mode,
            "measurements": len(values),
            "mean_ms": round(statistics.mean(values), 3),
            "p50_ms": round(percentile(values, 0.50), 3),
            "p95_ms": round(percentile(values, 0.95), 3),
            "p99_ms": round(percentile(values, 0.99), 3),
        })
    output = {
        "requested_repeats_per_mode": args.repeats,
        "environment": "FastAPI TestClient, middleware, API serialization, file-backed SQLite; no TCP socket, subscription targets, Agent, model, or tool network.",
        "order": ["off", "contract", "enforce", "enforce", "contract", "off"],
        "results": summary,
    }
    destination = HERE / "results" / "http_benchmark.json"
    destination.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
