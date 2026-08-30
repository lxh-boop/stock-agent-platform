from __future__ import annotations
from pathlib import Path
from typing import Any
import pandas as pd
from config import OUTPUT_DIR
from portfolio.trading_permissions import (
    DEFAULT_TRADING_PERMISSIONS,
    normalize_trading_permissions,
)


def portfolio_output_dir(user_id, output_dir=OUTPUT_DIR):
    return Path(output_dir) / "portfolio" / str(user_id)


def user_output_dir(user_id, output_dir=OUTPUT_DIR):
    return Path(output_dir) / "users" / str(user_id)


from portfolio.paper_account import (
    add_paper_cash_flow,
    cancel_pending_paper_cash_flow,
    list_daily_order_snapshot_dates,
    list_daily_position_snapshot_dates,
    load_daily_order_snapshot,
    load_daily_position_snapshot,
    load_paper_cash_flows,
    load_paper_trading_snapshot,
)
from pipelines.paper_trading_pipeline import run_paper_trading_from_latest
from pipelines.paper_backfill_pipeline import (
    load_paper_backfill_status,
    run_paper_trading_backfill as run_ai_paper_backfill,
)

CLASSIC_AI_COLUMNS = [
    "trade_date",
    "stock_code",
    "stock_name",
    "pred_score",
    "pred_rank",
    "confidence",
    "risk_score",
    "news_adjustment",
    "user_adjustment",
    "effective_news_adjustment",
    "combined_adjustment",
    "original_target_weight",
    "ai_reliability_weight",
    "position_adjustment_ratio",
    "target_weight",
    "ai_adjustment_score",
    "ai_adjustment_effect_status",
    "triggered_rules",
    "evidence_news_ids",
    "evidence_chunk_ids",
    "reason",
    "risk_warning",
]

CLASSIC_DISPLAY_RENAME = {
    "trade_date": "预测交易日",
    "stock_code": "股票代码",
    "stock_name": "股票名称",
    "pred_score": "原始预测分",
    "pred_rank": "原始排名",
    "confidence": "模型置信度",
    "news_adjustment": "新闻调整分",
    "user_adjustment": "用户适配调整",
    "effective_news_adjustment": "有效新闻调整",
    "combined_adjustment": "综合调整",
    "original_target_weight": "原始建议仓位",
    "ai_reliability_weight": "AI可靠度",
    "position_adjustment_ratio": "仓位调整比例",
    "target_weight": "最终目标仓位",
    "ai_adjustment_score": "修正效果评分",
    "ai_adjustment_effect_status": "修正效果状态",
    "triggered_rules": "触发规则",
    "evidence_news_ids": "新闻证据",
    "evidence_chunk_ids": "证据片段",
    "reason": "AI修正原因",
    "risk_warning": "风险提示",
}


def _canonical_ranking(ranking):
    if ranking is None or ranking.empty:
        return ranking
    df = ranking.copy()
    df["stock_code"] = (df.get("stock_code", df.get("code", "")).astype(str).str.split(".").str[0].str.zfill(6))
    df["stock_name"] = df.get("stock_name", df.get("name", ""))
    df["trade_date"] = df.get("trade_date", df.get("date", ""))
    if "date" in df.columns:
        _sig = pd.to_datetime(df["date"], errors="coerce")
        _td = pd.to_datetime(df["trade_date"], errors="coerce")
        _same = _td.notna() & _sig.notna() & (_td == _sig)
        if _same.any():
            df.loc[_same, "trade_date"] = (_sig[_same] + pd.offsets.BDay(1)).dt.strftime("%Y-%m-%d")
    df["pred_rank"] = pd.to_numeric(df.get("pred_rank", df.get("rank", None)), errors="coerce")
    df["pred_score"] = pd.to_numeric(df.get("pred_score", df.get("score", 0.0)), errors="coerce")
    if "risk_score" not in df.columns:
        df["risk_score"] = None
    return df


def _canonical_recommendations(recommendations):
    if recommendations.empty:
        return recommendations
    df = recommendations.copy()
    df["stock_code"] = (df.get("stock_code", df.get("code", "")).astype(str).str.split(".").str[0].str.zfill(6))
    return df


def format_classic_ranking_for_display(df):
    if df.empty:
        return pd.DataFrame(columns=list(CLASSIC_DISPLAY_RENAME.values()))
    display = df.copy()
    cols = [col for col in CLASSIC_DISPLAY_RENAME if col in display.columns]
    return display[cols].rename(columns=CLASSIC_DISPLAY_RENAME)



