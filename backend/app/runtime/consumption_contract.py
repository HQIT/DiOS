"""Shared CLI and HTTP consumption input contracts."""

from pydantic import BaseModel, ConfigDict, Field


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160)
    selector: dict
    unit: str
    maximum: int
    per_attempt: int
    expected_revision: int
    key: str = Field(min_length=1, max_length=160)
    actor: str = Field(min_length=1, max_length=160)
    enforcement: str = "required"
    enabled: bool = True


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    account_id: str = Field(min_length=1, max_length=160)
    attempt_id: str = Field(min_length=1, max_length=160)
    actual: int
    evidence: dict
    key: str = Field(min_length=1, max_length=160)
    actor: str = Field(min_length=1, max_length=160)
