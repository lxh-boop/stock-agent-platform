from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.domain_tools._common import (
    first_present,
    is_valid_agent_price,
    latest_trade_date,
    normalize_stock_code,
    parse_jsonish,
    safe_float,
    safe_int,
)
from agent_graph.domain_tools.portfolio_state_tool import query_portfolio_state
from agent_graph.domain_tools.stock_lookup_tool import load_latest_ranking, load_latest_recommendations, lookup_stock
from agent_graph.domain_tools.stock_news_tool import query_stock_news
from agent_graph.domain_tools.stock_rag_tool import query_stock_rag
from agent_graph.domain_tools.tool_schemas import StockAnalysisResult, ToolPermission, ToolResult


def _position_weight(user_id: str, code: str, output_dir: str | Path, db_path: str | Path | None) -> float:
    state = query_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
    for position in state.get("positions") or []:
        if normalize_stock_code(position.get("stock_code")) == code:
            return safe_float(position.get("position_ratio"), 0.0)
    return 0.0


def _analyze_stock_impl(
    user_id: str,
    stock_code: str,
    as_of_date: str | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    include_rag: bool = True,
) -> ToolResult:
    lookup = lookup_stock(
        stock_code, user_id=user_id, output_dir=output_dir, db_path=db_path
    )
    code = lookup.get("stock_code") or normalize_stock_code(stock_code)
    ranking_row = dict(lookup.get("ranking_row") or {})
    rec_row = dict(lookup.get("recommendation_row") or {})
    if not code:
        return ToolResult(
            success=False,
            message="Missing or invalid stock code.",
            data={"stock_code": ""},
            errors=["invalid_stock_code"],
            permission=ToolPermission.READ,
            tool_name="stock_analysis",
        )

    ranking_records = load_latest_ranking(output_dir, db_path=db_path)
    recommendation_records = load_latest_recommendations(
        user_id, output_dir, db_path=db_path
    )
    trade_date = (
        str(first_present(rec_row, ["trade_date", "date", "signal_date"], "")).strip()[:10]
        or str(first_present(ranking_row, ["trade_date", "date", "signal_date"], "")).strip()[:10]
        or latest_trade_date(recommendation_records or ranking_records)
    )
    stock_name = str(first_present(rec_row, ["stock_name", "name"], "") or first_present(ranking_row, ["stock_name", "name"], ""))
    current_price = safe_float(first_present(rec_row, ["current_price", "close", "price"], None), 0.0)
    if not is_valid_agent_price(current_price):
        current_price = safe_float(first_present(ranking_row, ["current_price", "close", "price"], None), 0.0)
    original_score = safe_float(first_present(ranking_row, ["score", "pred_score"], 0.0), 0.0)
    news_adjustment = safe_float(first_present(rec_row, ["news_adjustment", "news_adjustment_score"], 0.0), 0.0)
    effective_news_adjustment = safe_float(first_present(rec_row, ["effective_news_adjustment"], news_adjustment), 0.0)
    user_adjustment = safe_float(first_present(rec_row, ["user_adjustment", "user_adjustment_score"], 0.0), 0.0)
    combined_adjustment = safe_float(first_present(rec_row, ["combined_adjustment"], effective_news_adjustment + user_adjustment), 0.0)
    final_action = str(first_present(rec_row, ["final_action", "action"], ""))
    position_adjustment_ratio = safe_float(first_present(rec_row, ["position_adjustment_ratio"], 1.0 + combined_adjustment), 1.0)
    risk_warning = str(first_present(rec_row, ["risk_warning", "warning"], ""))
    if final_action in {"exclude", "risk_alert"} and final_action not in risk_warning:
        risk_warning = "; ".join(filter(None, [risk_warning, final_action]))
    triggered_rules = parse_jsonish(first_present(rec_row, ["triggered_rules"], []), default=[])
    evidence_news_ids = parse_jsonish(first_present(rec_row, ["evidence_news_ids", "evidence_news_id"], []), default=[])
    evidence_chunk_ids = parse_jsonish(first_present(rec_row, ["evidence_chunk_ids", "evidence_chunk_id"], []), default=[])
    rank_value = safe_int(first_present(ranking_row, ["rank", "ranking"], None), None)
    non_topk_warning = ""
    if rank_value is None or int(rank_value) > int(top_k):
        non_topk_warning = f"Stock {code} is outside the selected TopK={top_k}; analysis is for reference only."

    news_result = query_stock_news(code, as_of_date=trade_date, db_path=db_path)
    rag_result = query_stock_rag(code, query=f"{code} risk evidence", top_k=5, output_dir=output_dir) if include_rag else {"chunks": []}
    positive_evidence: list[str] = []
    negative_evidence: list[str] = []
    for event in news_result.get("events") or []:
        title = str(event.get("title") or event.get("headline") or event.get("summary") or "")[:160]
        direction = str((event.get("mapping") or {}).get("impact_direction") or event.get("sentiment") or "").lower()
        if direction in {"positive", "bullish", "good"}:
            positive_evidence.append(title)
        elif direction in {"negative", "bearish", "bad"}:
            negative_evidence.append(title)
    news_summary = "No valid news evidence was found."
    if news_result.get("event_count", 0):
        news_summary = f"Found {news_result.get('event_count')} mapped news events."
    if rag_result.get("chunks"):
        evidence_chunk_ids = list(dict.fromkeys(evidence_chunk_ids + [str(item.get("chunk_id") or "") for item in rag_result["chunks"] if item.get("chunk_id")]))

    current_weight = _position_weight(user_id, code, output_dir, db_path)
    if position_adjustment_ratio <= 0:
        suitability = "not_suitable_for_new_paper_position"
    elif position_adjustment_ratio < 1.0:
        suitability = "suitable_only_with_reduced_weight"
    elif is_valid_agent_price(current_price):
        suitability = "suitable_for_paper_trading_review"
    else:
        suitability = "price_missing_review_only"

    conclusion = (
        f"{code} combined_adjustment={combined_adjustment:.3f}, "
        f"position_adjustment_ratio={position_adjustment_ratio:.3f}. "
        "Use user risk caps and execution constraints before any paper action."
    )
    result = StockAnalysisResult(
        stock_code=code,
        stock_name=stock_name,
        trade_date=trade_date,
        current_price=current_price,
        original_score=original_score,
        original_rank=rank_value,
        model_confidence=first_present(ranking_row, ["confidence", "confidence_score"], ""),
        news_adjustment=news_adjustment,
        effective_news_adjustment=effective_news_adjustment,
        user_adjustment=user_adjustment,
        combined_adjustment=combined_adjustment,
        target_weight=safe_float(first_present(rec_row, ["target_weight", "adjusted_target_weight"], 0.0), 0.0),
        current_weight=current_weight,
        position_adjustment_ratio=position_adjustment_ratio,
        ai_reliability_weight=safe_float(first_present(rec_row, ["ai_reliability_weight"], 0.0), 0.0),
        news_summary=news_summary,
        announcement_summary=str(first_present(rec_row, ["announcement_summary"], "")),
        positive_evidence=positive_evidence,
        negative_evidence=negative_evidence,
        risk_warnings=[item for item in [risk_warning, non_topk_warning] if item],
        triggered_rules=triggered_rules if isinstance(triggered_rules, list) else [str(triggered_rules)],
        evidence_ids=[str(item) for item in evidence_news_ids if item],
        evidence_chunk_ids=[str(item) for item in evidence_chunk_ids if item],
        suitability_for_user=suitability,
        analysis_conclusion=conclusion,
        non_topk_warning=non_topk_warning,
    )
    return ToolResult(
        success=True,
        message=conclusion,
        data=result.to_dict(),
        warnings=result.risk_warnings,
        permission=ToolPermission.READ,
        tool_name="stock_analysis",
    )


def analyze_stock(
    user_id: str,
    stock_code: str,
    as_of_date: str | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    include_rag: bool = True,
) -> ToolResult:
    # Compatibility wrapper. Agent default path is market.analyze_stock via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.market_analysis_service import market_analysis_service

    return market_analysis_service.analyze_stock(
        user_id,
        stock_code,
        as_of_date=as_of_date,
        output_dir=output_dir,
        db_path=db_path,
        top_k=top_k,
        include_rag=include_rag,
        tool_name="stock_analysis",
    )
