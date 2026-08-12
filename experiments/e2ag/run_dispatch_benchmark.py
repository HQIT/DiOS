"""Benchmark Event Dispatcher admission and audit persistence with SQLite."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.services.event_dispatcher import dispatch_event  # noqa: E402


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))]


def make_event(index: int) -> dict:
    return {
        "specversion": "1.0",
        "id": f"evt_bench_{index}",
        "source": "manual/benchmark",
        "type": "manual.trigger",
        "subject": f"benchmark/{index}",
        "data": {"requested_action": "test.run", "environment": "test"},
    }


async def benchmark(repeats: int) -> list[dict]:
    old_dedup = settings.event_dedup_enabled
    old_mode = settings.e2ag_mode
    settings.event_dedup_enabled = False
    summaries = []
    try:
        orders = (
            ("off", "contract", "enforce"),
            ("enforce", "contract", "off"),
        )
        mode_samples: dict[str, list[float]] = {mode: [] for mode in orders[0]}
        for order in orders:
            for mode in order:
                # Fresh database per mode avoids table-growth bias. Reversed order
                # in the second round reduces systematic warm-up/run-order effects.
                engine = create_async_engine("sqlite+aiosqlite:///:memory:")
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                settings.e2ag_mode = mode
                async with sessions() as db:
                    for index in range(repeats):
                        start = perf_counter_ns()
                        log, error = await dispatch_event(make_event(index), [], db)
                        mode_samples[mode].append((perf_counter_ns() - start) / 1_000)
                        if error or log is None or log.status != "dispatched":
                            raise RuntimeError(f"unexpected dispatch result in {mode}: {error}")
                await engine.dispose()
        for mode in orders[0]:
            samples = mode_samples[mode]
            summaries.append({
                "mode": mode,
                "repeats_per_round": repeats,
                "rounds": len(orders),
                "measurements": len(samples),
                "mean_us": round(statistics.fmean(samples), 2),
                "p50_us": round(percentile(samples, 0.50), 2),
                "p95_us": round(percentile(samples, 0.95), 2),
                "p99_us": round(percentile(samples, 0.99), 2),
            })
    finally:
        settings.event_dedup_enabled = old_dedup
        settings.e2ag_mode = old_mode
    return summaries


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=500)
    args = parser.parse_args()
    summaries = await benchmark(args.repeats)
    output = Path(__file__).with_name("results") / "dispatch_benchmark.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
