from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class FactorDefinitionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=80)
    expression: str = Field(min_length=1, max_length=2000)


class FactorCreateRequest(FactorDefinitionRequest):
    request_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=200)
    confirmed: bool = False