def load_classic_ranking_with_ai_adjustment(
    output_dir=".",
    ranking_path=None,
    recommendations_path=None,
    sort_by="original_rank",
    db_path=None):
    from agent.services.market_analysis_service import market_analysis_service

    result = market_analysis_service.get_signal_summary(
        output_dir=output_dir,
        ranking_path=ranking_path,
        recommendations_path=recommendations_path,
        sort_by=sort_by,
        include_dataframe=True,
        db_path=db_path,
    )
    frame = (result.get("data") or {}).get("dataframe")
    if isinstance(frame, pd.DataFrame):
        return frame
    return pd.DataFrame(columns=CLASSIC_AI_COLUMNS + ["has_ai_adjustment"])



def build_ai_adjustment_detail(row):
    data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    evidence_news_ids = data.get("evidence_news_ids", "")
    evidence_chunk_ids = data.get("evidence_chunk_ids", "")
    return {
        "original_signal": {"pred_score": data.get("pred_score"), "pred_rank": data.get("pred_rank")},
        "news_adjustment": data.get("news_adjustment", ""),
        "user_adjustment": data.get("user_adjustment", ""),
        "effective_news_adjustment": data.get("effective_news_adjustment", ""),
        "combined_adjustment": data.get("combined_adjustment", ""),
        "original_target_weight": data.get("original_target_weight", ""),
        "ai_reliability_weight": data.get("ai_reliability_weight", ""),
        "position_adjustment_ratio": data.get("position_adjustment_ratio", ""),
        "target_weight": data.get("target_weight", ""),
        "ai_adjustment_score": data.get("ai_adjustment_score", ""),
        "ai_adjustment_effect_status": data.get("ai_adjustment_effect_status", ""),
        "evidence_news_ids": evidence_news_ids or "未检索到相关新闻证据",
        "evidence_chunk_ids": evidence_chunk_ids or "未检索到相关证据片段",
        "reason": data.get("reason", ""),
        "risk_warning": data.get("risk_warning", ""),
    }



def load_current_ai_reliability_state(
    user_id,
    output_dir=".",
    db_path=None,
):
    from evaluation.evaluation_store import load_ai_reliability_state

    return load_ai_reliability_state(user_id, output_dir, db_path)



def load_scheduler_status_summary(root="."):
    try:
        from scheduler.runtime_scheduler import scheduler_public_status

        status = scheduler_public_status(root=root)
        try:
            from scheduler.job_state import load_latest_job_status

            latest_job = load_latest_job_status(root)
        except Exception:
            latest_job = {}
        return {
            "is_available": True,
            "overall_status": status.get("last_status", latest_job.get("overall_status", "unknown")),
            "trade_date": status.get("last_trade_date", latest_job.get("trade_date", "")),
            "finished_at": status.get("last_finished_at", latest_job.get("finished_at", "")),
            "next_run_time": status.get("next_run_time", ""),
            "runtime_running": status.get("runtime_running", False),
            "job_registered": status.get("job_registered", False),
            "latest_signal_date": status.get("latest_signal_date", ""),
            "expected_signal_date": status.get("expected_signal_date", ""),
            "stale": status.get("stale", False),
            "market_stale": status.get("market_stale", False),
            "news_stale": status.get("news_stale", False),
            "public_data_ready": status.get("public_data_ready", False),
            "public_data_healthy": status.get("public_data_healthy", False),
            "ordinary_news_status": status.get("ordinary_news_status", "unknown"),
            "ordinary_news_listing_rows": status.get("ordinary_news_listing_rows", 0),
            "ordinary_news_full_text_written": status.get("ordinary_news_full_text_written", 0),
            "latest_news_publish_time": status.get("latest_news_publish_time", ""),
            "recommendation_count": int(latest_job.get("recommendation_count") or 0),
            "paper_order_count": int(latest_job.get("paper_order_count") or 0),
            "position_count": int(latest_job.get("position_count") or 0),
            "last_error": status.get("last_error", ""),
        }
    except Exception:
        return {"is_available": False, "overall_status": "unknown"}



def run_ai_news_adjustment_from_latest(user_id="default", top_k=50, output_dir=".", db_path=None, paper_trading_enabled=False, dry_run=False):
    from pipelines.schemas import PipelineContext
    context = PipelineContext(user_id=user_id, trade_date="latest", top_k=int(top_k), output_dir=output_dir, db_path=db_path, dry_run=bool(dry_run), paper_trading_enabled=bool(paper_trading_enabled))
    steps = ["prediction", "rag", "scoring"]
    if paper_trading_enabled:
        steps.append("paper")
    steps.append("report")
    return {"context": context, "steps": steps}



