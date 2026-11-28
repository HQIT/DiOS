"""Compile complete SCIM exports into reviewable, revision-bound identity patches."""
import argparse
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
ACTIONS = {"run.submit", "run.read", "run.cancel", "run.revoke", "artifact.read",
           "tool.read", "tool.approve", "delegation.issue", "delegation.read", "delegation.revoke"}
PERMISSION = {"action", "resource_kind", "resource_id", "resource_version", "constraint_revision"}


class MappingError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise MappingError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/@+-]{0,199}", value),
            "INVALID_IDENTIFIER")
    return value


def profiles(value):
    require(isinstance(value, dict) and len(value) <= 64, "INVALID_PROFILES")
    result = {}
    for profile, grant in value.items():
        identifier(profile)
        require(isinstance(grant, dict) and set(grant) == {"actions", "permissions"}, "INVALID_GRANT")
        actions, permissions = grant["actions"], grant["permissions"]
        require(isinstance(actions, list) and all(isinstance(a, str) and a in ACTIONS for a in actions),
                "INVALID_ACTIONS")
        require("run.submit" not in actions or "run.read" in actions, "SUBMISSION_REQUIRES_READ")
        require(isinstance(permissions, list) and len(permissions) <= 256, "INVALID_PERMISSIONS")
        for permission in permissions:
            require(isinstance(permission, dict) and set(permission) == PERMISSION, "INVALID_PERMISSION")
            for item in permission.values():
                identifier(item)
        result[profile] = {"actions": sorted(set(actions)),
                           "permissions": [json.loads(p) for p in sorted({canonical(p) for p in permissions})]}
    return result


def resources(document, schema):
    require(isinstance(document, dict) and LIST in document.get("schemas", []), "INVALID_SCIM_LIST")
    rows = document.get("Resources")
    require(isinstance(rows, list) and len(rows) <= 4096, "INVALID_SCIM_RESOURCES")
    require(type(document.get("totalResults")) is int and document["totalResults"] == len(rows)
            and type(document.get("startIndex", 1)) is int and document.get("startIndex", 1) == 1
            and type(document.get("itemsPerPage", len(rows))) is int
            and document.get("itemsPerPage", len(rows)) == len(rows), "INCOMPLETE_DIRECTORY_SNAPSHOT")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and schema in row.get("schemas", []), "INVALID_SCIM_RESOURCE")
        key = identifier(row.get("id"))
        require(key not in result, "DUPLICATE_SCIM_ID")
        result[key] = row
    return result


def plan(snapshot, mapping, current, *, now=None, max_age=300):
    """Inputs come from a trusted directory export and operator policy projections.

    Missing mapped users are disabled. Missing groups remove their grants. Only
    complete, recent exports may express deletion; transport failures are errors.
    """
    now = time.time() if now is None else now
    require(type(max_age) in (int, float) and math.isfinite(max_age) and 0 < max_age <= 3600,
            "INVALID_MAX_AGE")
    require(isinstance(snapshot, dict) and isinstance(mapping, dict) and isinstance(current, list),
            "INVALID_INPUT")
    require(snapshot.get("version") == mapping.get("version") == 1, "UNSUPPORTED_VERSION")
    require(isinstance(mapping.get("source"), str) and bool(mapping["source"])
            and snapshot.get("source") == mapping["source"], "DIRECTORY_SOURCE_MISMATCH")
    observed = snapshot.get("observed_at")
    require(type(observed) in (int, float) and math.isfinite(observed)
            and 0 <= now - observed <= max_age, "STALE_DIRECTORY_SNAPSHOT")
    users = resources(snapshot.get("users"), USER)
    groups = resources(snapshot.get("groups"), GROUP)
    for user in users.values():
        require(type(user.get("active")) is bool, "SCIM_ACTIVE_REQUIRED")
    membership = {uid: set() for uid in users}
    for gid, group in groups.items():
        members = group.get("members", [])
        require(isinstance(members, list) and len(members) <= 4096, "INVALID_MEMBERS")
        for member in members:
            require(isinstance(member, dict) and member.get("type", "User") == "User",
                    "NESTED_GROUP_UNSUPPORTED")
            uid = member.get("value")
            require(isinstance(uid, str) and uid in users, "UNKNOWN_GROUP_MEMBER")
            membership[uid].add(gid)
    subjects, roles = mapping.get("subjects"), mapping.get("roles")
    require(isinstance(subjects, dict) and 0 < len(subjects) <= 256
            and isinstance(roles, dict) and len(roles) <= 256, "INVALID_MAPPING")
    role_profiles = {identifier(gid): profiles(value) for gid, value in roles.items()}
    for uid, pid in subjects.items():
        identifier(uid); identifier(pid)
    require(len(set(subjects.values())) == len(subjects), "DUPLICATE_PRINCIPAL_MAPPING")
    states = {}
    for row in current:
        require(isinstance(row, dict) and set(row) == {"issuer", "scope", "principal_id", "revision", "enabled", "profiles"},
                "INVALID_POLICY_PROJECTION")
        pid = identifier(row["principal_id"])
        require(pid not in states, "DUPLICATE_POLICY_PROJECTION")
        require(row["issuer"] == mapping.get("issuer") and row["scope"] == mapping.get("scope"),
                "IDENTITY_NAMESPACE_MISMATCH")
        require(type(row["revision"]) is int and row["revision"] >= 1
                and type(row["enabled"]) is bool, "INVALID_POLICY_PROJECTION")
        states[pid] = dict(row, profiles=profiles(row["profiles"]))
    patches, unchanged = [], []
    for uid, pid in sorted(subjects.items()):
        identifier(uid); identifier(pid)
        require(pid in states, "PRINCIPAL_NOT_PROVISIONED")
        active = uid in users and users[uid]["active"]
        selected = sorted(membership.get(uid, set()) & roles.keys()) if active else []
        merged = {}
        for gid in selected:
            for profile, grant in role_profiles[gid].items():
                target = merged.setdefault(profile, {"actions": [], "permissions": []})
                target["actions"].extend(grant["actions"])
                target["permissions"].extend(grant["permissions"])
        desired = {"enabled": active, "profiles": profiles(merged)}
        old = states[pid]
        if all(old[k] == v for k, v in desired.items()):
            unchanged.append(pid)
        else:
            patches.append({"principal_id": pid, "expected_revision": old["revision"],
                            "source_user_id": uid, "source_group_ids": selected, "changes": desired})
    return {"version": 1, "source": mapping["source"], "issuer": mapping["issuer"], "scope": mapping["scope"],
            "snapshot_sha256": hashlib.sha256(canonical(snapshot).encode()).hexdigest(),
            "mapping_sha256": hashlib.sha256(canonical(mapping).encode()).hexdigest(),
            "observed_at": observed, "valid_until": observed + max_age,
            "patches": patches, "unchanged": unchanged}


def load(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    require(len(raw) <= 4 * 1024 * 1024, "INPUT_TOO_LARGE")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def main():
    parser = argparse.ArgumentParser(description="Plan directory policy changes; does not modify DiOS")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--current", required=True, help="JSON array of trusted identity-policy CLI projections")
    parser.add_argument("--max-age", type=int, default=300)
    args = parser.parse_args()
    try:
        value = plan(load(args.snapshot), load(args.mapping), load(args.current), max_age=args.max_age)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, MappingError) else "INVALID_DIRECTORY_INPUT"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
