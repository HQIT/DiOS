from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.os.runtime_tasks import RuntimeRoute
from app.api.os.upgrade import control_operator
from app.runtime.database import RuntimeErrorCode
from app.services import managed_adoption

router = APIRouter(
    prefix="/runtime/adoptions",
    tags=["runtime"],
    dependencies=[Depends(control_operator)],
    route_class=RuntimeRoute,
)


class Inspect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=32)
    native_id: str = Field(pattern="^[0-9a-f]{64}$")
    workspace: str | None = Field(default=None, max_length=4096)


class Adopt(Inspect):
    expected_digest: str = Field(pattern="^[0-9a-f]{64}$")
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    source_quiesced: bool = Field(strict=True)


async def invoke(operation, **kwargs):
    try:
        return await operation(**kwargs)
    except RuntimeErrorCode as exc:
        raise HTTPException(409, detail=exc.code) from None
    except (OSError, ValueError):
        raise HTTPException(409, detail="ADOPTION_SOURCE_INVALID") from None


@router.post("/preview")
async def preview(body: Inspect):
    return await invoke(managed_adoption.preview, **body.model_dump())


@router.post("")
async def adopt(body: Adopt):
    return await invoke(managed_adoption.adopt, **body.model_dump())
