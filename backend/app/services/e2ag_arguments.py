"""Versioned, closed top-level argument constraints (not full JSON Schema).

Every declared field is required; undeclared fields are rejected. String enums
use exact identifiers, never filesystem paths/URLs to resolve. No coercion,
wildcards, arbitrary Python predicates, nesting or information-flow analysis.
Version 2 adds bounded Unicode strings (code points, no normalization), without
constraining their meaning. Version 1 semantics are unchanged.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

MAX_STRING_CODEPOINTS = 262144


def valid_constraints(policy: Any) -> bool:
    if not isinstance(policy, dict) or set(policy) != {"version", "tools"}:
        return False
    if type(policy["version"]) is not int or policy["version"] not in (1, 2):
        return False
    tools = policy["tools"]
    if not isinstance(tools, dict) or not tools:
        return False
    for tool, fields in tools.items():
        if not isinstance(tool, str) or not tool or any(c in tool for c in "*?["):
            return False
        if not isinstance(fields, dict):
            return False
        for field, spec in fields.items():
            if not isinstance(field, str) or not field or not isinstance(spec, dict):
                return False
            if spec.get("type") == "string":
                if set(spec) == {"type", "enum"}:
                    if not isinstance(spec["enum"], list) or not spec["enum"] or not all(type(s) is str for s in spec["enum"]):
                        return False
                elif policy["version"] == 2 and set(spec) == {"type", "minLength", "maxLength"}:
                    low, high = spec["minLength"], spec["maxLength"]
                    if type(low) is not int or type(high) is not int or not 0 <= low <= high <= MAX_STRING_CODEPOINTS:
                        return False
                else:
                    return False
            elif spec.get("type") == "integer":
                if set(spec) != {"type", "minimum", "maximum"}:
                    return False
                if type(spec["minimum"]) is not int or type(spec["maximum"]) is not int or spec["minimum"] > spec["maximum"]:
                    return False
            else:
                return False
    return True


def constraints_digest(policy: Any) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def check_arguments(tool: str, arguments: Any, policy: Any) -> tuple[bool, str]:
    if not valid_constraints(policy):
        return False, "MCP_ARGUMENT_CONSTRAINT_INVALID"
    fields = policy["tools"].get(tool)
    if fields is None:
        return False, "MCP_ARGUMENT_TOOL_UNSPECIFIED"
    if not isinstance(arguments, dict) or set(arguments) != set(fields):
        return False, "MCP_ARGUMENT_FIELDS_DENIED"
    for field, spec in fields.items():
        value = arguments[field]
        if spec["type"] == "string":
            if type(value) is not str:
                return False, "MCP_ARGUMENT_TYPE_DENIED"
            if "enum" in spec:
                if value not in spec["enum"]:
                    return False, "MCP_ARGUMENT_ENUM_DENIED"
            else:
                # Reject unpaired surrogates before downstream UTF-8 encoding.
                if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
                    return False, "MCP_ARGUMENT_UNICODE_DENIED"
                if not spec["minLength"] <= len(value) <= spec["maxLength"]:
                    return False, "MCP_ARGUMENT_LENGTH_DENIED"
        else:
            if type(value) is not int:  # bool and float are deliberately excluded.
                return False, "MCP_ARGUMENT_TYPE_DENIED"
            if not spec["minimum"] <= value <= spec["maximum"]:
                return False, "MCP_ARGUMENT_RANGE_DENIED"
    return True, "MCP_ARGUMENTS_GRANTED"
