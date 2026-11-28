"""Write synthetic, credential-free inputs for the directory planner."""
import json
from pathlib import Path
import sys
import time
from dios_directory.mapping import LIST, USER, GROUP

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)

def listing(schema, rows):
    return {"schemas": [LIST], "totalResults": len(rows),
            "Resources": [dict(schemas=[schema], **row) for row in rows]}

snapshot = {"version": 1, "source": "directory:demo", "observed_at": time.time(),
            "users": listing(USER, [{"id": "user-a", "active": True}, {"id": "user-b", "active": False}]),
            "groups": listing(GROUP, [{"id": "department-a", "members": [{"value": "user-a"}]},
                                      {"id": "department-b", "members": [{"value": "user-b"}]}])}
mapping = {"version": 1, "source": "directory:demo", "issuer": "trusted:demo", "scope": "demo",
           "subjects": {"user-a": "principal-a", "user-b": "principal-b"},
           "roles": {"department-a": {"review": {"actions": ["run.read", "artifact.read"], "permissions": []}},
                     "department-b": {"review": {"actions": ["run.read"], "permissions": []}}}}
current = [{"issuer": "trusted:demo", "scope": "demo", "principal_id": p,
            "revision": 1, "enabled": True, "profiles": {}} for p in ("principal-a", "principal-b")]
for name, value in (("snapshot", snapshot), ("mapping", mapping), ("current", current)):
    (root / (name + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
