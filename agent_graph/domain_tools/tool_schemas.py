from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PAPER_AGENT_DISCLAIMER = (
    "This Agent only operates paper-trading and internal app workflows. "
    "It is not investment advice, does not promise returns, does not place real trades, "
    "and does not connect to brokers."
)


class ToolPermission:
    READ = "read"
    PREVIEW = "preview"
    WRITE = "write"


def _dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return dict(value or {})


@dataclass(frozen=True)
class ToolResult:
    success: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    permission: str = ToolPermission.READ
    tool_name: str = ""
    disclaimer: str = PAPER_AGENT_DISCLAIMER
    status: str = ""
    requires_confirmation: bool = False
    confirmation_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StockAnalysisResult:
    stock_code: str
    stock_name: str = ""
    trade_date: str = ""
    current_price: float = 0.0
    original_score: float = 0.0
    original_rank: int | None = None
    model_confidence: str | float = ""
    news_adjustment: float = 0.0
    effective_news_adjustment: float = 0.0
    user_adjustment: float = 0.0
    combined_adjustment: float = 0.0
    target_weight: float = 0.0
    current_weight: float = 0.0
    position_adjustment_ratio: float = 0.0
    ai_reliability_weight: float = 0.0
    news_summary: str = "当前未检索到有效新闻证据"
    announcement_summary: str = ""
    positive_evidence: list[str] = field(default_factory=list)
    negative_evidence: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    triggered_rules: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    evidence_chunk_ids: list[str] = field(default_factory=list)
    suitability_for_user: str = "unknown"
    analysis_conclusion: str = ""
    non_topk_warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PositionRecommendation:
    user_id: str
    stock_code: str
    trade_date: str
    minimum_weight: float = 0.0
    recommended_weight: float = 0.0
    maximum_allowed_weight: float = 0.0
    recommended_amount: float = 0.0
    estimated_quantity: float = 0.0
    lot_size: int = 100
    estimated_cost: float = 0.0
    confidence: str = "medium"
    reason: str = ""
    risk_warning: str = ""
    hard_rejection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplacementCandidate:
    stock_code: str
    stock_name: str = ""
    current_weight: float = 0.0
    recommended_weight_after: float = 0.0
    reduce_weight: float = 0.0
    estimated_sell_quantity: float = 0.0
    replacement_priority_score: float = 0.0
    replacement_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplacementRecommendation:
    user_id: str
    candidate_stock_code: str
    candidate_target_weight: float
    trade_date: str
    replacement_candidates: list[ReplacementCandidate] = field(default_factory=list)
    risk_before: dict[str, Any] = field(default_factory=dict)
    risk_after_estimate: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["replacement_candidates"] = [item.to_dict() for item in self.replacement_candidates]
        return data


@dataclass(frozen=True)
class PaperTradePreview:
    plan_id: str
    confirmation_token: str
    expires_at: str
    user_id: str
    stock_code: str
    stock_name: str
    trade_date: str
    recommended_weight: float
    estimated_quantity: float
    estimated_cost: float
    current_price: float
    funding_sources: list[dict[str, Any]] = field(default_factory=list)
    replacement_stocks: list[dict[str, Any]] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    risk_warning: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PaperTradeExecutionResult:
    success: bool
    plan_id: str
    confirmation_status: str
    execution_status: str
    order_ids: list[str] = field(default_factory=list)
    position_count_after: int = 0
    message: str = ""
    output_paths: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
