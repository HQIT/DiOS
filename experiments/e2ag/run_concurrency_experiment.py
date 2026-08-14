"""Stress replay deduplication and single-consumption approval under concurrency."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import settings  # noqa: E402
from app.db.database import Base  # noqa: E402
from app.models.tables import A2ATask, E2AGApproval, EventLog  # noqa: E402
from app.services.e2ag_approval import decide_approval  # noqa: E402
from app.services.event_dispatcher import dispatch_event  # noqa: E402
from app.services.event_normalizer import compute_dedup_hash  # noqa: E402


def replay_event(level: int, round_index: int) -> dict:
    return {
        "specversion": "1.0",
        "id": f"replay-{level}-{round_index}",
        "source": "github/concurrency/repo",
        "type": "git.push",
        "subject": f"refs/heads/race-{round_index}",
        "data": {
            "requested_action": "git.read",
            "before": "a",
            "after": f"b-{level}-{round_index}",
        },
    }


def approval_event(level: int, round_index: int) -> dict:
    return {
        "specversion": "1.0",
        "id": f"approval-race-{level}-{round_index}",
        "source": "github/concurrency/repo",
        "type": "git.push",
        "subject": f"approval/{round_index}",
        "data": {
            "requested_action": "secret.read",
            "environment": "production",
        },
    }


async def attempt_replay(sessions, payload: dict) -> str:
    try:
        async with sessions() as db:
            log, error = await dispatch_event(payload, [], db)
            if log is not None:
                return "created"
            if error and error.startswith("Duplicate of event"):
                return "deduplicated"
            return "unexpected"
    except OperationalError:
        return "database_error"
    except Exception as exc:  # retain unexpected implementation behavior in the result
        return f"exception:{type(exc).__name__}"


async def replay_phase(sessions, levels: list[int], rounds: int) -> dict:
    settings.event_dedup_enabled = True
    results = {}
    for level in levels:
        aggregate = Counter()
        violating_rounds = 0
        for round_index in range(rounds):
            payload = replay_event(level, round_index)
            outcomes = await asyncio.gather(*[
                attempt_replay(sessions, payload) for _ in range(level)
            ])
            aggregate.update(outcomes)
            digest = compute_dedup_hash(payload)
            async with sessions() as db:
                persisted = await db.scalar(
                    select(func.count()).select_from(EventLog).where(EventLog.dedup_hash == digest)
                )
            if persisted != 1:
                violating_rounds += 1
        results[str(level)] = {
            "rounds": rounds,
            "requests": level * rounds,
            "outcomes": dict(sorted(aggregate.items())),
            "rounds_with_persisted_count_not_one": violating_rounds,
        }
    return results


async def attempt_decision(sessions, approval_id: str, decision: str, actor: str) -> str:
    try:
        async with sessions() as db:
            await decide_approval(
                db, approval_id, decision=decision, actor=actor, reason="concurrency experiment",
            )
        return f"success:{decision}"
    except ValueError as exc:
        if str(exc) == "APPROVAL_ALREADY_DECIDED":
            return "expected_conflict"
        return f"value_error:{exc}"
    except OperationalError:
        return "database_error"
    except Exception as exc:
        return f"exception:{type(exc).__name__}"


async def approval_phase(sessions, levels: list[int], rounds: int) -> dict:
    settings.event_dedup_enabled = False
    results = {}
    for level in levels:
        aggregate = Counter()
        multi_success_rounds = 0
        invalid_terminal_rounds = 0
        duplicate_task_rounds = 0
        for round_index in range(rounds):
            async with sessions() as db:
                log, _ = await dispatch_event(approval_event(level, round_index), [], db)
                if log is None:
                    raise RuntimeError("approval setup was deduplicated")
                approval = (
                    await db.execute(select(E2AGApproval).where(E2AGApproval.event_log_id == log.id))
                ).scalar_one()
                approval_id = approval.id
                event_log_id = log.id
            outcomes = await asyncio.gather(*[
                attempt_decision(
                    sessions,
                    approval_id,
                    "approved" if index % 2 == 0 else "rejected",
                    f"actor-{index}",
                )
                for index in range(level)
            ])
            aggregate.update(outcomes)
            successes = [value for value in outcomes if value.startswith("success:")]
            if len(successes) != 1:
                multi_success_rounds += 1
            async with sessions() as db:
                stored = await db.get(E2AGApproval, approval_id)
                task_count = await db.scalar(
                    select(func.count()).select_from(A2ATask).where(A2ATask.context_id == event_log_id)
                )
            if stored is None or stored.status not in {"approved", "rejected"}:
                invalid_terminal_rounds += 1
            if task_count not in {0, 1}:
                duplicate_task_rounds += 1
        results[str(level)] = {
            "rounds": rounds,
            "requests": level * rounds,
            "outcomes": dict(sorted(aggregate.items())),
            "rounds_without_exactly_one_success": multi_success_rounds,
            "rounds_without_one_terminal_state": invalid_terminal_rounds,
            "rounds_with_more_than_one_task": duplicate_task_rounds,
        }
    return results


async def experiment(levels: list[int], rounds: int) -> dict:
    old_mode = settings.e2ag_mode
    old_dedup = settings.event_dedup_enabled
    settings.e2ag_mode = "enforce"
    try:
        with tempfile.TemporaryDirectory(prefix="e2ag-concurrency-") as temp:
            database = (Path(temp) / "race.db").as_posix()
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{database}",
                connect_args={"timeout": 30},
            )
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("PRAGMA journal_mode=WAL"))
                await connection.execute(text("PRAGMA busy_timeout=30000"))
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            try:
                replay = await replay_phase(sessions, levels, rounds)
                approval = await approval_phase(sessions, levels, rounds)
            finally:
                await engine.dispose()
        return {
            "protocol": "sqlite-concurrency-robustness-v1",
            "levels": levels,
            "rounds_per_level": rounds,
            "replay": replay,
            "approval": approval,
            "limitations": [
                "Single-host file-backed SQLite with independent async sessions.",
                "The result validates or falsifies current SQLite behavior; it does not establish behavior on PostgreSQL/MySQL.",
                "Database errors and invariant violations are retained rather than retried away.",
            ],
        }
    finally:
        settings.e2ag_mode = old_mode
        settings.event_dedup_enabled = old_dedup


async def async_main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", default="8,32")
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--output", type=Path, default=HERE / "results")
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",") if value.strip()]
    result = await experiment(levels, args.rounds)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "concurrency_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(async_main())
