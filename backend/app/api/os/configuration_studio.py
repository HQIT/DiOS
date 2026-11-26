import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute, service
from app.api.os.upgrade import control_operator
from app.services import configuration_studio as studio
from app.services.task_console import TaskConsole

Console = Annotated[TaskConsole, Depends(service)]

router = APIRouter(
    prefix="/runtime/studio",
    tags=["runtime-studio"],
    route_class=RuntimeRoute,
    dependencies=[Depends(control_operator)],
)


class Text(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=65536)


class Compare(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base: str = Field(max_length=65536)
    draft: str = Field(max_length=65536)


class Suggest(Text):
    profile_id: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)


@router.get("/profiles/{profile_id}")
async def source(profile_id: str, console: Console):
    return await studio.source(console, profile_id)


@router.post("/compare")
async def compare(value: Compare, console: Console):
    return await asyncio.to_thread(studio.compare, value.base, value.draft)


@router.post("/suggestions", status_code=202)
async def suggest(value: Suggest, console: Console):
    return await studio.suggest(
        console, value.profile_id, value.text, value.instruction, value.idempotency_key
    )


@router.post("/suggestions/{run_id}/review")
async def proposal(run_id: str, value: Text, console: Console):
    return await studio.proposal(console, run_id, value.text)
