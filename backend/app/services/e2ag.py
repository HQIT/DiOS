"""E2AG: Event-to-Agent Governance decision core.

The functions in this module are deliberately deterministic and side-effect free.
They form the policy-enforcement decision point used by the event dispatcher and
can also be replayed by experiments and auditors without starting an Agent or LLM.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
import hashlib
import json
from time import perf_counter_ns
from typing import Any, Mapping

from app.connectors import registry

POLICY_VERSION = "e2ag-2026-08-12.1"
SENSITIVE_ACTION_PREFIXES = (
    "admin.",
    "credential.",
    "filesystem.delete",
    "network.production.write",
    "secret.",
)


@dataclass(frozen=True)
class Decision:
    stage: str
    decision: str
    reason_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    contract_type: str = ""
    latency_us: int = 0
    evidence: dict[str, Any] | None = None

    @property
    def permits_execution(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        value["evidence"] = value["evidence"] or {}
        return value


def _elapsed_us(start_ns: int) -> int:
    return max(1, (perf_counter_ns() - start_ns) // 1_000)


def _contract_bindings() -> list[tuple[str, str, str]]:
    """Return (contract_type, source_pattern, event_type) bindings."""
    bindings: list[tuple[str, str, str]] = []
    for manifest in registry.all_manifests():
        for pattern in manifest.accepted_source_patterns:
            for event_type in manifest.event_types:
                bindings.append((manifest.type, pattern, event_type.type))
        for namespace in manifest.event_namespaces:
            for event_type in namespace.event_types:
                bindings.append((manifest.type, namespace.source_pattern, event_type.type))

    # Include externally declared namespaces. Built-in declarations duplicate the
    # entries above harmlessly; de-duplication keeps evidence stable for replay.
    known = set(bindings)
    for namespace in registry.event_namespaces():
        for event_type in namespace.event_types:
            item = ("declared_namespace", namespace.source_pattern, event_type.type)
            if item not in known:
                bindings.append(item)
                known.add(item)
    return bindings


def evaluate_contract(event: Mapping[str, Any]) -> Decision:
    """Validate structure and bind CloudEvent source/type to one manifest contract."""
    start = perf_counter_ns()
    missing = [
        field for field in ("specversion", "id", "source", "type", "data")
        if field not in event
    ]
    if missing:
        return Decision(
            stage="contract",
            decision="deny",
            reason_codes=("CONTRACT_REQUIRED_FIELD_MISSING",),
            latency_us=_elapsed_us(start),
            evidence={"missing_fields": missing},
        )
    if event.get("specversion") != "1.0":
        return Decision(
            stage="contract",
            decision="deny",
            reason_codes=("CONTRACT_SPECVERSION_UNSUPPORTED",),
            latency_us=_elapsed_us(start),
            evidence={"specversion": event.get("specversion")},
        )
    invalid_types = [
        field for field in ("id", "source", "type")
        if not isinstance(event.get(field), str) or not event.get(field)
    ]
    if invalid_types or not isinstance(event.get("data"), dict):
        return Decision(
            stage="contract",
            decision="deny",
            reason_codes=("CONTRACT_FIELD_TYPE_INVALID",),
            latency_us=_elapsed_us(start),
            evidence={"invalid_fields": invalid_types + ([] if isinstance(event.get("data"), dict) else ["data"])},
        )

    source = str(event["source"])
    event_type = str(event["type"])
    for contract_type, pattern, declared_type in _contract_bindings():
        if event_type == declared_type and fnmatchcase(source, pattern):
            return Decision(
                stage="contract",
                decision="allow",
                reason_codes=("CONTRACT_BOUND",),
                contract_type=contract_type,
                latency_us=_elapsed_us(start),
                evidence={"source_pattern": pattern, "event_type": declared_type},
            )

    return Decision(
        stage="contract",
        decision="deny",
        reason_codes=("CONTRACT_SOURCE_TYPE_UNBOUND",),
        latency_us=_elapsed_us(start),
        evidence={"source": source, "event_type": event_type},
    )


def _matches_any(value: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(value, pattern) for pattern in patterns)


def evaluate_policy(
    event: Mapping[str, Any],
    contract: Decision,
    target_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
) -> Decision:
    """Apply target-scoped least-privilege and approval rules.

    Agent governance declarations live at ``capabilities.governance``. Omitting a
    declaration preserves compatibility; declaring a list turns it into an
    allow-list for that dimension.
    """
    start = perf_counter_ns()
    if not contract.permits_execution:
        return Decision(
            stage="policy",
            decision="deny",
            reason_codes=("POLICY_CONTRACT_DENIED",),
            contract_type=contract.contract_type,
            latency_us=_elapsed_us(start),
        )

    data = event.get("data", {})
    request = data.get("_e2ag", {}) if isinstance(data, dict) else {}
    request = request if isinstance(request, dict) else {}
    action = str(request.get("action") or data.get("requested_action") or "")
    tool = str(request.get("tool") or data.get("requested_tool") or "")
    resource = str(request.get("resource") or data.get("target_resource") or "")
    environment = str(request.get("environment") or data.get("environment") or "unknown").lower()

    violations: list[dict[str, str]] = []
    for agent_id, capabilities in (target_capabilities or {}).items():
        governance = capabilities.get("governance", {}) if isinstance(capabilities, Mapping) else {}
        governance = governance if isinstance(governance, Mapping) else {}
        if not governance:
            violations.append({"agent_id": agent_id, "dimension": "governance_missing"})
            continue
        allowed_sources = governance.get("allowed_event_sources") or []
        allowed_types = governance.get("allowed_event_types") or []
        allowed_tools = governance.get("allowed_tools") or []
        allowed_actions = governance.get("allowed_actions") or []
        if governance.get("require_action_declaration") and not action:
            violations.append({"agent_id": agent_id, "dimension": "action_missing"})
        if allowed_sources and not _matches_any(str(event["source"]), list(allowed_sources)):
            violations.append({"agent_id": agent_id, "dimension": "source"})
        if allowed_types and not _matches_any(str(event["type"]), list(allowed_types)):
            violations.append({"agent_id": agent_id, "dimension": "event_type"})
        if tool and allowed_tools and not _matches_any(tool, list(allowed_tools)):
            violations.append({"agent_id": agent_id, "dimension": "tool"})
        if action and allowed_actions and not _matches_any(action, list(allowed_actions)):
            violations.append({"agent_id": agent_id, "dimension": "action"})

    if violations:
        return Decision(
            stage="policy",
            decision="deny",
            reason_codes=("POLICY_TARGET_CAPABILITY_VIOLATION",),
            contract_type=contract.contract_type,
            latency_us=_elapsed_us(start),
            evidence={"violations": violations, "action": action, "tool": tool},
        )

    sensitive = bool(action) and any(action == p or action.startswith(p) for p in SENSITIVE_ACTION_PREFIXES)
    if sensitive and environment in {"prod", "production"}:
        return Decision(
            stage="policy",
            decision="approval_required",
            reason_codes=("POLICY_PRODUCTION_SENSITIVE_ACTION",),
            contract_type=contract.contract_type,
            latency_us=_elapsed_us(start),
            evidence={"action": action, "tool": tool, "resource": resource, "environment": environment},
        )

    return Decision(
        stage="policy",
        decision="allow",
        reason_codes=("POLICY_ALLOWED",),
        contract_type=contract.contract_type,
        latency_us=_elapsed_us(start),
        evidence={"action": action, "tool": tool, "resource": resource, "environment": environment},
    )


def evaluate_event(
    event: Mapping[str, Any],
    target_capabilities: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[Decision, Decision]:
    contract = evaluate_contract(event)
    return contract, evaluate_policy(event, contract, target_capabilities)


def append_audit_entry(
    chain: list[dict[str, Any]],
    *,
    trace_id: str,
    stage: str,
    outcome: str,
    evidence: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Append a tamper-evident causal link and return a new chain value."""
    previous_hash = chain[-1]["entry_hash"] if chain else ""
    entry = {
        "sequence": len(chain),
        "trace_id": trace_id,
        "stage": stage,
        "outcome": outcome,
        "evidence": dict(evidence or {}),
        "previous_hash": previous_hash,
    }
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    entry["entry_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return [*chain, entry]


def verify_audit_chain(chain: list[dict[str, Any]]) -> bool:
    """Verify ordering, trace continuity and all hash links."""
    if not chain:
        return True
    trace_id = chain[0].get("trace_id")
    previous_hash = ""
    for index, stored in enumerate(chain):
        entry = dict(stored)
        entry_hash = entry.pop("entry_hash", "")
        if (
            entry.get("sequence") != index
            or entry.get("trace_id") != trace_id
            or entry.get("previous_hash") != previous_hash
        ):
            return False
        canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != entry_hash:
            return False
        previous_hash = entry_hash
    return True
