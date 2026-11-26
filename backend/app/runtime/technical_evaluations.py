"""Versioned technical suites executed through governed Runtime Runs."""

import asyncio
import hashlib
import json
import math

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable
from sqlalchemy import func, select

from app.services.task_inputs import dynamic_input, registration

from . import technical_records as ledger
from .database import RuntimeErrorCode
from .execution_revisions import load_profile
from .lifecycle import admission
from .object_lifetime import ObjectLifetime
from .remote_control import TransactionView
from .result_contract import local_schema
from .schema import artifacts, attempts, events
from .store import RuntimeStore, bounded, digest, identifier


def suite(value):
    bounded(value, 65536)
    if not isinstance(value, dict) or set(value) != {"name", "cases"}:
        raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
    if not isinstance(value["name"], str) or not 1 <= len(value["name"]) <= 200:
        raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
    cases = value["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
        raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "input",
            "result_schema",
            "max_seconds",
        }:
            raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
        identifier(case["id"])
        if case["id"] in ids or not isinstance(case["input"], dict):
            raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
        ids.add(case["id"])
        duration = case["max_seconds"]
        if (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 0 < duration <= 3600
        ):
            raise RuntimeErrorCode("INVALID_EVALUATION_SUITE")
        try:
            local_schema(case["result_schema"])
            Draft202012Validator.check_schema(case["result_schema"])
        except (ValueError, TypeError, SchemaError, RecursionError):
            raise RuntimeErrorCode("INVALID_EVALUATION_SCHEMA") from None
        bounded(case["input"], 11000)
    return json.loads(json.dumps(value, allow_nan=False))


def case_input(raw, case):
    display = raw.get("console", {})
    if "input_catalog" in display:
        raise RuntimeErrorCode("EVALUATION_FROZEN_CONTRACT_REQUIRED")
    contract = registration(display)
    value = case["input"]
    if contract["dynamic_inputs"]:
        value = dynamic_input(contract, value)
    else:
        value = next((x for x in contract["inputs"] if x == value), None)
        if value is None:
            raise RuntimeErrorCode("EVALUATION_INPUT_NOT_REGISTERED")
    if contract["result_schema"] is None:
        raise RuntimeErrorCode("EVALUATION_RESULT_SCHEMA_REQUIRED")
    result = {
        "payload": {
            "goal": "Run the registered technical evaluation case",
            "sample": value,
        },
        "input_registration": {
            "digest": contract["digest"],
            "result_schema": contract["result_schema"],
        },
        "task_kind": contract["kind"],
    }
    bounded(result, 12000)
    return result


