"""Scoped immutable package catalog over existing content-addressed Objects."""

import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path

from packaging.utils import canonicalize_version
from sqlalchemy import select

from .database import RuntimeErrorCode
from .object_lifetime import ObjectLifetime
from .objects import Objects
from .package_lock import MAX_CATALOG, manifest, resolve, verify
from .package_registry_schema import packages, receipts, require
from .store import digest, identifier


def package_digest(value):
    return "sha256:" + digest(value)


def version_key(value):
    # PEP 440 compares 1.0 and 1.0.0 equally; a catalog must not contain both.
    return canonicalize_version(value)


class PackageRegistry:
    def __init__(self, db, scope):
        identifier(scope)
        self.db, self.scope = db, scope

    async def _get(self, c, reference):
        if not isinstance(reference, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", reference
        ):
            raise RuntimeErrorCode("INVALID_PACKAGE_DIGEST")
        row = (
            (
                await c.execute(
                    select(packages).where(
                        packages.c.scope == self.scope, packages.c.digest == reference
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise RuntimeErrorCode("PACKAGE_NOT_REGISTERED")
        return dict(row)

    async def show(self, reference):
        async with self.db.read(self.scope) as c:
            await require(c)
            return await self._get(c, reference)

    async def _receipt(self, c, key, request):
        old = (
            (
                await c.execute(
                    select(receipts).where(
                        receipts.c.scope == self.scope, receipts.c.key == key
                    )
                )
            )
            .mappings()
            .first()
        )
        if old:
            if old["request_digest"] != request:
                raise RuntimeErrorCode("IDEMPOTENCY_CONFLICT")
            return await self._get(c, old["package_digest"])

    async def register(self, value, blobs, provider, *, key, actor):
        from .package_payloads import materialize

        value = manifest(value)
        identifier(key)
        identifier(actor)
        ref = package_digest(value)
        request = digest({"digest": ref, "actor": actor})
        async with self.db.read(self.scope) as c:
            await require(c)
            old = await self._receipt(c, key, request)
            if old:
                return old
        # Intake verifies this package's bytes; dependency closure is resolved
        # separately when publishing a lock, never implicitly during registration.
        intake = resolve(
            [dict(value, dependencies=[])], [{"name": value["name"], "specifier": ""}]
        )
        refs = []
        with tempfile.TemporaryDirectory(prefix="dios-registry-intake-") as temp:
            await asyncio.to_thread(materialize, intake, blobs, temp)
            for file in value["files"]:
                content = (Path(temp) / value["name"] / file["path"]).read_bytes()
                saved = await Objects(self.db, self.scope).publish(
                    provider,
                    [content],
                    "package-" + digest([key, ref, file["path"]]),
                    actor,
                    media_type=file["mediaType"],
                )
                refs.append({"path": file["path"], "ref": saved["ref"]})
        async with self.db.transaction(self.scope) as c:
            await require(c)
            old = await self._receipt(c, key, request)
            if old:
                return old
            collision = (
                (
                    await c.execute(
                        select(packages).where(
                            packages.c.scope == self.scope,
                            packages.c.name == value["name"],
                            packages.c.version == version_key(value["version"]),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if collision and collision["digest"] != ref:
                raise RuntimeErrorCode("PACKAGE_VERSION_IMMUTABLE")
            if not collision:
                row = {
                    "scope": self.scope,
                    "digest": ref,
                    "name": value["name"],
                    "version": version_key(value["version"]),
                    "manifest": value,
                    "files": refs,
                    "actor": actor,
                    "created_at": await self.db.now(c),
                }
                await c.execute(packages.insert().values(**row))
            await c.execute(
                receipts.insert().values(
                    scope=self.scope,
                    key=key,
                    request_digest=request,
                    package_digest=ref,
                )
            )
            return await self._get(c, ref)

    async def lock(self, roots):
        async with self.db.read(self.scope) as c:
            await require(c)
            catalog = (
                (
                    await c.execute(
                        select(packages.c.manifest)
                        .where(packages.c.scope == self.scope)
                        .order_by(packages.c.digest)
                        .limit(MAX_CATALOG + 1)
                    )
                )
                .scalars()
                .all()
            )
        return resolve(catalog, roots)

    async def _fetch(self, lock, provider, destination):
        """Resolve scoped object refs, then verify bounded bytes while streaming."""
        files = []
        async with self.db.read(self.scope) as c:
            await require(c)
            for item in lock["packages"]:
                row = await self._get(c, item["digest"])
                if row["manifest"] != item["manifest"]:
                    raise RuntimeErrorCode("PACKAGE_REGISTRY_INTEGRITY")
                refs = {x["path"]: x["ref"] for x in row["files"]}
                for file in item["manifest"]["files"]:
                    ref = refs[file["path"]]
                    await ObjectLifetime(self.db, self.scope).available(c, ref)
                    obj = await Objects(self.db, self.scope)._get(c, ref)
                    if (
                        obj["descriptor"]
                        != {
                            "digest": file["digest"],
                            "size": file["size"],
                            "media_type": file["mediaType"],
                        }
                        or obj["storage"]["provider"] != provider.name
                    ):
                        raise RuntimeErrorCode("PACKAGE_REGISTRY_INTEGRITY")
                    files.append((file, obj["storage"]["key"]))

        def copy():
            for file, key in files:
                target = Path(destination) / file["digest"].split(":")[1]
                if target.exists():
                    continue
                h, size = hashlib.sha256(), 0
                with target.open("xb") as out:
                    for chunk in provider.read(key):
                        size += len(chunk)
                        if size > file["size"]:
                            raise RuntimeErrorCode("PACKAGE_PAYLOAD_MISMATCH")
                        h.update(chunk)
                        out.write(chunk)
                if size != file["size"] or "sha256:" + h.hexdigest() != file["digest"]:
                    raise RuntimeErrorCode("PACKAGE_PAYLOAD_MISMATCH")

        await asyncio.to_thread(copy)

    async def export(self, lock, provider, destination):
        """Export verified files without building images or starting services."""
        import os

        lock = verify(lock)
        target = Path(destination).absolute()
        if target.exists() or target.is_symlink():
            raise RuntimeErrorCode("PACKAGE_EXPORT_EXISTS")
        # All staging lives in the target filesystem; failed exports leave no bundle.
        with tempfile.TemporaryDirectory(
            prefix=".dios-package-", dir=target.parent
        ) as temp:
            staging = Path(temp) / "bundle"
            (staging / "blobs").mkdir(parents=True)
            await self._fetch(lock, provider, staging / "blobs")
            (staging / "lock.json").write_text(
                json.dumps(lock, ensure_ascii=False), encoding="utf-8"
            )
            if target.exists() or target.is_symlink():
                raise RuntimeErrorCode("PACKAGE_EXPORT_EXISTS")
            os.rename(staging, target)
        return {
            "lock_digest": lock["digest"],
            "packages": len(lock["packages"]),
            "destination": str(target),
        }

    async def publish_profile(self, lock, profile, provider, *, key, actor):
        from .execution_revisions import publish_profile
        from .package_admission import require_lock
        from .package_lock import read_json

        lock = verify(lock)
        async with self.db.read(self.scope) as c:
            await require_lock(c, self.scope, lock)
        raw = read_json(profile)
        if (
            not isinstance(raw, dict)
            or "package_source" in raw
            or "package_lock" in raw
        ):
            raise RuntimeErrorCode("PACKAGE_PROFILE_SOURCE_CONFLICT")
        with tempfile.TemporaryDirectory(prefix="dios-registry-publish-") as temp:
            root = Path(temp)
            await self.export(lock, provider, root / "bundle")
            raw["package_source"] = {
                "lock": str(root / "bundle/lock.json"),
                "blobs": str(root / "bundle/blobs"),
            }
            path = root / "profile.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            return await publish_profile(self.db, self.scope, path, key, actor)