def start_scheduler_manual_run(
    user_id=None,
    all_users=True,
    force=False,
    dry_run=False,
    output_dir="outputs",
    db_path=None,
):
    """在 Task Runtime 子进程中同步执行完整更新链路。

    不再启动一个脱离 Task Runtime 的后台子进程，因此任务中心能够准确
    展示成功、失败、超时和最终结果。
    """

    from scheduler.daily_worker import run_scheduled_daily_update

    selected_users = None if all_users else ([str(user_id)] if user_id else None)
    result = run_scheduled_daily_update(
        user_ids=selected_users,
        force=bool(force),
        dry_run=bool(dry_run),
        source="manual",
        output_dir=str(output_dir or "outputs"),
        db_path=db_path,
        root=".",
    )
    return result.to_dict()



def read_scheduler_log_tail(max_chars=12000):
    log_dir = Path("logs") / "scheduler"
    if not log_dir.exists():
        return ""
    files = sorted(log_dir.glob("daily_worker_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return ""
    text = files[0].read_text(encoding="utf-8", errors="ignore")
    return text[-max_chars:] if len(text) > max_chars else text




def has_required_paper_trading_profile(user_context):
    """Check if user profile has enough data for paper trading."""
    if not user_context:
        return False
    try:
        capital = float(user_context.get("available_capital") or user_context.get("initial_capital") or 0.0)
    except Exception:
        capital = 0.0
    required = ["age_range", "income_stability", "risk_level", "investment_horizon"]
    return capital > 0 and all(k in user_context and user_context.get(k) for k in required)


def _parse_percent(value, default=0.0):
    try:
        if isinstance(value, str):
            text = value.strip().replace("%", "")
            return float(text) / 100.0 if "%" in value else float(text)
        return float(value)
    except Exception:
        return default


def _classic_profile_payload(form: dict) -> dict:
    return {
        "user_id": str(form.get("user_id") or "default"),
        "profile_type": form.get("profile_type") or "稳健型",
        "nickname": form.get("nickname", ""),
        "age_range": form.get("age_range", ""),
        "income_level": form.get("income_level", ""),
        "income_stability": form.get("income_stability", ""),
        "available_capital": float(form.get("available_capital") or form.get("initial_capital") or 0.0),
        "investment_experience": form.get("investment_experience", ""),
        "liquidity_need": form.get("liquidity_need", ""),
        "trading_permissions": normalize_trading_permissions(
            form.get("trading_permissions")
        ),
    }


def save_classic_user_context(form, db_path=None, output_dir=".", repository_factory=None):
    """Save user context to the database, then emit an offline file mirror."""
    import json
    from datetime import datetime
    from database.repositories import UserRepository

    data = dict(form or {})
    user_id = str(data.get("user_id") or "default")
    data["user_id"] = user_id
    data["available_capital"] = float(
        data.get("available_capital")
        or data.get("initial_capital")
        or 0.0
    )
    data["trading_permissions"] = normalize_trading_permissions(
        data.get("trading_permissions")
    )

    repository_factory = repository_factory or UserRepository
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    repo = repository_factory(db_path)
    repo.insert_user_profile(_classic_profile_payload(data))
    repo.insert_risk_assessment(
        {
            "assessment_id": f"risk_{user_id}",
            "user_id": user_id,
            "risk_score": _parse_percent(data.get("risk_score"), 0.0),
            "risk_level": data.get("risk_level", ""),
            "max_drawdown_tolerance": _parse_percent(data.get("max_drawdown_tolerance"), 0.0),
            "single_loss_tolerance": _parse_percent(data.get("single_loss_tolerance"), 0.0),
            "volatility_tolerance": data.get("volatility_tolerance", ""),
            "investment_horizon": data.get("investment_horizon", ""),
            "questionnaire_version": "classic_v1",
            "assessment_time": now,
            "is_valid": 1,
        }
    )
    repo.insert_investment_goal(
        {
            "goal_id": f"goal_{user_id}",
            "user_id": user_id,
            "goal_type": data.get("goal_type", ""),
            "target_return": _parse_percent(data.get("target_return"), 0.0),
            "target_period": data.get("target_period", ""),
            "priority": data.get("priority", ""),
            "capital_usage": data.get("capital_usage", ""),
        }
    )
    repo.insert_trading_behavior(
        {
            "behavior_id": f"behavior_{user_id}",
            "user_id": user_id,
            "avg_holding_days": float(data.get("avg_holding_days") or 0.0),
            "turnover_rate": float(data.get("turnover_rate") or 0.0),
            "avg_position_size": float(data.get("avg_position_size") or 0.0),
            "preferred_industries": json.dumps(data.get("preferred_industries") or [], ensure_ascii=False),
            "avoided_industries": json.dumps(data.get("avoided_industries") or [], ensure_ascii=False),
            "stop_loss_behavior": data.get("stop_loss_behavior", ""),
            "max_historical_loss": _parse_percent(data.get("max_historical_loss"), 0.0),
            "holding_period_preference": data.get("holding_period_preference", ""),
            "allow_high_volatility": int(bool(data.get("allow_high_volatility", False))),
            "trading_style": data.get("trading_style", ""),
        }
    )

    root = user_output_dir(user_id, output_dir)
    root.mkdir(parents=True, exist_ok=True)
    mirror_path = root / "user_profile.json"
    mirror_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "database", "path": str(mirror_path)}


def load_classic_user_context(
    user_id,
    db_path=None,
    output_dir=".",
    repository_factory=None,
):
    """Load user context and merge file-backed trading permissions."""

    import json
    from database.repositories import UserRepository

    user_id = str(user_id or "default")
    fallback_data: dict[str, Any] = {}
    fallback_path = (
        user_output_dir(user_id, output_dir)
        / "user_profile.json"
    )
    if fallback_path.exists():
        try:
            loaded = json.loads(
                fallback_path.read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(loaded, dict):
                fallback_data = loaded
        except Exception:
            fallback_data = {}

    repository_factory = (
        repository_factory or UserRepository
    )
    database_data: dict[str, Any] = {}

    try:
        repo = repository_factory(db_path)
        profile = repo.get_user_profile(user_id)
        if profile:
            database_data = dict(profile)
            risks = repo.list_risk_assessments(user_id)
            goals = repo.list_investment_goals(user_id)
            behaviors = repo.list_trading_behaviors(user_id)
            if risks:
                database_data.update(risks[-1])
            if goals:
                database_data.update(goals[-1])
            if behaviors:
                database_data.update(behaviors[-1])
    except Exception:
        database_data = {}

    if database_data:
        data = dict(fallback_data)
        data.update(database_data)
    else:
        data = dict(fallback_data)

    if not data:
        legacy_path = (
            Path(output_dir)
            / "portfolio"
            / user_id
            / "user_context.json"
        )
        if legacy_path.exists():
            try:
                loaded = json.loads(
                    legacy_path.read_text(
                        encoding="utf-8"
                    )
                )
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}

    data["user_id"] = user_id
    data["trading_permissions"] = (
        normalize_trading_permissions(
            fallback_data.get(
                "trading_permissions",
                data.get("trading_permissions"),
            )
        )
    )
    return data


def cancel_pending_paper_cash_flow(*args, **kwargs):
    """Cancel a pending paper-trading cash flow."""
    from portfolio.paper_account import cancel_pending_paper_cash_flow as _cancel_pending_paper_cash_flow

    return _cancel_pending_paper_cash_flow(*args, **kwargs)


def get_classic_user_profile_form_options():
    return {
        "age_range": ["18-25", "26-35", "36-45", "46-60", "60以上"],
        "income_stability": ["不稳定", "一般", "较稳定", "稳定"],
        "investment_experience": ["无经验", "1年以内", "1-3年", "3年以上"],
        "liquidity_need": ["高", "中", "低"],
        "risk_level": ["C1 保守型", "C2 稳健偏保守", "C3 稳健型", "C4 积极型", "C5 激进型"],
        "max_drawdown_tolerance": ["5%", "10%", "15%", "20%", "30%以上"],
        "single_loss_tolerance": ["3%", "5%", "8%", "10%以上"],
        "volatility_tolerance": ["低", "中", "高"],
        "investment_horizon": ["1个月以内", "1-3个月", "3-6个月", "6-12个月", "1年以上"],
        "goal_type": ["现金管理", "稳健增值", "短期机会", "长期成长", "模拟学习"],
        "target_return": ["3%", "5%", "8%", "10%", "15%以上"],
        "target_period": ["1个月以内", "1-3个月", "3-6个月", "6-12个月", "1年以上"],
        "priority": ["风险优先", "均衡", "收益优先"],
        "capital_usage": ["闲置资金", "模拟学习资金", "短期备用资金"],
        "preferred_industries": ["新能源", "消费", "医药", "科技", "金融", "制造"],
        "avoided_industries": ["ST股票", "高杠杆行业", "高波动题材", "退市风险"],
        "trading_style": ["保守", "稳健", "积极", "激进"],
    }
