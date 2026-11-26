"""Read-only application registration and exact-revision compatibility inspection."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute, operator, service
from app.api.os.upgrade import control_operator
from app.runtime.application_contracts import compatibility, description
from app.runtime.execution_revisions import load_profile
from app.services.task_console import TaskConsole

router = APIRouter(
    prefix="/runtime",
    tags=["application-contracts"],
    route_class=RuntimeRoute,
)
Console = Annotated[TaskConsole, Depends(service)]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision_ref: str = Field(min_length=1, max_length=160)
    contract: dict | None = None


@router.get("/contracts", dependencies=[Depends(operator)])
async def contracts():
    return description()


@router.get("/applications", dependencies=[Depends(control_operator)])
async def applications(console: Console):
    return {
        "items": [
            row for row in await console.catalog() if row.get("application") is not None
        ]
    }


@router.post("/applications/compatibility", dependencies=[Depends(control_operator)])
async def check(value: Check, console: Console):
    raw, _, _, _, _ = await load_profile(console.db, console.scope, value.revision_ref)
    return dict(
        compatibility(raw, value.contract),
        revision_ref=value.revision_ref,
        registered_contract=value.contract is None,
    )
