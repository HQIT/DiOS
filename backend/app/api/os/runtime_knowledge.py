"""Business-neutral knowledge API; caller identity comes from runtime service."""
from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from app.services.knowledge import CAPABILITIES, ObjectKnowledgeProvider


class KnowledgeQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=3, ge=1, le=20, strict=True)


class KnowledgeSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=160)
    state: str = 'active'
    object_ref: dict | None = None
    provenance: dict | None = None
    expires_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)


def register(router, service):
    dependency = Depends(service)

    @router.get('/knowledge/capabilities')
    async def capabilities(s=dependency):
        return CAPABILITIES

    @router.post('/knowledge/search')
    async def search(body: KnowledgeQuery, s=dependency):
        return await ObjectKnowledgeProvider(s).search(body.query, body.limit)

    @router.get('/knowledge/sources/{source_id}')
    async def source(source_id: str, s=dependency):
        return await ObjectKnowledgeProvider(s).source(source_id)

    @router.put('/knowledge/sources/{source_id}')
    async def change(source_id: str, body: KnowledgeSource, s=dependency):
        return await ObjectKnowledgeProvider(s).change(source_id, body.model_dump())
