import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.os.runtime_tasks import RuntimeRoute
from app.config import settings
from app.middleware.access_token import _extract_token
from app.runtime.database import RuntimeErrorCode
from app.services.managed_upgrade import execute


async def control_operator(request: Request):
    if not settings.access_token:
        raise HTTPException(503, detail="RUNTIME_OPERATOR_TOKEN_REQUIRED")
    supplied = _extract_token(request) or ""
    if not secrets.compare_digest(supplied.encode(), settings.access_token.encode()):
        raise HTTPException(401, detail="Invalid or missing access token")


router = APIRouter(
    prefix="/upgrade",
    tags=["upgrade"],
    dependencies=[Depends(control_operator)],
    route_class=RuntimeRoute,
)


async def invoke(command, **kwargs):
    try:
        return await execute(command, **kwargs)
    except RuntimeErrorCode as exc:
        raise HTTPException(
            409
            if exc.code in {"VERSION_CONFLICT", "ACTIVE_OR_UNKNOWN_EXECUTION"}
            else 503,
            detail=exc.code,
        ) from None


@router.get("/status")
async def status():
    return await invoke("status")


@router.post("/drain")
async def drain():
    return await invoke("drain")


@router.post("/settle-services")
async def settle_services():
    return await invoke("settle-services")


class Resume(BaseModel):
    revision: int = Field(ge=1)
    kernel_revision: int | None = Field(default=None, ge=0)


@router.post("/resume")
async def resume(body: Resume):
    return await invoke(
        "resume", revision=body.revision, kernel_revision=body.kernel_revision
    )
