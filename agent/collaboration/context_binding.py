from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EntityScope(str, Enum):
    EXPLICIT_ENTITIES = "explicit_entities"
    CONVERSATION_FOCUS = "conversation_focus"
    PORTFOLIO = "portfolio"
    ACCOUNT = "account"
    GLOBAL = "global"
    NONE = "none"


class ReferenceEntityType(str, Enum):
    SECURITY = "security"
    PORTFOLIO = "portfolio"
    ACCOUNT = "account"
    EVENT = "event"
    UNKNOWN = "unknown"
    NONE = "none"


class FreshnessExpectation(str, Enum):
    # 新鲜度期望（复用判断的意图声明，由 decompose LLM 填写）
    LATEST = "latest"  # 必须最新数据：禁止任何复用引用
    REUSABLE = "reusable"  # 旧结果可直接复用
    REFERENCE = "reference"  # 旧结果作为新计算的参考输入（不是直接当答案）
    UNSPECIFIED = "unspecified"  # 未明确（安全默认：不标记复用引用）


@dataclass(frozen=True)
class ContextBinding:
    """Request-local entity/context authority selected by RequestBundle decomposition.

    This is not a routing mode. It only describes which financial object scope
    the current Request is allowed to resolve/inherit, plus the freshness
    expectation and coarse reuse-candidate marks for answer-level reuse.
    """

    entity_scope: EntityScope = EntityScope.NONE
    inherit_previous_focus: bool = False
    reference_entity_type: ReferenceEntityType = ReferenceEntityType.NONE
    reason: str = ""
    freshness_expectation: FreshnessExpectation = FreshnessExpectation.UNSPECIFIED  # 新鲜度期望（四枚举）
    reuse_reference: tuple[str, ...] = ()  # 复用候选轮次显示编号（粗筛标记，可多个，非最终决定）

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_scope": self.entity_scope.value,
            "inherit_previous_focus": bool(self.inherit_previous_focus),
            "reference_entity_type": self.reference_entity_type.value,
            "reason": str(self.reason or ""),
            "freshness_expectation": self.freshness_expectation.value,
            "reuse_reference": list(self.reuse_reference),
        }


__all__ = ["ContextBinding", "EntityScope", "FreshnessExpectation", "ReferenceEntityType"]
