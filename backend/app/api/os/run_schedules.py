from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute, Submit
from app.api.os.upgrade import control_operator
from app.config import settings
from app.services.run_schedules import RunSchedules

router = APIRouter(
    prefix="/runtime/run-schedules",
    tags=["runtime"],
    route_class=RuntimeRoute,
    dependencies=[Depends(control_operator)],
)


async def service(request: Request):
    console = getattr(request.app.state, "runtime_console", None)
    if console is None:
        raise HTTPException(503, detail="RUNTIME_CONFIGURATION_REQUIRED")
    return RunSchedules(console.for_principal(None))


Service = Annotated[RunSchedules, Depends(service)]


class Create(Submit):
    cron_expression: str = Field(min_length=1, max_length=64)


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    enabled: bool = Field(strict=True)
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class Reauthorize(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class Resolve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["retry", "discard"]
    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=160)


@router.get("")
async def listing(
    s: Service,
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=32),
):
    return {
        **await s.list(limit, before),
        "scheduler_enabled": settings.runtime_schedules_enabled,
    }


@router.post("", status_code=201)
async def create(body: Create, s: Service):
    value = body.model_dump()
    expression = value.pop("cron_expression")
    key = value.pop("idempotency_key")
    return await s.create(value, expression, key)


@router.post("/{identity}/control")
async def control(identity: str, body: Control, s: Service):
    return await s.control(
        identity, body.expected_version, body.enabled, body.actor, body.reason
    )


@router.post("/{identity}/reauthorize")
async def reauthorize(identity: str, body: Reauthorize, s: Service):
    return await s.reauthorize(identity, body.expected_version, body.actor, body.reason)


@router.get("/{identity}/occurrences")
async def history(
    identity: str,
    s: Service,
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=32),
):
    return await s.history(identity, limit, before)


@router.post("/{identity}/occurrences/{item_id}/resolve")
async def resolve(identity: str, item_id: str, body: Resolve, s: Service):
    return await s.resolve(
        identity, item_id, body.action, body.actor, body.reason, body.idempotency_key
    )
