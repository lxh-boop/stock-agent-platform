"""Pydantic schemas for structured LLM outputs (decompose / plan).

These mirror the legacy ``RequestBundle`` / ``CoordinatorPlanner`` contracts in
simplified form. The LLM only ever produces natural-language entity names;
authoritative identifiers stay behind worker tools.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class RequestCategory(StrEnum):
    GENERAL = "general"
    BUSINESS = "business"
    PRESENTATION = "presentation"


class SemanticEntity(BaseModel):
    text: str = Field(description="自然语言实体名，如'贵州茅台'")
    entity_type: str = Field(default="security", description="security|portfolio|account|event|global_market")
    role: str = Field(default="target", description="target|context|constraint")
    source: str = Field(default="explicit", description="explicit|conversation_context")


class SemanticTarget(BaseModel):
    entity_type: str = Field(default="none", description="security|portfolio|account|event|global_market|none|unknown")
    display_text: str = ""
    source: Literal["explicit", "conversation_context", "none"] = "none"
    status: Literal["identified", "missing", "not_required"] = "not_required"


class DecomposedRequest(BaseModel):
    """One normalized request carved out of the raw user message."""

    source_index: int = Field(ge=1, description="1-based位置，对应用户消息中的显式边界")
    category: RequestCategory
    objective: str
    request_type: Literal["read", "write", "none"] = "none"
    proposal_required: bool = False
    semantic_target: SemanticTarget = Field(default_factory=SemanticTarget)
    semantic_entities: list[SemanticEntity] = Field(default_factory=list)
    presentation: dict[str, str] = Field(default_factory=dict, description="language/format/style 等")
    depends_on: list[int] = Field(default_factory=list, description="引用requests数组1-based位置")

    @field_validator("objective")
    @classmethod
    def _objective_not_blank(cls, value: str) -> str:
        if not str(value or "").strip():
            raise ValueError("objective 不能为空")
        return value.strip()


class Decomposition(BaseModel):
    """Output of the decompose node."""

    requests: list[DecomposedRequest] = Field(min_length=1)


class WorkerAssignment(BaseModel):
    """One supervisor-planned unit of work bound to a specialist worker card."""

    worker_id: str = Field(description="必须来自worker_descriptions提供的卡片ID")
    objective: str = Field(description="面向该Worker的明确产出指令")
    request_index: int = Field(ge=1, description="所属Request在requests数组中的1-based位置")


class WorkerPlan(BaseModel):
    """Output of the plan node."""

    tasks: list[WorkerAssignment] = Field(min_length=1)


__all__ = [
    "RequestCategory",
    "SemanticEntity",
    "SemanticTarget",
    "DecomposedRequest",
    "Decomposition",
    "WorkerAssignment",
    "WorkerPlan",
]
