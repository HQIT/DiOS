from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.models.tables import LLMModel
from app.models.schemas import LLMModelCreate, LLMModelUpdate, LLMModelOut

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[LLMModelOut])
async def list_models(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(LLMModel).order_by(LLMModel.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=LLMModelOut, status_code=201)
async def create_model(body: LLMModelCreate, db: AsyncSession = Depends(get_db)):
    m = LLMModel(
        name=body.name, provider=body.provider, model=body.model,
        base_url=body.base_url, api_key=body.api_key,
        display_name=body.display_name, description=body.description,
        context_length=body.context_length,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


@router.get("/{model_id}", response_model=LLMModelOut)
async def get_model(model_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(LLMModel, model_id)
    if not m:
        raise HTTPException(404, "Model not found")
    return m


@router.put("/{model_id}", response_model=LLMModelOut)
async def update_model(model_id: str, body: LLMModelUpdate, db: AsyncSession = Depends(get_db)):
    m = await db.get(LLMModel, model_id)
    if not m:
        raise HTTPException(404, "Model not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(m, field, value)
    await db.commit()
    await db.refresh(m)
    return m


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(LLMModel, model_id)
    if not m:
        raise HTTPException(404, "Model not found")
    await db.delete(m)
    await db.commit()
