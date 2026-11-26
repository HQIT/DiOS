"""Operator-facing technical evaluation and environment promotion contracts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute, service
from app.api.os.upgrade import control_operator
from app.config import settings
from app.runtime import technical_records as ledger
from app.runtime.environment_promotions import Promotions
from app.runtime.technical_evaluations import Evaluations
from app.runtime.tool_catalog import FileApprovers
from app.services.task_console import TaskConsole

Console = Annotated[TaskConsole, Depends(service)]
router = APIRouter(
    prefix="/runtime/technical",
    tags=["technical-releases"],
    route_class=RuntimeRoute,
    dependencies=[Depends(control_operator)],
)


def evaluations(console):
    return Evaluations(console.db, console.scope, console.policy, console.provider)


def promotions(console):
    approvers = (
        FileApprovers(settings.runtime_approvers_file)
        if settings.runtime_approvers_file
        else None
    )
    return Promotions(console.db, console.scope, console.policy, approvers)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Put(Strict):
    document: dict
    expected_version: int = Field(ge=0)


class Binding(Strict):
    environment_id: str = Field(min_length=1, max_length=100)
    definition_id: str = Field(min_length=1, max_length=160)


class Evaluate(Strict):
    revision_ref: str = Field(min_length=1, max_length=160)
    suite_ref: dict
    idempotency_key: str = Field(min_length=1, max_length=160)


class Promote(Strict):
    deployment_id: str = Field(min_length=1, max_length=160)
    evaluation_id: str = Field(min_length=1, max_length=160)
    deployment_version: int = Field(ge=0)
    source_deployment: str | None = Field(default=None, max_length=160)
    external_approval: dict | None = None
    idempotency_key: str = Field(min_length=1, max_length=160)
    rollback: bool = False


class Version(Strict):
    expected_version: int = Field(ge=1)


class Decision(Version):
    decision: str = Field(pattern="^(approved|denied)$")
    reason: str = Field(default="", max_length=2000)


@router.get("/records/{kind}")
async def listing(kind: str, console: Console, after: str = Query("", max_length=160)):
    async with console.db.read(console.scope) as c:
        rows = await ledger.page(c, console.scope, kind, after)
    return {
        "items": [dict(row, ref=ledger.reference(row)) for row in rows],
        "next_after": rows[-1]["id"] if len(rows) == 30 else None,
    }


@router.get("/records/{kind}/{record_id}")
async def detail(
    kind: str, record_id: str, console: Console, version: int | None = Query(None, ge=1)
):
    async with console.db.read(console.scope) as c:
        row = await ledger.get(c, console.scope, kind, record_id, version)
    return dict(row, ref=ledger.reference(row))


@router.put("/suites/{suite_id}")
async def put_suite(suite_id: str, value: Put, console: Console):
    return await evaluations(console).put_suite(
        suite_id, value.document, value.expected_version, "operator:console"
    )


@router.put("/environments/{environment_id}")
async def put_environment(environment_id: str, value: Put, console: Console):
    return await promotions(console).environment(
        environment_id, value.document, value.expected_version, "operator:console"
    )


@router.put("/bindings/{deployment_id}")
async def bind(deployment_id: str, value: Binding, console: Console):
    return await promotions(console).bind(
        deployment_id, value.environment_id, value.definition_id, "operator:console"
    )


@router.post("/evaluations", status_code=202)
async def evaluate(value: Evaluate, console: Console):
    return await evaluations(console).start(
        value.revision_ref, value.suite_ref, value.idempotency_key, "operator:console"
    )


@router.post("/evaluations/{evaluation_id}/reconcile")
async def reconcile(evaluation_id: str, console: Console):
    return await evaluations(console).reconcile(evaluation_id)


@router.get("/comparison")
async def comparison(
    console: Console,
    baseline: str = Query(..., max_length=160),
    candidate: str = Query(..., max_length=160),
):
    return await evaluations(console).compare(baseline, candidate)


@router.post("/promotions", status_code=202)
async def promote(value: Promote, console: Console):
    return await promotions(console).request(
        value.deployment_id,
        value.evaluation_id,
        value.deployment_version,
        value.source_deployment,
        value.external_approval,
        value.idempotency_key,
        "operator:console",
        rollback=value.rollback,
    )


@router.post("/promotions/{promotion_id}/approval")
async def decide(
    promotion_id: str, value: Decision, request: Request, console: Console
):
    return await promotions(console).decide(
        promotion_id,
        value.expected_version,
        value.decision,
        request.headers.get("x-dios-approver-token", ""),
        value.reason,
    )


@router.post("/promotions/{promotion_id}/publish", status_code=202)
async def publish(promotion_id: str, value: Version, console: Console):
    return await promotions(console).publish(
        promotion_id, value.expected_version, "operator:console"
    )
