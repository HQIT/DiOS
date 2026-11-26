"""Data-only configuration drafts. No filesystem writes, publication or execution."""

import asyncio
import copy
import json
import math

import jsonpatch
from jsonpointer import resolve_pointer
from jsonschema import Draft202012Validator

from app.runtime.database import RuntimeErrorCode
from app.runtime.revisions import safe_configuration
from app.runtime.store import digest
from app.runtime.work_policy import security_profile
from app.runtime.worker import read_profile

MAX_BYTES = 65536
KIND = "dios-config-proposal-v1"
STRING = {"type": "string", "minLength": 1}
SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "bridge"],
    "properties": {
        "id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"},
        "bridge": {"enum": ["jsonl-v1", "a2a-0.3-jsonrpc"]},
        "governance": {
            "type": "object",
            "required": ["mode"],
            "properties": {"mode": {"enum": ["trusted-local", "e2ag"]}},
        },
        "launch": {
            "type": "object",
            "required": ["image", "command"],
            "additionalProperties": False,
            "properties": {
                "image": STRING,
                "command": {"type": "array", "minItems": 1, "items": STRING},
                "kind": {"const": "task"},
                "network": STRING,
                "user": STRING,
                "working_dir": {"type": "string", "pattern": "^/"},
                "environment": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "host_aliases": {"type": "object", "additionalProperties": STRING},
                "memory_bytes": {"type": "integer", "minimum": 16777216},
                "cpu_count": {"type": "number", "exclusiveMinimum": 0, "maximum": 128},
                "pids_limit": {"type": "integer", "minimum": 1, "maximum": 4096},
                "read_only_root": {"type": "boolean"},
                "mounts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["source", "target"],
                        "additionalProperties": False,
                        "properties": {
                            "source": {"type": "string", "pattern": "^/"},
                            "target": {"type": "string", "pattern": "^/"},
                            "read_only": {"type": "boolean"},
                        },
                    },
                },
            },
        },
        "remote": {"type": "object"},
        "console": {"type": "object"},
        "recovery": {"type": "object"},
        "tool_channel": {"type": "object"},
        "isolation": {"type": "object"},
        "secrets": {"type": "array"},
        "package_source": {"type": "object"},
        "package_lock": {"type": "object"},
        "input_mount_targets": {"type": "array", "items": STRING},
        "packaged_mounts": {"type": "array"},
    },
    "allOf": [
        {
            "if": {"properties": {"bridge": {"const": "jsonl-v1"}}},
            "then": {"required": ["launch"]},
        },
        {
            "if": {"properties": {"bridge": {"const": "a2a-0.3-jsonrpc"}}},
            "then": {"required": ["remote", "governance"]},
        },
    ],
}


def pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def problem(code, path="", **details):
    return {"code": code, "path": path, **details}


def parse(text):
    try:
        size = len(text.encode("utf-8")) if isinstance(text, str) else MAX_BYTES + 1
    except UnicodeError:
        return None, [problem("STUDIO_JSON_INVALID")]
    if size > MAX_BYTES:
        return None, [problem("STUDIO_DOCUMENT_TOO_LARGE")]

    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError("STUDIO_DUPLICATE_KEY")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("STUDIO_NONFINITE_NUMBER")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        pending = [(value, 0)]
        count = 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("STUDIO_NONFINITE_NUMBER")
            if isinstance(item, str):
                item.encode("utf-8")
            if depth > 32 or count > 4096 or isinstance(item, list) and len(item) > 512:
                raise ValueError("STUDIO_DOCUMENT_COMPLEXITY")
            if isinstance(item, dict):
                for key in item:
                    key.encode("utf-8")
                pending.extend((x, depth + 1) for x in item.values())
            if isinstance(item, list):
                pending.extend((x, depth + 1) for x in item)
        return value, []
    except json.JSONDecodeError as exc:
        return None, [problem("STUDIO_JSON_SYNTAX", line=exc.lineno, column=exc.colno)]
    except (ValueError, RecursionError):
        return None, [problem("STUDIO_JSON_INVALID")]


def validate(value):
    errors = []
    for error in Draft202012Validator(SCHEMA).iter_errors(value):
        errors.append(
            problem(
                "STUDIO_SCHEMA_INVALID",
                pointer(error.absolute_path),
                keyword=error.validator,
                expected=error.validator_value,
                schema_path=pointer(error.absolute_schema_path),
            )
        )
        if len(errors) == 50:
            break
    if errors:
        return errors
    try:
        # Secret descriptors are checked by the existing reference-only contract.
        safe_configuration({k: v for k, v in value.items() if k != "secrets"})
        security_profile("studio-draft", raw=value)
        read_profile("studio-draft", value=value, package_revision=True)
    except RuntimeErrorCode as exc:
        path = (
            "/recovery"
            if "RECOVERY" in exc.code
            else "/tool_channel"
            if "TOOL_CHANNEL" in exc.code
            else "/isolation"
            if "ISOLATION" in exc.code
            else ""
        )
        return [problem(exc.code, path)]
    except (KeyError, TypeError, ValueError, AttributeError):
        return [problem("STUDIO_PROFILE_INVALID")]
    return []


