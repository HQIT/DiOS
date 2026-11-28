"""Stable business identity, immutable revisions and durable submission receipts."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from packaging.specifiers import SpecifierSet
from packaging.version import Version


class RegistryError(ValueError): pass


def check(value, code):
    if not value: raise RegistryError(code)


def identifier(value):
    check(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,159}", value), "INVALID_ID")
    return value


def encoded(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
def digest(value): return hashlib.sha256(encoded(value).encode()).hexdigest()


def definition(value):
    fields = {"name", "role", "responsibilities", "owner", "audience", "capability_refs", "knowledge_refs", "bindings"}
    check(isinstance(value, dict) and set(value) == fields and len(encoded(value).encode()) <= 65536, "INVALID_DEFINITION")
    for key in ("name", "role", "responsibilities", "owner", "audience"):
        check(isinstance(value[key], str) and 0 < len(value[key]) <= 2000, "INVALID_DEFINITION")
    for key in ("capability_refs", "knowledge_refs"):
        check(isinstance(value[key], list) and len(value[key]) <= 64, "INVALID_REFERENCES")
        for ref in value[key]: identifier(ref)
    check(isinstance(value["bindings"], dict) and 1 <= len(value["bindings"]) <= 16, "INVALID_BINDINGS")
    for kind, row in value["bindings"].items():
        identifier(kind)
        check(isinstance(row, dict) and set(row) == {"profile_id", "profile_digest", "requires"}, "INVALID_BINDING")
        identifier(row["profile_id"])
        check(isinstance(row["profile_digest"], str) and re.fullmatch(r"[0-9a-f]{64}", row["profile_digest"]), "INVALID_PROFILE_DIGEST")
        check(isinstance(row["requires"], dict) and len(row["requires"]) <= 32, "INVALID_REQUIREMENTS")
        for name, spec in row["requires"].items():
            identifier(name); check(isinstance(spec, str) and 0 < len(spec) <= 120, "INVALID_REQUIREMENTS")
            SpecifierSet(spec)
    return copy.deepcopy(value)


def principal(view):
    check(view.get("mode") == "principal" and isinstance(view.get("principal"), dict), "DEDICATED_PRINCIPAL_REQUIRED")
    return {key: view["principal"][key] for key in ("issuer", "scope", "id", "subject", "actor")}


async def validate(value, client, expected_identity):
    identity = await client.identity()
    check(principal(identity) == expected_identity, "EMPLOYEE_IDENTITY_MISMATCH")
    contracts = await client.runtime_contract()
    catalog = {row["id"]: row for row in await client.profiles()}
    report = {}
    for kind, binding in value["bindings"].items():
        row = catalog.get(binding["profile_id"])
        check(row is not None, "PROFILE_NOT_AVAILABLE")
        check(row["profile_digest"] == binding["profile_digest"], "PROFILE_VERSION_CONFLICT")
        check(row["governance"] == "e2ag" and row["policy_configured"], "E2AG_REQUIRED")
        check({"run.submit", "run.read"} <= set(identity["profiles"].get(row["id"], [])), "EMPLOYEE_SUBMISSION_DENIED")
        protocol = {"managed-process": "jsonl-v1", "remote-a2a": "a2a-0.3-jsonrpc"}.get(row["execution_kind"])
        actual = dict(contracts["interfaces"], **contracts.get("execution_interfaces", {}).get(protocol, {}))
        for name, spec in list(binding["requires"].items()) + [("profile-precondition", ">=1,<2"), ("e2ag-work", ">=1,<2")]:
            check(name in actual and Version(actual[name]) in SpecifierSet(spec), "REQUIRED_CAPABILITY_UNSUPPORTED")
        report[kind] = {key: row.get(key) for key in ("id", "profile_digest", "revision_ref", "deployment_id", "implementation")}
    return report


class Registry:
    def __init__(self, directory):
        self.root = Path(directory)
        check(self.root.is_absolute() and not self.root.is_symlink(), "INVALID_STATE_PATH")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.stat()
        check(info.st_uid == os.getuid() and not info.st_mode & 0o077, "PRIVATE_STATE_REQUIRED")
        self.lock = os.open(self.root/"owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.db = None
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root/"registry.sqlite"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600); os.close(fd)
            check(path.stat().st_uid == os.getuid() and not path.stat().st_mode & 0o077, "PRIVATE_STATE_REQUIRED")
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            check(self.db.execute("PRAGMA user_version").fetchone()[0] in (0,1), "UNSUPPORTED_REGISTRY_SCHEMA")
            self.db.execute("PRAGMA journal_mode=WAL"); self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS templates (id TEXT, version INTEGER, definition TEXT NOT NULL, PRIMARY KEY(id,version));
                CREATE TABLE IF NOT EXISTS employees (id TEXT PRIMARY KEY, principal TEXT UNIQUE NOT NULL, identity TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL, draft INTEGER NOT NULL, active INTEGER, disable_intent TEXT);
                CREATE TABLE IF NOT EXISTS revisions (employee TEXT, revision INTEGER, definition TEXT NOT NULL, template TEXT NOT NULL,
                    PRIMARY KEY(employee,revision));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT, kind TEXT, details TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS submissions (employee TEXT, key TEXT, request TEXT NOT NULL, request_digest TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL, run_id TEXT, PRIMARY KEY(employee,key));
                PRAGMA user_version=1;
            ''')
        except BaseException:
            self.close(); raise

    def close(self):
        if self.db is not None: self.db.close()
        os.close(self.lock)

    def event(self, employee, kind, details):
        self.db.execute("INSERT INTO events(employee,kind,details,created) VALUES (?,?,?,?)", (employee, kind, encoded(details), time.time()))

    def template(self, name, version, value):
        identifier(name); check(type(version) is int and version > 0, "INVALID_TEMPLATE_VERSION")
        value = definition(value)
        old = self.db.execute("SELECT definition FROM templates WHERE id=? AND version=?", (name,version)).fetchone()
        if old:
            check(old[0] == encoded(value), "IMMUTABLE_TEMPLATE_CONFLICT"); return
        with self.db: self.db.execute("INSERT INTO templates VALUES (?,?,?)", (name,version,encoded(value)))

    def get(self, employee):
        row = self.db.execute("SELECT * FROM employees WHERE id=?", (employee,)).fetchone()
        check(row is not None, "EMPLOYEE_NOT_FOUND")
        return dict(row)

    def revise(self, employee, template_id, template_version, overrides, *, expected_version, identity=None):
        identifier(employee)
        row = self.db.execute("SELECT definition FROM templates WHERE id=? AND version=?", (template_id,template_version)).fetchone()
        check(row is not None, "TEMPLATE_NOT_FOUND")
        base = json.loads(row[0]); check(isinstance(overrides, dict) and set(overrides) <= set(base), "INVALID_OVERRIDES")
        value = definition(dict(base, **overrides))
        previous = self.db.execute("SELECT * FROM employees WHERE id=?", (employee,)).fetchone()
        with self.db:
            if previous is None:
                check(expected_version == 0 and isinstance(identity, dict)
                      and set(identity) == {"issuer","scope","id","subject","actor"}, "EMPLOYEE_IDENTITY_REQUIRED")
                for item in identity.values(): identifier(item)
                owner = encoded({k:identity[k] for k in ("issuer","scope","id")})
                check(not self.db.execute("SELECT id FROM employees WHERE principal=?", (owner,)).fetchone(), "PRINCIPAL_ALREADY_ASSIGNED")
                self.db.execute("INSERT INTO employees VALUES (?,?,?,'draft',1,1,NULL,NULL)", (employee,owner,encoded(identity)))
                revision = 1
            else:
                check(previous["version"] == expected_version, "EMPLOYEE_VERSION_CONFLICT")
                check(previous["state"] != "disabling", "EMPLOYEE_DISABLE_PENDING")
                check(identity is None or encoded(identity) == previous["identity"], "EMPLOYEE_IDENTITY_IMMUTABLE")
                revision = previous["draft"] + 1
                self.db.execute("UPDATE employees SET draft=?,version=version+1 WHERE id=?", (revision, employee))
            self.db.execute("INSERT INTO revisions VALUES (?,?,?,?)", (employee, revision, encoded(value), encoded({"id":template_id,"version":template_version})))
            self.event(employee, "drafted", {"revision":revision})
        return self.get(employee)

    def revision(self, employee, revision):
        row = self.db.execute("SELECT definition FROM revisions WHERE employee=? AND revision=?", (employee,revision)).fetchone()
        check(row is not None, "REVISION_NOT_FOUND"); return json.loads(row[0])

    async def publish(self, employee, client, *, expected_version):
        row = self.get(employee)
        check(row["version"] == expected_version, "EMPLOYEE_VERSION_CONFLICT")
        check(row["state"] != "disabling", "EMPLOYEE_DISABLE_PENDING")
        report = await validate(self.revision(employee, row["draft"]), client, json.loads(row["identity"]))
        with self.db:
            self.db.execute("UPDATE employees SET active=draft,state='active',version=version+1 WHERE id=?", (employee,))
            self.event(employee, "published", {"revision":row["draft"],"bindings":report})
        return self.get(employee)

    async def submit(self, employee, kind, sample, goal, key, client):
        identifier(key)
        row = self.get(employee)
        check(principal(await client.identity()) == json.loads(row["identity"]), "EMPLOYEE_IDENTITY_MISMATCH")
        logical = {"kind":kind,"sample":sample,"goal":goal}
        old = self.db.execute("SELECT * FROM submissions WHERE employee=? AND key=?", (employee,key)).fetchone()
        if old:
            check(old["request_digest"] == digest(logical), "IDEMPOTENCY_CONFLICT")
            if old["state"] == "accepted": return dict(old)
        check(row["state"] == "active", "EMPLOYEE_NOT_ACTIVE")
        if old:
            request = json.loads(old["request"])
        else:
            value = self.revision(employee, row["active"])
            check(kind in value["bindings"], "WORK_TYPE_UNAVAILABLE")
            binding = value["bindings"][kind]
            request = {"profile_id":binding["profile_id"],"sample_id":sample,"goal":goal,
                "expected_profile_digest":binding["profile_digest"],"idempotency_key":"employee-"+digest([employee,key])}
            with self.db:
                self.db.execute("INSERT INTO submissions VALUES (?,?,?,?,?,'pending',NULL)",
                    (employee,key,encoded(request),digest(logical),row["active"]))
                self.event(employee,"submission_pending",{"key":key,"revision":row["active"]})
        # A changed capability or uncertain response retains the original request.
        result = await client.submit(**request)
        with self.db:
            self.db.execute("UPDATE submissions SET state='accepted',run_id=? WHERE employee=? AND key=?", (result["id"],employee,key))
            self.event(employee,"submitted",{"key":key,"run_id":result["id"]})
        return dict(self.db.execute("SELECT * FROM submissions WHERE employee=? AND key=?", (employee,key)).fetchone())

    async def disable(self, employee, admin, *, expected_version):
        row = self.get(employee)
        check(row["version"] == expected_version, "EMPLOYEE_VERSION_CONFLICT")
        if row["state"] == "disabled": return row
        identity = json.loads(row["identity"])
        if row["state"] != "disabling":
            current = await admin.projection(identity["id"])
            check(current["issuer"] == identity["issuer"] and current["scope"] == identity["scope"], "EMPLOYEE_IDENTITY_MISMATCH")
            intent = {"principal_id":identity["id"], "expected_revision":current["revision"], "changes":{"enabled":False}}
            with self.db:
                self.db.execute("UPDATE employees SET state='disabling',disable_intent=? WHERE id=?", (encoded(intent),employee))
                self.event(employee,"disable_requested",intent)
        else:
            intent = json.loads(row["disable_intent"])
        receipt = await admin.apply(intent)
        check(receipt.get("principal_id") == identity["id"] and receipt.get("enabled") is False
              and receipt.get("revision") == intent["expected_revision"]+1, "INVALID_DISABLE_RECEIPT")
        with self.db:
            self.db.execute("UPDATE employees SET state='disabled',version=version+1,disable_intent=NULL WHERE id=?", (employee,))
            self.event(employee,"disabled",receipt)
        return self.get(employee)

    def history(self, employee):
        self.get(employee)
        return {"employee":self.get(employee),
            "revisions":[dict(r) for r in self.db.execute("SELECT * FROM revisions WHERE employee=? ORDER BY revision", (employee,))],
            "events":[dict(r) for r in self.db.execute("SELECT * FROM events WHERE employee=? ORDER BY id", (employee,))],
            "tasks":[dict(r) for r in self.db.execute("SELECT * FROM submissions WHERE employee=? ORDER BY rowid", (employee,))]}
