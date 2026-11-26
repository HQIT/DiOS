"""Compile immutable execution profiles; live E2AG policy remains authoritative."""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import select

from .database import RuntimeErrorCode
from .revision_schema import require, revisions
from .revisions import DEPENDENCY_KINDS, Revisions, representation
from .store import digest

FORMAT = "dios-execution-profile-v1"


def adapter_descriptor(bridge):
    names = {
        "jsonl-v1": (
            "local-docker-v1",
            ("adapter.py", "docker_adapter.py", "bridge.py"),
        ),
        "a2a-0.3-jsonrpc": ("a2a-0.3-jsonrpc", ("a2a_profile.py", "a2a_transport.py")),
    }
    if bridge not in names:
        raise RuntimeErrorCode("BRIDGE_UNSUPPORTED")
    name, files = names[bridge]
    hashes = {
        f: hashlib.sha256(Path(__file__).with_name(f).read_bytes()).hexdigest()
        for f in files
    }
    return {
        "name": name,
        "reference": "urn:dios:adapter:" + name,
        "version": "1",
        "digest": "sha256:" + digest(hashes),
        "visibility": "pinned",
    }


def compile_profile(raw):
    from .work_policy import security_profile
    from .worker import read_profile

    # Copy before stripping Secret descriptors from the public configuration.
    raw = json.loads(json.dumps(raw, allow_nan=False))
    from .package_payloads import validate_binding

    package_lock = validate_binding(raw)
    profile_id, spec = read_profile("immutable-profile", value=raw, package_revision=True)
    mode, refs = security_profile("immutable-profile", raw=raw)
    dependencies = {kind: [] for kind in sorted(DEPENDENCY_KINDS)}
    if package_lock:
        for row in package_lock["packages"]:
            item = row["manifest"]
            dependencies[item["kind"]].append(
                {
                    "name": "package-" + item["name"],
                    "reference": "urn:dios:package:" + item["name"],
                    "version": item["version"],
                    "digest": row["digest"],
                    "visibility": "pinned",
                }
            )
    dependencies["adapter"] = [adapter_descriptor(raw["bridge"])]
    if raw["bridge"] == "jsonl-v1":
        inputs = raw.get("input_mount_targets", [])
        if (
            not isinstance(inputs, list)
            or any(not isinstance(x, str) for x in inputs)
            or len(set(inputs)) != len(inputs)
            or set(inputs) != {m.target for m in spec.mounts}
        ):
            raise RuntimeErrorCode("REVISION_MOUNTS_NOT_PINNED")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", spec.image):
            raise RuntimeErrorCode("REVISION_IMAGE_NOT_PINNED")
        dependencies["image"] = [
            {
                "name": "runtime",
                "reference": spec.image,
                "version": None,
                "digest": spec.image,
                "visibility": "pinned",
            }
        ]
        for index, mount in enumerate(raw.get("packaged_mounts", [])):
            dependencies["harness"].append(
                {
                    "name": "mount-" + str(index),
                    "reference": "urn:dios:mount:" + mount["digest"],
                    "version": None,
                    "digest": mount["digest"],
                    "visibility": "pinned",
                }
            )
    else:
        dependencies["harness"] = [
            {
                "name": "endpoint",
                "reference": spec.endpoint,
                "version": None,
                "digest": None,
                "visibility": "opaque",
            }
        ]
    # Model choice may be internal to the Harness. Preserve the public profile
    # configuration and explicitly record the lack of an inspectable model build.
    dependencies["model"] = [
        {
            "name": "harness-model",
            "reference": "urn:dios:harness-selected-model",
            "version": None,
            "digest": None,
            "visibility": "opaque",
        }
    ]
    raw.pop("secrets", None)
    return {
        "schema_version": 1,
        "definition_id": profile_id,
        "implementation_version": 1,
        "configuration": {
            "format": FORMAT,
            "execution_profile": raw,
            "secret_bindings": refs,
            "profile_digest": spec.fingerprint(),
            "governance_mode": mode,
        },
        "dependencies": dependencies,
        "secret_refs": sorted({x["ref"] for x in refs}),
        "artifact_refs": [],
    }


