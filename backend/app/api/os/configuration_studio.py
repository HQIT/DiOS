import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute, service
from app.api.os.upgrade import control_operator
from app.config import settings
from app.runtime.tool_catalog import FileApprovers
from app.services import configuration_studio as studio
from app.services.studio_release import StudioReleases
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


def releases(console):
    provider = (
        FileApprovers(settings.runtime_approvers_file)
        if settings.runtime_approvers_file
        else None
    )
    return StudioReleases(console, provider)


class ReleaseRequest(Compare):
    deployment_id: str = Field(min_length=1, max_length=160)
    deployment_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)
    sample_id: str | None = Field(default=None, min_length=1, max_length=160)
    input_payload: dict | None = None


class ReleaseVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ReleaseDecision(ReleaseVersion):
    decision: str = Field(pattern="^(approved|denied)$")
    reason: str = Field(default="", max_length=2000)


@router.get("/releases")
async def release_list(console: Console, after: str = Query("", max_length=160)):
    return {"items": await releases(console).list(after)}


@router.get("/releases/{release_id}")
async def release_detail(release_id: str, console: Console):
    return await releases(console).show(release_id)


@router.post("/releases", status_code=202)
async def release_request(value: ReleaseRequest, console: Console):
    return await releases(console).request(
        value.base,
        value.draft,
        value.deployment_id,
        value.deployment_version,
        value.idempotency_key,
        "operator:console",
        value.sample_id,
        value.input_payload,
    )


@router.get("/releases/{release_id}/probe")
async def release_probe(release_id: str, console: Console):
    return await releases(console).probe(release_id)


@router.post("/releases/{release_id}/approval")
async def release_approval(
    release_id: str, value: ReleaseDecision, request: Request, console: Console
):
    return await releases(console).decide(
        release_id,
        value.expected_version,
        value.decision,
        request.headers.get("x-dios-approver-token", ""),
        value.reason,
    )


@router.post("/releases/{release_id}/publish", status_code=202)
async def release_publish(release_id: str, value: ReleaseVersion, console: Console):
    return await releases(console).publish(
        release_id, value.expected_version, "operator:console"
    )
