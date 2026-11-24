from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.os.runtime_tasks import RuntimeRoute
from app.api.os.upgrade import control_operator
from app.db.database import get_db
from app.services import schedule_control

router = APIRouter(
    prefix="/runtime/schedules",
    route_class=RuntimeRoute,
    dependencies=[Depends(control_operator)],
    tags=["runtime"],
)


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["retry", "discard"]
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    idempotency_key: str = Field(min_length=1, max_length=128)


@router.get("")
async def list_schedules(db: Annotated[AsyncSession, Depends(get_db)]):
    return await schedule_control.list_schedules(db)


@router.get("/{subscription_id}/occurrences")
async def history(
    subscription_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(50, ge=1, le=100),
    before: str | None = Query(None, max_length=32),
):
    return await schedule_control.history(
        db, subscription_id, limit=limit, before=before
    )


@router.post("/occurrences/{occurrence_id}/resolve")
async def resolve(occurrence_id: str, body: Resolution):
    return await schedule_control.resolve(occurrence_id, **body.model_dump())