def decode(record):
    from .work_policy import security_profile
    from .worker import read_profile

    value = record["manifest"]
    config = value["configuration"]
    if (
        set(config)
        != {
            "format",
            "execution_profile",
            "secret_bindings",
            "profile_digest",
            "governance_mode",
        }
        or config["format"] != FORMAT
    ):
        raise RuntimeErrorCode("REVISION_EXECUTION_PROFILE_REQUIRED")
    raw = dict(config["execution_profile"], secrets=config["secret_bindings"])
    profile_id, spec = read_profile("immutable-profile", value=raw, package_revision=True)
    mode, refs = security_profile("immutable-profile", raw=raw)
    if (
        profile_id != value["definition_id"]
        or spec.fingerprint() != config["profile_digest"]
        or mode != config["governance_mode"]
        or sorted({x["ref"] for x in refs}) != value["secret_refs"]
    ):
        raise RuntimeErrorCode("REVISION_EXECUTION_BINDING_INVALID")
    if value["dependencies"]["adapter"] != [adapter_descriptor(raw["bridge"])]:
        raise RuntimeErrorCode("REVISION_ADAPTER_VERSION_MISMATCH")
    expected = compile_profile(raw)
    if expected != value:
        raise RuntimeErrorCode("REVISION_EXECUTION_BINDING_INVALID")
    return raw, profile_id, spec, mode, refs


async def load_profile(db, scope, revision_ref):
    record = await Revisions(db, scope).show(revision_ref, include_manifest=True)
    return decode(record)


async def bind_input(c, scope, revision_ref, value, *, authorized):
    await require(c)
    row = (
        (
            await c.execute(
                select(revisions).where(
                    revisions.c.scope == scope, revisions.c.id == revision_ref
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise RuntimeErrorCode("NOT_FOUND")
    record = representation(row, include_manifest=True)
    raw, profile_id, spec, mode, refs = decode(record)
    if (
        value.get("runtime_profile") != profile_id
        or value.get("profile_digest") != spec.fingerprint()
        or value.get("secret_refs", []) != refs
    ):
        raise RuntimeErrorCode("REVISION_EXECUTION_BINDING_INVALID")
    if mode == "e2ag" and not authorized:
        raise RuntimeErrorCode("WORK_AUTHORIZATION_REQUIRED")
    if "package_lock" in raw:
        if mode != "e2ag" or not authorized:
            raise RuntimeErrorCode("WORK_AUTHORIZATION_REQUIRED")
        from .package_admission import require_lock

        value = dict(
            value, package_admissions=await require_lock(c, scope, raw["package_lock"])
        )
    return dict(
        value,
        revision_ref=revision_ref,
        revision_digest=record["revision"]["content_digest"],
    )


async def publish_profile(db, scope, path, key, actor):
    def read_source():
        with Path(path).open("rb") as source:
            return source.read(65537)

    data = await asyncio.to_thread(read_source)
    if len(data) > 65536:
        raise RuntimeErrorCode("INVALID_PROFILE_CATALOG")
    raw = json.loads(data)
    from .work_policy import security_profile
    from .worker import read_profile

    read_profile(path, value=raw, allow_unfrozen_packages=True)
    security_profile(path, raw=raw)
    freeze_console(raw)
    if raw.get("bridge") == "jsonl-v1":
        import docker
        from docker.errors import ImageNotFound

        def resolve():
            from .package_payloads import prepare

            with prepare(raw, path):
                client = docker.from_env(timeout=5)
                try:
                    raw["launch"]["image"] = client.images.get(
                        raw["launch"]["image"]
                    ).id
                    from .revision_mounts import package

                    package(client, raw)
                except ImageNotFound:
                    raise RuntimeErrorCode("RUNTIME_IMAGE_MISSING") from None
                finally:
                    client.close()

        await asyncio.to_thread(resolve)
    return await Revisions(db, scope).create(key, compile_profile(raw), actor=actor)


def freeze_console(raw):
    """Include the input contract and samples in the published configuration."""
    from app.services.task_inputs import catalog_value, registration

    display = raw.get("console", {})
    if display.get("enabled") is True:
        try:
            value = catalog_value(display)
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
        ):
            raise RuntimeErrorCode("INVALID_INPUT_CATALOG") from None
        display.pop("input_catalog", None)
        display["input_catalog_data"] = value
        registration(display)


def read_export(path, scope):
    """Verify a public CLI export on a Worker without database credentials."""
    from .revisions import hashes, manifest

    try:
        with Path(path).open("rb") as stream:
            data = stream.read(300001)
        if len(data) > 300000:
            raise ValueError()
        record = json.loads(data)
        value = manifest(record["manifest"])
        content, config = hashes(value)
        ref = "rev-" + digest([scope, value["definition_id"], content])
        expected = record["revision"]
        if (expected["id"], expected["content_digest"], expected["config_digest"]) != (
            ref,
            content,
            config,
        ):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise RuntimeErrorCode("REVISION_INTEGRITY_FAILED") from None
    return ref, decode(record)
