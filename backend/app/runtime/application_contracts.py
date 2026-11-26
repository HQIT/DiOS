"""Versioned, data-only application requirements bound to immutable execution profiles."""

import re

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .database import RuntimeErrorCode
from .store import bounded, digest

API_VERSION = "1"
HEADER = "X-DiOS-Runtime-Contract"
INTERFACES = {
    "runtime-api": "1.0",
    "runtime-events": "1.0",
    "input-result-schema": "1.0",
    "e2ag-work": "1.0",
    "revision-deployment": "1.0",
}
EXECUTION_INTERFACES = {
    "jsonl-v1": {"worker-jsonl": "1.0", "confirmed-cancellation": "1.0"},
    "a2a-0.3-jsonrpc": {"a2a-jsonrpc": "0.3"},
}
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def description():
    return {
        "contract_version": API_VERSION,
        "interfaces": dict(INTERFACES),
        "execution_interfaces": {k: dict(v) for k, v in EXECUTION_INTERFACES.items()},
        "events": {
            "version": "1.0",
            "transport": "task-history",
            "identity": "run_id+version",
            "unknown_types": "preserve",
            "breaking_changes": "new-interface-major",
        },
        "application_manifest_version": 1,
        "version_ranges": "PEP-440",
        "schema": "JSON-Schema-2020-12-local-refs",
        "compatibility": "Additive response fields are allowed; required fields and meanings are stable within an interface major.",
    }


def manifest(value):
    bounded(value, 16384)
    fields = {"schema_version", "id", "version", "deployment", "requires", "input"}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise RuntimeErrorCode("INVALID_APPLICATION_CONTRACT")
    if (
        not isinstance(value["id"], str)
        or not NAME.fullmatch(value["id"])
        or value["deployment"] != "independent"
    ):
        raise RuntimeErrorCode("INVALID_APPLICATION_CONTRACT")
    try:
        if not isinstance(value["version"], str) or len(value["version"]) > 80:
            raise ValueError()
        if str(Version(value["version"])) != value["version"]:
            raise ValueError()
        requires = value["requires"]
        if (
            not isinstance(requires, dict)
            or not 1 <= len(requires) <= 32
            or not {
                "runtime-api",
                "runtime-events",
                "input-result-schema",
                "e2ag-work",
                "revision-deployment",
            }
            <= set(requires)
        ):
            raise ValueError()
        for name, spec in requires.items():
            if (
                not isinstance(name, str)
                or not NAME.fullmatch(name)
                or not isinstance(spec, str)
                or not 1 <= len(spec) <= 120
                or "===" in spec
            ):
                raise ValueError()
            SpecifierSet(spec)
        contract = value["input"]
        if not isinstance(contract, dict) or set(contract) != {
            "kind",
            "schema_digest",
            "result_schema_digest",
        }:
            raise ValueError()
        if not isinstance(contract["kind"], str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,159}", contract["kind"]
        ):
            raise ValueError()
        if any(
            not isinstance(contract[k], str) or not SHA.fullmatch(contract[k])
            for k in ["schema_digest", "result_schema_digest"]
        ):
            raise ValueError()
    except (ValueError, TypeError, InvalidVersion, InvalidSpecifier):
        raise RuntimeErrorCode("INVALID_APPLICATION_CONTRACT") from None
    return value


def compatibility(raw, contract=None):
    from app.services.task_inputs import registration

    contract = manifest(
        raw.get("application_contract") if contract is None else contract
    )
    registered = registration(raw.get("console", {}))
    actual = dict(INTERFACES) | EXECUTION_INTERFACES.get(raw.get("bridge"), {})
    if raw.get("bridge") == "jsonl-v1" and raw.get("tool_channel") is not None:
        actual["governed-tool-channel"] = "1.0"
    if raw.get("governance", {}).get("mode") != "e2ag":
        actual.pop("e2ag-work")
    issues = []
    for name, spec in sorted(contract["requires"].items()):
        version = actual.get(name)
        if version is None or not SpecifierSet(spec).contains(version):
            issues.append(
                {
                    "code": "APPLICATION_INTERFACE_UNSUPPORTED",
                    "interface": name,
                    "required": spec,
                    "available": version,
                }
            )
    if not ({"worker-jsonl", "a2a-jsonrpc"} & set(contract["requires"])):
        issues.append({"code": "APPLICATION_EXECUTION_INTERFACE_REQUIRED"})
    expected = {
        "kind": registered["kind"],
        "schema_digest": "sha256:" + digest(registered["schema"]),
        "result_schema_digest": "sha256:" + digest(registered["result_schema"]),
    }
    if registered["result_schema"] is None or not registered["requires_e2ag"]:
        issues.append({"code": "APPLICATION_GOVERNED_RESULT_REQUIRED"})
    if contract["input"] != expected:
        issues.append(
            {"code": "APPLICATION_SCHEMA_BINDING_CHANGED", "expected": expected}
        )
    return {
        "compatible": not issues,
        "issues": issues,
        "application": contract,
        "contract_digest": "sha256:" + digest(contract),
        "interfaces": actual,
    }


def require(raw):
    if "application_contract" not in raw:
        return None
    if "input_catalog_data" not in raw.get("console", {}):
        raise RuntimeErrorCode("APPLICATION_INPUT_CONTRACT_NOT_PINNED")
    report = compatibility(raw)
    if not report["compatible"]:
        raise RuntimeErrorCode(report["issues"][0]["code"])
    return {
        "id": report["application"]["id"],
        "version": report["application"]["version"],
        "digest": report["contract_digest"],
    }
