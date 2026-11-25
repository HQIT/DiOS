"""Current identity and object policy at the actual storage read boundary."""

import asyncio
import hashlib

from app.runtime.database import RuntimeErrorCode
from app.runtime.object_access import ObjectPolicies, subject
from app.runtime.objects import Objects, public


class ObjectReader:
    def __init__(self, console):
        self.console = console
        self.objects = Objects(console.db, console.scope)
        self.policies = ObjectPolicies(console.db, console.scope)

    async def authorize(self, value, action="object.read", *, audit=True):
        s = self.console
        refresh = getattr(s, "credential_recheck", None)
        if refresh:
            await refresh()
        if s.principal is not None:
            await asyncio.to_thread(s.access_row)
        binding = value["binding"]
        if binding:
            await s.authorize(binding["run_id"], "run.read")
            if value["ref"]["kind"] == "artifact":
                await s.authorize(binding["run_id"], "artifact.read")
        await self.policies.check(value["ref"], s.principal, action, audit=audit)

    async def get(self, ref):
        value = await self.objects.get(ref)
        await self.authorize(value)
        return value

    async def prepare(self, ref):
        value = await self.get(ref)
        if value["storage"] is None:
            raise RuntimeErrorCode("OBJECT_CONTENT_UNAVAILABLE")
        # Verify complete immutable content through the same checked read boundary.
        async for _ in self.stream(value):
            pass
        return value

    async def stream(self, value):
        s = self.console
        storage = value["storage"]
        descriptor = value["descriptor"]
        if not storage:
            raise RuntimeErrorCode("OBJECT_CONTENT_UNAVAILABLE")
        if storage["provider"] != s.provider.name:
            raise RuntimeErrorCode("ARTIFACT_PROVIDER_UNAVAILABLE")
        await self.authorize(value, "object.read")
        await self.authorize(value, "object.download")
        stream = iter(s.provider.read(storage["key"]))
        hashed = hashlib.sha256()
        count = 0
        try:
            while True:
                await self.authorize(value, "object.read", audit=False)
                await self.authorize(value, "object.download", audit=False)
                chunk = await asyncio.to_thread(next, stream, None)
                if chunk is None:
                    break
                # A storage read may block while revocation commits.
                await self.authorize(value, "object.read", audit=False)
                await self.authorize(value, "object.download", audit=False)
                hashed.update(chunk)
                count += len(chunk)
                if count > descriptor["size"]:
                    raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
                yield chunk
            if (
                count != descriptor["size"]
                or "sha256:" + hashed.hexdigest() != descriptor["digest"]
            ):
                raise RuntimeErrorCode("ARTIFACT_DIGEST_MISMATCH")
        finally:
            if hasattr(stream, "close"):
                await asyncio.to_thread(stream.close)

    async def versions(self, kind, object_id, *, after=0, limit=50):
        result = await self.objects.versions(kind, object_id, after=after, limit=limit)
        visible = []
        for item in result["items"]:
            try:
                await self.authorize(item, audit=False)
            except RuntimeErrorCode as exc:
                if exc.code != "NOT_FOUND":
                    raise
            else:
                visible.append(public(item))
        # Cursor covers scanned versions; an empty filtered page can precede a grant.
        return dict(result, items=visible)

    async def relations(self, ref, *, after="", limit=50):
        await self.get(ref)
        result = await self.objects.relations(ref, after=after, limit=limit)
        visible = []
        for item in result["items"]:
            if "kind" in item["target"]:
                try:
                    await self.get(item["target"])
                except RuntimeErrorCode as exc:
                    if exc.code != "NOT_FOUND":
                        raise
                    continue
            visible.append(item)
        return dict(result, items=visible)

    async def resolve_grants(self, grants):
        provider = self.console.identity_provider()
        if provider is None:
            raise RuntimeErrorCode("AUTH_PROVIDER_UNAVAILABLE")
        document = await asyncio.to_thread(provider.document)
        result = []
        for grant in grants:
            row = document["principals"].get(grant.principal_id)
            if not row or not row["enabled"] or row["scope"] != self.console.scope:
                raise RuntimeErrorCode("INVALID_OBJECT_SUBJECT")
            binding = provider.binding(document, grant.principal_id, row)
            result.append(
                {
                    "principal": subject(binding),
                    "actions": grant.actions,
                    "expires_at": min(grant.expires_at, row["expires_at"]),
                }
            )
        return result