def compare(base_text, draft_text):
    base, base_errors = parse(base_text)
    draft, errors = parse(draft_text)
    result = {
        "valid": False,
        "errors": errors,
        "base_errors": base_errors,
        "base_digest": None,
        "draft_digest": None,
        "patch": [],
        "changes": [],
    }
    if base_errors or errors:
        return result
    errors = validate(draft)
    base_errors = validate(base)
    result.update(errors=errors, base_errors=base_errors)
    # Credentials and invalid structure never enter a diff or an assistant prompt.
    if base_errors or errors:
        return result
    patch = jsonpatch.make_patch(base, draft).patch
    current = copy.deepcopy(base)
    changes = []
    for operation in patch:
        row = dict(operation)
        if operation["op"] in {"remove", "replace"}:
            row["before"] = resolve_pointer(current, operation["path"])
        if "from" in operation:
            row["before"] = resolve_pointer(current, operation["from"])
        current = jsonpatch.apply_patch(current, [operation])
        if operation["op"] != "remove":
            row["after"] = resolve_pointer(current, operation["path"])
        changes.append(row)
    return dict(
        result,
        valid=True,
        base_digest=digest(base),
        draft_digest=digest(draft),
        patch=patch,
        changes=changes,
    )


async def source(console, profile_id):
    from app.runtime.deployments import row as deployment_row
    from app.runtime.execution_revisions import freeze_console, load_profile

    for _, raw in await asyncio.to_thread(console._profile_sources):
        if raw.get("id") != profile_id:
            continue
        revision_ref = raw.get("revision_ref")
        if raw.get("deployment_id"):
            async with console.db.read(console.scope) as connection:
                deployed = await deployment_row(
                    connection, console.scope, raw["deployment_id"]
                )
            revision_ref = deployed["active_revision_id"]
            if revision_ref is None:
                raise RuntimeErrorCode("PROFILE_NOT_AVAILABLE")
        if revision_ref:
            raw, _, _, _, _ = await load_profile(
                console.db, console.scope, revision_ref
            )
        if raw.get("id") != profile_id:
            raise RuntimeErrorCode("STUDIO_SOURCE_ID_MISMATCH")
        # Source paths are operator-owned; return a self-contained editable copy.
        await asyncio.to_thread(freeze_console, raw)
        errors = validate(raw)
        if errors:
            raise RuntimeErrorCode("STUDIO_SOURCE_NOT_EDITABLE")
        return {
            "profile_id": profile_id,
            "revision_ref": revision_ref,
            "text": json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            "digest": digest(raw),
        }
    raise RuntimeErrorCode("PROFILE_NOT_AVAILABLE")


async def suggest(console, profile_id, text, instruction, key):
    checked = compare(text, text)
    if not checked["valid"]:
        raise RuntimeErrorCode("INVALID_STUDIO_DRAFT")
    profiles = await console.catalog()
    selected = next((p for p in profiles if p["id"] == profile_id), None)
    if (
        not selected
        or selected["input_kind"] != KIND
        or selected["governance"] != "e2ag"
        or not selected["dynamic_inputs"]
    ):
        raise RuntimeErrorCode("STUDIO_ASSISTANT_NOT_AVAILABLE")
    value = {
        "id": "configuration-proposal",
        "name": "Configuration proposal",
        "configuration": json.loads(text),
        "base_digest": checked["base_digest"],
        "instruction": instruction,
    }
    if len(json.dumps(value, ensure_ascii=False).encode()) > 11000:
        raise RuntimeErrorCode("INVALID_STUDIO_ASSISTANT_INPUT_SIZE")
    return await console.submit(
        profile_id,
        None,
        "Propose a configuration change for human review",
        key,
        input_payload=value,
    )


async def proposal(console, run_id, text):
    checked = compare(text, text)
    if not checked["valid"]:
        raise RuntimeErrorCode("INVALID_STUDIO_DRAFT")
    run = await console.authorize(run_id, "run.read")
    if run["input"].get("task_kind") != KIND or run["state"] != "succeeded":
        raise RuntimeErrorCode("STUDIO_PROPOSAL_NOT_READY")
    if run["input"]["payload"]["sample"]["base_digest"] != checked["base_digest"]:
        raise RuntimeErrorCode("STUDIO_PROPOSAL_STALE")
    detail = await console.detail(run_id)
    artifact = next((a for a in detail["artifacts"] if a["kind"] == "result"), None)
    if artifact is None:
        raise RuntimeErrorCode("STUDIO_PROPOSAL_NOT_READY")
    value = json.loads((await console.preview(run_id, artifact["id"]))["text"])
    if not isinstance(value, dict) or not isinstance(
        value.get("configuration_proposal"), dict
    ):
        raise RuntimeErrorCode("STUDIO_PROPOSAL_INVALID")
    value = value.get("configuration_proposal", {})
    if value.get("base_digest") != checked["base_digest"]:
        raise RuntimeErrorCode("STUDIO_PROPOSAL_STALE")
    candidate = (
        json.dumps(value.get("configuration"), ensure_ascii=False, indent=2) + "\n"
    )
    review = compare(text, candidate)
    if not review["valid"]:
        return {"review": review, "text": None}
    return {"review": review, "text": candidate}
