"""Object metadata endpoints; native references retain original Run permissions."""

import base64

from fastapi import Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.runtime.database import RuntimeErrorCode
from app.runtime.objects import Objects, public, reference
from app.runtime.object_access import ObjectPolicies
from app.services.object_access import ObjectReader
from app.runtime.object_lifetime import ObjectLifetime


class FileVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_base64: str = Field(max_length=1398104)
    idempotency_key: str = Field(min_length=1, max_length=160)
    object_id: str | None = None
    expected_version: int = Field(default=0, ge=0)
    media_type: str = "application/octet-stream"


class ObjectLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: dict
    relation: str
    target: dict


class ObjectGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    principal_id: str = Field(min_length=1, max_length=160)
    actions: list[str] = Field(min_length=1, max_length=2)
    expires_at: float = Field(allow_inf_nan=False)


class ObjectPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grants: list[ObjectGrant] = Field(max_length=128)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ObjectRetention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    retain_until: float = Field(default=0, ge=0, allow_inf_nan=False)
    expires_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ObjectDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=160)


def operator_only(service):
    if service.principal is not None:
        raise RuntimeErrorCode("AUTH_OPERATOR_REQUIRED")


async def authorized(service, kind, object_id, version):
    return await ObjectReader(service).get(reference(kind, object_id, version))


def register(router, service):
    dependency = Depends(service)

    @router.get("/objects/{kind}/{object_id}/versions/{version}/lifecycle")
    async def object_lifecycle(kind: str, object_id: str, version: int, s=dependency):
        operator_only(s)
        return await ObjectLifetime(s.db, s.scope).show(
            reference(kind, object_id, version)
        )

    @router.put("/objects/{kind}/{object_id}/versions/{version}/retention")
    async def object_retention(
        kind: str, object_id: str, version: int, body: ObjectRetention, s=dependency
    ):
        operator_only(s)
        return await ObjectLifetime(s.db, s.scope).change(
            reference(kind, object_id, version),
            expected_revision=body.expected_revision,
            key=body.idempotency_key,
            actor="operator:runtime-api",
            retain_until=body.retain_until,
            expires_at=body.expires_at,
        )

    @router.post("/objects/{kind}/{object_id}/versions/{version}/delete")
    async def object_delete(
        kind: str, object_id: str, version: int, body: ObjectDelete, s=dependency
    ):
        operator_only(s)
        return await ObjectLifetime(s.db, s.scope).change(
            reference(kind, object_id, version),
            expected_revision=body.expected_revision,
            key=body.idempotency_key,
            actor="operator:runtime-api",
            delete=True,
            reason=body.reason,
        )

    @router.post("/objects/files")
    async def publish_file(body: FileVersion, s=dependency):
        operator_only(s)
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except ValueError:
            raise RuntimeErrorCode("INVALID_FILE_ENCODING") from None
        if len(data) > 1024 * 1024:
            raise RuntimeErrorCode("INVALID_FILE_SIZE")
        value = await Objects(s.db, s.scope).publish(
            s.provider,
            [data],
            body.idempotency_key,
            "operator:runtime-api",
            object_id=body.object_id,
            expected_version=body.expected_version,
            media_type=body.media_type,
        )
        return public(value)

    @router.get("/objects/{kind}/{object_id}/versions")
    async def object_versions(
        kind: str, object_id: str, after: int = 0, limit: int = 50, s=dependency
    ):
        return await ObjectReader(s).versions(kind, object_id, after=after, limit=limit)

    @router.get("/objects/{kind}/{object_id}/versions/{version}")
    async def object_version(kind: str, object_id: str, version: int, s=dependency):
        return public(await authorized(s, kind, object_id, version))

    @router.get("/objects/{kind}/{object_id}/versions/{version}/relations")
    async def object_relations(
        kind: str,
        object_id: str,
        version: int,
        after: str = "",
        limit: int = 50,
        s=dependency,
    ):
        return await ObjectReader(s).relations(
            reference(kind, object_id, version), after=after, limit=limit
        )

    @router.get("/objects/{kind}/{object_id}/versions/{version}/download")
    async def object_download(kind: str, object_id: str, version: int, s=dependency):
        reader = ObjectReader(s)
        value = await reader.prepare(reference(kind, object_id, version))
        return StreamingResponse(
            reader.stream(value),
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(value["descriptor"]["size"]),
                "Content-Disposition": 'attachment; filename="object.bin"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/objects/{kind}/{object_id}/versions/{version}/policy")
    async def object_policy(kind: str, object_id: str, version: int, s=dependency):
        operator_only(s)
        return await ObjectPolicies(s.db, s.scope).show(
            reference(kind, object_id, version)
        )

    @router.put("/objects/{kind}/{object_id}/versions/{version}/policy")
    async def object_policy_change(
        kind: str, object_id: str, version: int, body: ObjectPolicy, s=dependency
    ):
        operator_only(s)
        grants = (
            await ObjectReader(s).resolve_grants(body.grants) if body.grants else []
        )
        return await ObjectPolicies(s.db, s.scope).change(
            reference(kind, object_id, version),
            grants,
            body.expected_revision,
            body.idempotency_key,
            "operator:runtime-api",
        )

    @router.post("/objects/relations")
    async def object_link(body: ObjectLink, s=dependency):
        operator_only(s)
        return await Objects(s.db, s.scope).link(
            body.source, body.relation, body.target, "operator:runtime-api"
        )