class Evaluations:
    def __init__(self, db, scope, policy, provider):
        self.db, self.scope, self.policy, self.provider = db, scope, policy, provider

    async def put_suite(self, suite_id, value, version, actor):
        value = suite(value)
        async with self.db.transaction(self.scope) as c:
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            row = await ledger.append(
                c, self.db, self.scope, "suite", suite_id, value, actor, version
            )
            return dict(row, ref=ledger.reference(row))

    async def start(self, revision_ref, suite_ref, key, actor):
        identifier(key)
        evaluation_id = "eval-" + digest([self.scope, key])
        identity = {
            "revision_ref": revision_ref,
            "suite_ref": suite_ref,
            "actor": actor,
        }
        async with self.db.transaction(self.scope) as c:
            old = await ledger.get(
                c, self.scope, "evaluation", evaluation_id, optional=True
            )
            if old:
                if old["document"]["request"] != identity:
                    raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
                return old
            if not await admission(c, self.scope):
                raise RuntimeErrorCode("RUNTIME_DRAINING")
            spec_row = await ledger.resolve(c, self.scope, "suite", suite_ref)
            bound = TransactionView(self.db, c, self.scope)
            raw, profile, spec, mode, refs = await load_profile(
                bound, self.scope, revision_ref
            )
            if self.policy is None or mode != "e2ag":
                raise RuntimeErrorCode("WORK_POLICY_REQUIRED")
            grant = await asyncio.to_thread(
                self.policy.admission, profile, spec.fingerprint(), refs
            )
            cases = []
            store = RuntimeStore(bound, self.scope)
            for case in spec_row["document"]["cases"]:
                value = case_input(raw, case)
                run = await store.submit(
                    evaluation_id + ":" + digest(case["id"]),
                    dict(
                        value,
                        runtime_profile=profile,
                        profile_digest=spec.fingerprint(),
                        secret_refs=refs,
                        origin="technical-evaluation/v1",
                    ),
                    authorization=grant,
                    revision_ref=revision_ref,
                )
                cases.append(
                    {
                        "case_id": case["id"],
                        "run_id": run["id"],
                        "input_digest": digest(value),
                    }
                )
            document = {
                "state": "running",
                "request": identity,
                "cases": cases,
                "profile_digest": spec.fingerprint(),
                "policy_digest": grant["policy_digest"],
                "measurements": [],
                "completed_at": None,
            }
            return await ledger.append(
                c, self.db, self.scope, "evaluation", evaluation_id, document, actor
            )

    async def _measure(self, c, evaluation, case, binding):
        store = RuntimeStore(TransactionView(self.db, c, self.scope), self.scope)
        run = await store._run(c, binding["run_id"])
        result = {
            "case_id": case["id"],
            "run_id": run["id"],
            "state": "running",
            "revision_ref": run["input"].get("revision_ref"),
        }
        if run["input"].get("revision_ref") != evaluation["request"]["revision_ref"]:
            raise RuntimeErrorCode("EVALUATION_REVISION_MISMATCH")
        if run["state"] in {"failed", "cancelled"}:
            return dict(result, state="failed", reason="EVALUATION_EXECUTION_FAILED")
        if run["state"] != "succeeded":
            return result
        attempt = (
            (
                await c.execute(
                    select(attempts).where(attempts.c.id == run["attempt_id"])
                )
            )
            .mappings()
            .one()
        )
        event = (
            (
                await c.execute(
                    select(events).where(
                        events.c.run_id == run["id"], events.c.type == "run.completed"
                    )
                )
            )
            .mappings()
            .one()
        )
        artifact = (
            (
                await c.execute(
                    select(artifacts).where(
                        artifacts.c.id == event["payload"]["detail"]["artifact_id"],
                        artifacts.c.run_id == run["id"],
                        artifacts.c.attempt_id == attempt["id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        ref = {"kind": "artifact", "id": artifact["id"], "version": 1}
        result.update(
            attempt_id=attempt["id"],
            session_id=attempt["session_id"],
            artifact=ref,
            artifact_digest=artifact["digest"],
            endpoint_digest=attempt["remote"].get("a2a", {}).get("declaration_digest"),
        )
        try:
            await ObjectLifetime(self.db, self.scope).available(c, ref)
            if artifact["size"] > 65536 or artifact["provider"] != self.provider.name:
                raise RuntimeErrorCode("EVALUATION_EVIDENCE_UNAVAILABLE")

            def read():
                data = bytearray()
                for chunk in self.provider.read(artifact["storage_key"]):
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise RuntimeErrorCode("EVALUATION_EVIDENCE_UNAVAILABLE")
                if (
                    len(data) != artifact["size"]
                    or hashlib.sha256(data).hexdigest() != artifact["digest"]
                ):
                    raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
                return json.loads(data)

            value = await asyncio.to_thread(read)
        except (OSError, ValueError) as exc:
            return dict(
                result,
                state="inconclusive",
                reason=exc.code
                if isinstance(exc, RuntimeErrorCode)
                else "EVALUATION_EVIDENCE_UNAVAILABLE",
            )
        first_started = (
            await c.execute(
                select(func.min(attempts.c.started_at)).where(
                    attempts.c.run_id == run["id"]
                )
            )
        ).scalar_one()
        elapsed = attempt["finished_at"] - first_started
        try:
            errors = []
            for error in Draft202012Validator(case["result_schema"]).iter_errors(value):
                errors.append(
                    {"path": list(error.absolute_path), "keyword": error.validator}
                )
                if len(errors) == 20:
                    break
        except (Unresolvable, RecursionError):
            return dict(
                result, state="inconclusive", reason="EVALUATION_SCHEMA_UNRESOLVED"
            )
        passed = not errors and 0 <= elapsed <= case["max_seconds"]
        return dict(
            result,
            state="passed" if passed else "failed",
            elapsed_seconds=elapsed,
            schema_errors=errors,
            max_seconds=case["max_seconds"],
            reason=None if passed else "EVALUATION_THRESHOLD_FAILED",
        )

    async def reconcile(self, evaluation_id):
        async with self.db.transaction(self.scope) as c:
            current = await ledger.get(c, self.scope, "evaluation", evaluation_id)
            document = current["document"]
            if document["state"] in {"passed", "failed"}:
                return current
            spec = await ledger.resolve(
                c, self.scope, "suite", document["request"]["suite_ref"]
            )
            values = [
                await self._measure(c, document, case, binding)
                for case, binding in zip(
                    spec["document"]["cases"], document["cases"], strict=True
                )
            ]
            states = {v["state"] for v in values}
            state = (
                "running"
                if "running" in states
                else "failed"
                if "failed" in states
                else "inconclusive"
                if "inconclusive" in states
                else "passed"
            )
            updated = dict(
                document,
                state=state,
                measurements=values,
                completed_at=await self.db.now(c)
                if state in {"passed", "failed"}
                else None,
            )
            if updated == document:
                return current
            return await ledger.append(
                c,
                self.db,
                self.scope,
                "evaluation",
                evaluation_id,
                updated,
                "evaluation-controller",
                current["version"],
            )

    async def compare(self, first, second):
        async with self.db.read(self.scope) as c:
            a = await ledger.get(c, self.scope, "evaluation", first)
            b = await ledger.get(c, self.scope, "evaluation", second)
        a, b = a["document"], b["document"]
        if any(x["state"] not in {"passed", "failed"} for x in (a, b)):
            raise RuntimeErrorCode("EVALUATION_COMPARISON_NOT_READY")
        if a["request"]["suite_ref"] != b["request"]["suite_ref"] or [
            x["input_digest"] for x in a["cases"]
        ] != [x["input_digest"] for x in b["cases"]]:
            raise RuntimeErrorCode("EVALUATION_COMPARISON_INPUT_CHANGED")
        return {
            "baseline": a,
            "candidate": b,
            "same_suite_and_inputs": True,
            "changes": [
                {
                    "case_id": x["case_id"],
                    "before": x["state"],
                    "after": y["state"],
                    "elapsed_seconds_delta": y["elapsed_seconds"] - x["elapsed_seconds"]
                    if "elapsed_seconds" in x and "elapsed_seconds" in y
                    else None,
                }
                for x, y in zip(a["measurements"], b["measurements"], strict=False)
            ],
        }
