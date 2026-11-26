"""Bounded package resolution with resolvelib and PEP 440 version constraints."""

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from resolvelib import AbstractProvider, BaseReporter, Resolver
from resolvelib.resolvers import ResolutionImpossible, ResolutionTooDeep

from .database import RuntimeErrorCode
from .store import digest, encoded

MAX_METADATA = 192 * 1024
MAX_PACKAGES = 128
MAX_CATALOG = 512
MAX_FILES = 2048
MAX_BYTES = 64 * 1024 * 1024
NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,79}\Z")
SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def fail(code="PACKAGE_MANIFEST_INVALID"):
    raise RuntimeErrorCode(code)


def snapshot(value):
    try:
        data = encoded(value)
        if len(data) > MAX_METADATA:
            fail("PACKAGE_METADATA_TOO_LARGE")
        return json.loads(data)
    except (TypeError, ValueError, RecursionError):
        fail()


def read_json(path):
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_METADATA + 1)
        if len(data) > MAX_METADATA:
            fail("PACKAGE_METADATA_TOO_LARGE")
        return json.loads(data)
    except (OSError, ValueError, RecursionError):
        fail("PACKAGE_METADATA_UNAVAILABLE")


def requirements(value):
    if not isinstance(value, list) or len(value) > MAX_PACKAGES:
        fail()
    names = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "specifier"}:
            fail()
        name, spec = item["name"], item["specifier"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in names:
            fail()
        if not isinstance(spec, str) or len(spec) > 256 or "===" in spec:
            fail()
        try:
            SpecifierSet(spec)
        except InvalidSpecifier:
            fail()
        names.add(name)
    return sorted(value, key=lambda x: x["name"])


def manifest(value):
    value = snapshot(value)
    if not isinstance(value, dict) or set(value) - {"declaration"} != {
        "schema_version",
        "name",
        "version",
        "kind",
        "dependencies",
        "files",
    }:
        fail()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        fail()
    if "declaration" in value:
        from .package_declarations import declaration

        value["declaration"] = declaration(value["declaration"])
    if not isinstance(value["name"], str) or not NAME.fullmatch(value["name"]):
        fail()
    if value["kind"] not in ("harness", "skill", "tool", "prompt", "policy"):
        fail()
    try:
        if not isinstance(value["version"], str) or len(value["version"]) > 80:
            fail()
        value["version"] = str(Version(value["version"]))
    except InvalidVersion:
        fail()
    value["dependencies"] = requirements(value["dependencies"])
    files = value["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        fail()
    paths = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "mediaType",
            "digest",
            "size",
            "mode",
        }:
            fail()
        path = entry["path"]
        if not isinstance(path, str) or not 1 <= len(path) <= 240:
            fail()
        parts = PurePosixPath(path)
        if (
            parts.is_absolute()
            or str(parts) != path
            or ".." in parts.parts
            or path == "."
            or any(ord(c) < 32 or c == "\\" for c in path)
            or path in paths
        ):
            fail("PACKAGE_PATH_INVALID")
        paths.add(path)
        if (
            not isinstance(entry["digest"], str)
            or not SHA.fullmatch(entry["digest"])
            or type(entry["size"]) is not int
            or not 0 <= entry["size"] <= MAX_BYTES
            or type(entry["mode"]) is not int
            or entry["mode"] not in (0o644, 0o755)
            or not isinstance(entry["mediaType"], str)
            or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", entry["mediaType"])
        ):
            fail()
    if any(str(parent) in paths for p in paths for parent in PurePosixPath(p).parents):
        fail("PACKAGE_PATH_INVALID")
    value["files"] = sorted(files, key=lambda x: x["path"])
    return value


@dataclass(frozen=True)
class Candidate:
    name: str
    version: Version
    checksum: str


class Provider(AbstractProvider):
    def __init__(self, catalog):
        self.catalog = catalog

    def identify(self, requirement_or_candidate):
        item = requirement_or_candidate
        return item.name if isinstance(item, Candidate) else item["name"]

    def get_preference(
        self, identifier, resolutions, candidates, information, backtrack_causes
    ):
        return identifier

    def find_matches(self, identifier, requirements, incompatibilities):
        requested = list(requirements[identifier])
        excluded = list(incompatibilities[identifier])
        return sorted(
            (
                c
                for c in self.catalog
                if c.name == identifier
                and c not in excluded
                and all(self.is_satisfied_by(r, c) for r in requested)
            ),
            key=lambda c: c.version,
            reverse=True,
        )

    def is_satisfied_by(self, requirement, candidate):
        return requirement["name"] == candidate.name and SpecifierSet(
            requirement["specifier"]
        ).contains(candidate.version)

    def get_dependencies(self, candidate):
        return self.catalog[candidate]["dependencies"]


def resolve(catalog, roots):
    roots = requirements(snapshot(roots))
    if not roots or not isinstance(catalog, list) or len(catalog) > MAX_CATALOG:
        fail()
    candidates = {}
    seen = set()
    for raw in catalog:
        item = manifest(raw)
        version = Version(item["version"])
        identity = (item["name"], version)
        if identity in seen:
            fail("PACKAGE_VERSION_AMBIGUOUS")
        seen.add(identity)
        candidates[Candidate(*identity, "sha256:" + digest(item))] = item
    try:
        result = Resolver(Provider(candidates), BaseReporter()).resolve(
            roots, max_rounds=1024
        )
    except ResolutionImpossible:
        fail("PACKAGE_DEPENDENCY_UNSATISFIED")
    except ResolutionTooDeep:
        fail("PACKAGE_RESOLUTION_LIMIT")
    body = {
        "schema_version": 1,
        "roots": roots,
        "packages": [
            {"digest": c.checksum, "manifest": candidates[c]}
            for _, c in sorted(result.mapping.items())
        ],
    }
    lock = dict(body, digest="sha256:" + digest(body))
    return verify(lock)


def verify(value):
    lock = snapshot(value)
    if not isinstance(lock, dict) or set(lock) != {
        "schema_version",
        "roots",
        "packages",
        "digest",
    }:
        fail("PACKAGE_LOCK_INVALID")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        fail("PACKAGE_LOCK_INVALID")
    body = {k: v for k, v in lock.items() if k != "digest"}
    if lock["digest"] != "sha256:" + digest(body):
        fail("PACKAGE_LOCK_DIGEST_MISMATCH")
    roots = requirements(lock["roots"])
    rows = lock["packages"]
    if not roots or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PACKAGES:
        fail("PACKAGE_LOCK_INVALID")
    selected = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"manifest", "digest"}:
            fail("PACKAGE_LOCK_INVALID")
        item = manifest(row["manifest"])
        if item != row["manifest"] or row["digest"] != "sha256:" + digest(item):
            fail("PACKAGE_DIGEST_MISMATCH")
        if item["name"] in selected:
            fail("PACKAGE_VERSION_AMBIGUOUS")
        selected[item["name"]] = item
    files = [f for m in selected.values() for f in m["files"]]
    if len(files) > MAX_FILES or sum(f["size"] for f in files) > MAX_BYTES:
        fail("PACKAGE_PAYLOAD_TOO_LARGE")
    visited, active = set(), set()

    def walk(req):
        name = req["name"]
        item = selected.get(name)
        if item is None or not SpecifierSet(req["specifier"]).contains(
            Version(item["version"])
        ):
            fail("PACKAGE_DEPENDENCY_UNSATISFIED")
        if name in active:
            fail("PACKAGE_DEPENDENCY_CYCLE")
        if name in visited:
            return
        active.add(name)
        for child in item["dependencies"]:
            walk(child)
        active.remove(name)
        visited.add(name)

    for req in roots:
        walk(req)
    if visited != set(selected):
        fail("PACKAGE_LOCK_UNREACHABLE")
    return lock
