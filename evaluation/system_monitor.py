from __future__ import annotations

import hashlib
import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd

from config import (
    BACKTEST_METRICS_PATH,
    BACKTEST_NAV_PATH,
    BACKTEST_TRADES_PATH,
    LATEST_RAW_DATA_PATH,
    OUTPUT_DIR,
    RANKING_LATEST_PATH,
)
from factor_provider import latest_alpha158
from database.connection import get_connection
from database.repositories import SystemMonitorRepository
from database.schemas import json_loads
from agent.runtime_reliability import collect_runtime_health_summary
from portfolio.storage import PortfolioStorage
from rag.dense_retriever import DEFAULT_DENSE_MODEL, DENSE_INDEX_SCHEMA_VERSION


DEFAULT_THRESHOLDS_PATH = Path("configs/system_monitor_thresholds.json")
STATUS_ORDER = {"normal": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class MonitorCollectionResult:
    snapshot: dict[str, Any]
    alerts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"snapshot": self.snapshot, "alerts": self.alerts}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _date_text(value: Any) -> str:
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return ""
    return dt.strftime("%Y-%m-%d")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in [None, ""]:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _read_csv(path: str | Path) -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists() or file_path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(file_path, dtype={"code": str, "stock_code": str}, encoding="utf-8-sig")


def _file_version(path: str | Path) -> str:
    file_path = Path(path)
    if not file_path.exists():
        return "missing"
    stat = file_path.stat()
    source = f"{file_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _table_exists(db_path: str | Path | None, table: str) -> bool:
    del db_path
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=%s",
            (table,),
        ).fetchone()
    return row is not None


def _query_all(db_path: str | Path | None, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    del db_path
    with get_connection() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _query_one(db_path: str | Path, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    rows = _query_all(db_path, sql, params)
    return rows[0] if rows else None


def _count_table(db_path: str | Path, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not _table_exists(db_path, table):
        return 0
    suffix = f" WHERE {where}" if where else ""
    row = _query_one(db_path, f"SELECT COUNT(*) AS n FROM {table}{suffix}", params)
    return int((row or {}).get("n") or 0)


def _json_list(value: Any) -> list[Any]:
    parsed = json_loads(value, default=[])
    return parsed if isinstance(parsed, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = json_loads(value, default={})
    return parsed if isinstance(parsed, dict) else {}


def _ratio(numerator: float, denominator: float) -> float | None:
    return float(numerator) / float(denominator) if denominator else None


def _psi(expected: pd.Series, actual: pd.Series, bins: int = 10) -> float | None:
    expected = pd.to_numeric(expected, errors="coerce").dropna()
    actual = pd.to_numeric(actual, errors="coerce").dropna()
    if expected.empty or actual.empty:
        return None
    combined = pd.concat([expected, actual], ignore_index=True)
    if combined.nunique(dropna=True) < 2:
        return 0.0
    try:
        edges = pd.qcut(combined, q=min(bins, combined.nunique()), duplicates="drop", retbins=True)[1]
    except Exception:
        return None
    if len(edges) < 3:
        return 0.0
    expected_counts = pd.cut(expected, bins=edges, include_lowest=True).value_counts(sort=False)
    actual_counts = pd.cut(actual, bins=edges, include_lowest=True).value_counts(sort=False)
    expected_ratio = (expected_counts / max(1, len(expected))).replace(0, 1e-6)
    actual_ratio = (actual_counts / max(1, len(actual))).replace(0, 1e-6)
    value = ((actual_ratio - expected_ratio) * (actual_ratio / expected_ratio).map(math.log)).sum()
    return float(value)


def _feature_psi(feature_df: pd.DataFrame) -> float | None:
    if feature_df.empty:
        return None
    date_col = "date" if "date" in feature_df.columns else "trade_date" if "trade_date" in feature_df.columns else ""
    if not date_col:
        return None
    dates = pd.to_datetime(feature_df[date_col], errors="coerce")
    valid_dates = sorted(dates.dropna().unique())
    if len(valid_dates) < 2:
        return None
    prev_date, latest_date = valid_dates[-2], valid_dates[-1]
    prev = feature_df.loc[dates == prev_date]
    latest = feature_df.loc[dates == latest_date]
    numeric_cols = [
        col
        for col in feature_df.select_dtypes(include=["number"]).columns
        if col not in {"label", "future_return", "future_5d_ret"}
    ][:20]
    values = [_psi(prev[col], latest[col]) for col in numeric_cols]
    values = [value for value in values if value is not None]
    return float(mean(values)) if values else None


def load_monitor_thresholds(path: str | Path = DEFAULT_THRESHOLDS_PATH) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {"rules": []}
    return json.loads(config_path.read_text(encoding="utf-8"))


def collect_data_metrics(db_path: str | Path, output_dir: str | Path = OUTPUT_DIR) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    ranking = _read_csv(Path(output_dir) / "ranking_latest.csv")
    raw = _read_csv(LATEST_RAW_DATA_PATH)
    try:
        features = latest_alpha158()
    except Exception:
        features = pd.DataFrame()
    stock_count = int(ranking["code"].nunique()) if "code" in ranking.columns and not ranking.empty else 0
    raw_stock_count = int(raw["code"].nunique()) if "code" in raw.columns and not raw.empty else stock_count
    universe_count = max(300, stock_count, raw_stock_count)
    if ranking.empty:
        missing.append("ranking_latest.csv")
    if features.empty:
        missing.append("latest_feature_data")
    feature_values = features.select_dtypes(include=["number"]) if not features.empty else pd.DataFrame()
    feature_nan_ratio = float(feature_values.isna().sum().sum() / feature_values.size) if feature_values.size else None
    feature_missing_ratio = float(features.isna().any(axis=1).mean()) if not features.empty else None
    news_count = _count_table(db_path, "news_event")
    full_text_count = _count_table(db_path, "news_event", "content_level='full_text'") if news_count else 0
    return {
        "stock_coverage": _ratio(max(stock_count, raw_stock_count), universe_count),
        "ranking_stock_count": stock_count,
        "raw_stock_count": raw_stock_count,
        "feature_nan_ratio": feature_nan_ratio,
        "feature_missing_ratio": feature_missing_ratio,
        "news_count": news_count,
        "full_text_ratio": _ratio(full_text_count, news_count),
        "data_source_failure_rate": 1.0 if not stock_count and not raw_stock_count else 0.0,
        "feature_psi": _feature_psi(features),
    }, missing


def _topk_stability(frame: pd.DataFrame, date_col: str, code_col: str, k: int = 10) -> float | None:
    dates = sorted(pd.to_datetime(frame[date_col], errors="coerce").dropna().unique())
    if len(dates) < 2:
        return None
    latest, prev = dates[-1], dates[-2]
    latest_codes = set(frame.loc[pd.to_datetime(frame[date_col], errors="coerce") == latest, code_col].astype(str).str.zfill(6).head(k))
    prev_codes = set(frame.loc[pd.to_datetime(frame[date_col], errors="coerce") == prev, code_col].astype(str).str.zfill(6).head(k))
    union = latest_codes | prev_codes
    return len(latest_codes & prev_codes) / len(union) if union else None


def collect_model_metrics(output_dir: str | Path = OUTPUT_DIR) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    ranking = _read_csv(Path(output_dir) / "ranking_latest.csv")
    backtest_trades = _read_csv(BACKTEST_TRADES_PATH)
    metrics: dict[str, Any] = {}
    try:
        metrics_path = Path(BACKTEST_METRICS_PATH)
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:
        missing.append(f"backtest_metrics:{type(exc).__name__}")
    score_col = "pred_score" if "pred_score" in ranking.columns else "score" if "score" in ranking.columns else ""
    scores = pd.to_numeric(ranking[score_col], errors="coerce") if score_col else pd.Series(dtype=float)
    model_names = ranking["model_name"].dropna().astype(str).unique().tolist() if "model_name" in ranking.columns and not ranking.empty else []
    date_col = "date" if "date" in backtest_trades.columns else "trade_date" if "trade_date" in backtest_trades.columns else ""
    code_col = "code" if "code" in backtest_trades.columns else "stock_code" if "stock_code" in backtest_trades.columns else ""
    industry_concentration = None
    if "industry" in ranking.columns and not ranking.empty:
        industry_concentration = float(ranking.head(10)["industry"].value_counts(normalize=True).max())
    if ranking.empty:
        missing.append("ranking_latest.csv")
    return {
        "rolling_ic": _safe_float(metrics.get("ic") or metrics.get("rolling_ic")),
        "rolling_rank_ic": _safe_float(metrics.get("rank_ic") or metrics.get("rolling_rank_ic")),
        "rolling_icir": _safe_float(metrics.get("icir") or metrics.get("rolling_icir")),
        "prediction_mean": _safe_float(scores.mean()) if not scores.empty else None,
        "prediction_std": _safe_float(scores.std()) if not scores.empty else None,
        "prediction_nan_ratio": float(scores.isna().mean()) if len(scores) else None,
        "topk_stability": _topk_stability(backtest_trades, date_col, code_col) if date_col and code_col and not backtest_trades.empty else None,
        "topk_industry_concentration": industry_concentration,
        "model_version_diff": "single" if len(model_names) <= 1 else "mixed",
        "model_names": model_names,
    }, missing


def _index_file_version(paths: list[Path]) -> str:
    if not paths:
        return "missing"
    source = "|".join(
        f"{path.resolve()}:{path.stat().st_mtime_ns}:{path.stat().st_size}"
        for path in sorted(paths, key=lambda item: str(item))
    )
    return hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]


def _read_dense_index_status(paths: list[Path]) -> tuple[dict[str, Any], str]:
    dense_path = next((path for path in paths if "dense" in path.name.lower()), None)
    if not dense_path:
        return {
            "dense_available": False,
            "embedding_model_name": DEFAULT_DENSE_MODEL,
            "embedding_dimension": 0,
            "dense_index_schema_version": DENSE_INDEX_SCHEMA_VERSION,
            "dense_load_error": "dense index file is missing",
        }, ""
    try:
        with dense_path.open("rb") as file:
            payload = pickle.load(file)
    except Exception as exc:
        return {
            "dense_available": False,
            "embedding_model_name": DEFAULT_DENSE_MODEL,
            "embedding_dimension": 0,
            "dense_index_schema_version": DENSE_INDEX_SCHEMA_VERSION,
            "dense_load_error": f"{type(exc).__name__}: {exc}",
        }, ""
    return {
        "dense_available": bool(payload.get("available")),
        "embedding_model_name": payload.get("embedding_model_name") or payload.get("model_name") or DEFAULT_DENSE_MODEL,
        "embedding_dimension": int(payload.get("embedding_dimension") or 0),
        "dense_index_schema_version": int(payload.get("schema_version") or DENSE_INDEX_SCHEMA_VERSION),
        "dense_load_error": str(payload.get("load_error") or ""),
    }, str(payload.get("index_version") or "")


def collect_rag_metrics(db_path: str | Path, output_dir: str | Path = OUTPUT_DIR) -> tuple[dict[str, Any], list[str], str]:
    missing: list[str] = []
    output_root = Path(output_dir)
    inferred_data_root = output_root.parent / "data"
    retrieval_rows = _query_all(db_path, "SELECT * FROM rag_retrieval_log ORDER BY created_at") if _table_exists(db_path, "rag_retrieval_log") else []
    query_count = len(retrieval_rows)
    bm25_hits = dense_hits = hybrid_hits = empty = duplicate = 0
    for row in retrieval_rows:
        bm25 = _json_list(row.get("bm25_results"))
        dense = _json_list(row.get("dense_results"))
        selected = _json_list(row.get("selected_chunk_ids"))
        returned = _json_list(row.get("returned_chunk_ids"))
        chosen = selected or returned
        bm25_hits += int(bool(bm25))
        dense_hits += int(bool(dense))
        hybrid_hits += int(bool(chosen))
        empty += int(not chosen)
        duplicate += int(len(chosen) != len(set(map(str, chosen)))) if chosen else 0
    chunk_count = _count_table(db_path, "news_chunk")
    traced_chunks = _count_table(db_path, "news_chunk", "source<>'' AND publish_time<>'' AND stock_code<>''") if chunk_count else 0
    full_text_chunks = _count_table(db_path, "news_chunk", "content_level='full_text'") if chunk_count else 0
    index_candidates = [
        output_root / "rag_indexes" / "news_dense.pkl",
        output_root / "rag_indexes" / "news_bm25.pkl",
        inferred_data_root / "rag_tfidf_index.pkl",
    ]
    existing_indexes = [path for path in index_candidates if path.exists()]
    dense_status, dense_index_version = _read_dense_index_status(existing_indexes)
    index_age = None
    if existing_indexes:
        newest = max(path.stat().st_mtime for path in existing_indexes)
        index_age = max(0.0, datetime.now().timestamp() - newest)
    else:
        missing.append("rag_index")
    return {
        "rag_query_count": query_count,
        "rag_empty_rate": _ratio(empty, query_count),
        "bm25_hit_rate": _ratio(bm25_hits, query_count),
        "dense_hit_rate": _ratio(dense_hits, query_count),
        "hybrid_hit_rate": _ratio(hybrid_hits, query_count),
        "future_news_filtered_count": 0,
        "wrong_stock_filter_count": 0,
        "full_text_evidence_ratio": _ratio(full_text_chunks, chunk_count),
        "duplicate_result_rate": _ratio(duplicate, query_count),
        "source_trace_rate": _ratio(traced_chunks, chunk_count),
        "index_age": index_age,
        **dense_status,
    }, missing, dense_index_version or _index_file_version(existing_indexes)


def _parse_dt(value: Any) -> datetime | None:
    dt = pd.to_datetime(value, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.to_pydatetime()


def _duration_seconds(start: Any, end: Any) -> float | None:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def collect_agent_metrics(db_path: str | Path) -> tuple[dict[str, Any], list[str], str]:
    missing: list[str] = []
    if not _table_exists(db_path, "agent_runs"):
        return {"agent_run_count": 0, "runtime_health": collect_runtime_health_summary(db_path)}, ["agent_runs"], ""
    runs = _query_all(db_path, "SELECT * FROM agent_runs ORDER BY created_at")
    tool_calls = _query_all(db_path, "SELECT * FROM agent_tool_calls ORDER BY started_at") if _table_exists(db_path, "agent_tool_calls") else []
    sources = _query_all(db_path, "SELECT * FROM agent_sources ORDER BY retrieved_at") if _table_exists(db_path, "agent_sources") else []
    steps = _query_all(db_path, "SELECT * FROM agent_steps ORDER BY started_at") if _table_exists(db_path, "agent_steps") else []
    run_count = len(runs)
    status_values = [str(row.get("status") or "").lower() for row in runs]
    success_count = sum(1 for status in status_values if status in {"completed", "success", "succeeded"})
    partial_count = sum(1 for status in status_values if "partial" in status)
    failed_count = sum(1 for status in status_values if status in {"failed", "error"} or "fail" in status)
    durations = [_duration_seconds(row.get("started_at"), row.get("finished_at")) for row in runs]
    durations = [value for value in durations if value is not None]
    step_counts: dict[str, int] = {}
    for step in steps:
        run_id = str(step.get("run_id") or "")
        step_counts[run_id] = step_counts.get(run_id, 0) + 1
    replan_count = 0
    replan_success = 0
    for row in runs:
        metadata = _json_dict(row.get("metadata_json"))
        count = int(metadata.get("replan_count") or 0)
        replan_count += int(count > 0)
        replan_success += int(count > 0 and str(row.get("status") or "").lower() in {"completed", "success", "succeeded"})
    tool_failure_count = sum(1 for row in tool_calls if str(row.get("status") or "").lower() not in {"completed", "success", "succeeded", "ok"})
    parameter_missing = sum(1 for row in tool_calls if "missing" in str(row.get("error_message") or "").lower() or "parameter" in str(row.get("error_message") or "").lower())
    latest_run_id = str(runs[-1].get("run_id") or "") if runs else ""
    runtime_health = collect_runtime_health_summary(db_path)
    circuit_states = runtime_health.get("circuit_states") if isinstance(runtime_health, dict) else {}
    circuit_open_count = int((circuit_states or {}).get("open") or 0)
    return {
        "agent_run_count": run_count,
        "success_rate": _ratio(success_count, run_count),
        "partial_success_rate": _ratio(partial_count, run_count),
        "failed_rate": _ratio(failed_count, run_count),
        "tool_failure_rate": _ratio(tool_failure_count, len(tool_calls)),
        "parameter_missing_rate": _ratio(parameter_missing, len(tool_calls)),
        "replan_rate": _ratio(replan_count, run_count),
        "replan_success_rate": _ratio(replan_success, replan_count),
        "average_steps": float(mean(step_counts.values())) if step_counts else None,
        "average_latency": float(mean(durations)) if durations else None,
        "p95_latency": float(pd.Series(durations).quantile(0.95)) if durations else None,
        "source_trace_rate": _ratio(len({row.get("run_id") for row in sources if row.get("run_id")}), run_count),
        "unanswered_rate": _ratio(failed_count, run_count),
        "runtime_success_rate": runtime_health.get("success_rate"),
        "runtime_p50_latency": runtime_health.get("p50_latency"),
        "runtime_p95_latency": runtime_health.get("p95_latency"),
        "runtime_tool_failure_rate": runtime_health.get("tool_failure_rate"),
        "runtime_retry_count": runtime_health.get("retry_count"),
        "runtime_timeout_count": runtime_health.get("timeout_count"),
        "runtime_circuit_open_count": circuit_open_count,
        "runtime_over_budget_count": runtime_health.get("over_budget_count"),
        "runtime_resumable_run_count": runtime_health.get("resumable_run_count"),
        "runtime_health": runtime_health,
    }, missing, latest_run_id


def _load_current_position_rows(
    db_path: str | Path,
    user_id: str,
    output_dir: str | Path,
) -> tuple[list[dict[str, Any]], str]:
    storage_dir = Path(output_dir) / "portfolio" / user_id
    storage = PortfolioStorage(db_path=db_path, output_dir=storage_dir)
    local_positions_exist = storage.positions_latest_path.exists() or storage.positions_path.exists()
    if local_positions_exist:
        rows = []
        for position in storage.load_positions(None):
            rows.append(
                {
                    "stock_code": str(getattr(position, "stock_code", "") or ""),
                    "stock_name": str(getattr(position, "stock_name", "") or ""),
                    "market_value": getattr(position, "market_value", 0.0),
                    "position_ratio": getattr(position, "position_ratio", 0.0),
                    "industry": str(getattr(position, "industry", "") or ""),
                    "updated_at": str(getattr(position, "updated_at", "") or ""),
                }
            )
        return rows, "latest_position_file"
    rows = (
        _query_all(db_path, "SELECT * FROM portfolio_position WHERE user_id=%s ORDER BY updated_at", (user_id,))
        if _table_exists(db_path, "portfolio_position")
        else []
    )
    return rows, "portfolio_position_table"


def collect_portfolio_metrics(
    db_path: str | Path,
    user_id: str,
    output_dir: str | Path = OUTPUT_DIR,
) -> tuple[dict[str, Any], list[str], str]:
    missing: list[str] = []
    nav_rows = _query_all(db_path, "SELECT * FROM paper_nav_history WHERE user_id=%s ORDER BY trade_date", (user_id,)) if _table_exists(db_path, "paper_nav_history") else []
    positions, position_source = _load_current_position_rows(db_path, user_id, output_dir)
    orders = _query_all(db_path, "SELECT * FROM paper_order WHERE user_id=%s ORDER BY created_at", (user_id,)) if _table_exists(db_path, "paper_order") else []
    decisions = _query_all(db_path, "SELECT * FROM agent_decision_log WHERE user_id=%s ORDER BY created_at", (user_id,)) if _table_exists(db_path, "agent_decision_log") else []
    latest_snapshot = _query_one(db_path, "SELECT * FROM paper_account_snapshot WHERE user_id=%s ORDER BY trade_date DESC, created_at DESC LIMIT 1", (user_id,)) if _table_exists(db_path, "paper_account_snapshot") else None
    if not nav_rows:
        missing.append("paper_nav_history")
    if not positions:
        missing.append("paper_positions")
    first_nav = nav_rows[0] if nav_rows else {}
    last_nav = nav_rows[-1] if nav_rows else {}
    snapshot_id = str((latest_snapshot or {}).get("snapshot_id") or last_nav.get("nav_id") or "")
    portfolio_return = _safe_float(last_nav.get("time_weighted_return") or last_nav.get("cumulative_return"))
    benchmark_return = None
    try:
        if Path(BACKTEST_METRICS_PATH).exists():
            metrics = json.loads(Path(BACKTEST_METRICS_PATH).read_text(encoding="utf-8"))
            benchmark_return = _safe_float(metrics.get("benchmark_return"))
            information_ratio = _safe_float(metrics.get("information_ratio"))
        else:
            information_ratio = None
    except Exception:
        information_ratio = None
    total_assets = _safe_float(last_nav.get("total_assets"), 0.0) or 0.0
    position_values = [_safe_float(row.get("market_value"), 0.0) or 0.0 for row in positions]
    single_concentration = max(position_values) / total_assets if total_assets and position_values else None
    industry_values: dict[str, float] = {}
    for row in positions:
        industry = str(row.get("industry") or "unknown")
        if industry == "unknown":
            continue
        industry_values[industry] = industry_values.get(industry, 0.0) + (_safe_float(row.get("market_value"), 0.0) or 0.0)
    turnover = None
    gross_amount = sum(abs(_safe_float(row.get("gross_amount"), 0.0) or 0.0) for row in orders)
    avg_assets = mean([_safe_float(row.get("total_assets"), 0.0) or 0.0 for row in nav_rows]) if nav_rows else 0.0
    if avg_assets:
        turnover = gross_amount / avg_assets
    news_values = [_safe_float(row.get("news_adjustment")) for row in decisions]
    user_values = [_safe_float(_json_dict(row.get("user_constraint")).get("position_adjustment_ratio")) for row in decisions]
    return {
        "portfolio_return": portfolio_return,
        "benchmark_return": benchmark_return,
        "excess_return": (portfolio_return - benchmark_return) if portfolio_return is not None and benchmark_return is not None else None,
        "information_ratio": information_ratio,
        "max_drawdown": min([_safe_float(row.get("drawdown"), 0.0) or 0.0 for row in nav_rows], default=None),
        "turnover": turnover,
        "transaction_cost": sum(_safe_float(row.get("total_fee"), 0.0) or 0.0 for row in orders),
        "cash_ratio": (_safe_float(last_nav.get("cash"), 0.0) or 0.0) / total_assets if total_assets else None,
        "single_stock_concentration": single_concentration,
        "industry_concentration": max(industry_values.values()) / total_assets if total_assets and industry_values else None,
        "news_adjustment_contribution": float(mean([value for value in news_values if value is not None])) if any(value is not None for value in news_values) else None,
        "user_adjustment_contribution": float(mean([value for value in user_values if value is not None])) if any(value is not None for value in user_values) else None,
        "first_total_assets": _safe_float(first_nav.get("total_assets")),
        "latest_total_assets": total_assets or None,
        "position_source": position_source,
    }, missing, snapshot_id


def _metric_value(snapshot: dict[str, Any], layer: str, metric: str) -> float | None:
    group = snapshot.get(f"{layer}_metrics") or {}
    return _safe_float(group.get(metric))


def evaluate_monitor_alerts(snapshot: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for rule in thresholds.get("rules") or []:
        layer = str(rule.get("layer") or "")
        metric = str(rule.get("metric") or "")
        operator = str(rule.get("operator") or "")
        value = _metric_value(snapshot, layer, metric)
        if value is None:
            continue
        severity = "normal"
        threshold_value = None
        if operator == "min":
            critical = _safe_float(rule.get("critical"))
            warning = _safe_float(rule.get("warning"))
            if critical is not None and value < critical:
                severity, threshold_value = "critical", critical
            elif warning is not None and value < warning:
                severity, threshold_value = "warning", warning
        elif operator == "max":
            critical = _safe_float(rule.get("critical"))
            warning = _safe_float(rule.get("warning"))
            if critical is not None and value > critical:
                severity, threshold_value = "critical", critical
            elif warning is not None and value > warning:
                severity, threshold_value = "warning", warning
        if severity == "normal":
            continue
        alert_id = hashlib.sha1(f"{snapshot['snapshot_id']}|{rule.get('rule_id')}|{severity}".encode("utf-8")).hexdigest()[:20]
        alerts.append(
            {
                "alert_id": f"monitor_alert_{alert_id}",
                "snapshot_id": snapshot["snapshot_id"],
                "trade_date": snapshot["trade_date"],
                "user_id": snapshot["user_id"],
                "layer": layer,
                "metric_name": metric,
                "severity": severity,
                "status": "active",
                "metric_value": value,
                "threshold_value": threshold_value,
                "message": str(rule.get("message") or f"{layer}.{metric} threshold breached"),
                "updated_at": _now_text(),
            }
        )
    return alerts


def _overall_status(alerts: list[dict[str, Any]]) -> str:
    status = "normal"
    for alert in alerts:
        severity = str(alert.get("severity") or "normal")
        if STATUS_ORDER.get(severity, 0) > STATUS_ORDER[status]:
            status = severity
    return status


def _resolve_trade_date(explicit: str | None, ranking: pd.DataFrame) -> str:
    if explicit:
        return _date_text(explicit) or str(explicit)
    if not ranking.empty:
        for column in ["prediction_date", "trade_date", "date"]:
            if column in ranking.columns:
                value = _date_text(ranking[column].iloc[0])
                if value:
                    return value
    return datetime.now().strftime("%Y-%m-%d")


def build_system_monitor_snapshot(
    *,
    db_path: str | Path = None,
    user_id: str = "default",
    trade_date: str | None = None,
    output_dir: str | Path = OUTPUT_DIR,
    thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
) -> MonitorCollectionResult:
    db = None
    output_root = Path(output_dir)
    ranking = _read_csv(output_root / "ranking_latest.csv")
    resolved_trade_date = _resolve_trade_date(trade_date, ranking)
    missing: list[str] = []
    data_metrics, data_missing = collect_data_metrics(db, output_root)
    model_metrics, model_missing = collect_model_metrics(output_root)
    rag_metrics, rag_missing, rag_index_version = collect_rag_metrics(db, output_root)
    agent_metrics, agent_missing, run_id = collect_agent_metrics(db)
    portfolio_metrics, portfolio_missing, portfolio_snapshot_id = collect_portfolio_metrics(db, user_id, output_root)
    missing.extend(f"data:{item}" for item in data_missing)
    missing.extend(f"model:{item}" for item in model_missing)
    missing.extend(f"rag:{item}" for item in rag_missing)
    missing.extend(f"agent:{item}" for item in agent_missing)
    missing.extend(f"portfolio:{item}" for item in portfolio_missing)
    model_version = ",".join(model_metrics.get("model_names") or []) or "unknown"
    data_version = _file_version(output_root / "ranking_latest.csv")
    snapshot_id = f"system_monitor_{user_id}_{resolved_trade_date.replace('-', '')}"
    now = _now_text()
    snapshot = {
        "snapshot_id": snapshot_id,
        "trade_date": resolved_trade_date,
        "user_id": user_id,
        "data_version": data_version,
        "model_version": model_version,
        "rag_index_version": rag_index_version,
        "run_id": run_id,
        "portfolio_snapshot_id": portfolio_snapshot_id,
        "overall_status": "normal",
        "data_metrics": data_metrics,
        "model_metrics": model_metrics,
        "rag_metrics": rag_metrics,
        "agent_metrics": agent_metrics,
        "portfolio_metrics": portfolio_metrics,
        "version_info": {
            "data_version": data_version,
            "model_version": model_version,
            "rag_index_version": rag_index_version,
            "run_id": run_id,
            "portfolio_snapshot_id": portfolio_snapshot_id,
            "thresholds_path": str(thresholds_path),
        },
        "missing_modules": list(dict.fromkeys(missing)),
        "updated_at": now,
    }
    thresholds = load_monitor_thresholds(thresholds_path)
    alerts = evaluate_monitor_alerts(snapshot, thresholds)
    snapshot["overall_status"] = _overall_status(alerts)
    return MonitorCollectionResult(snapshot=snapshot, alerts=alerts)


def collect_and_store_system_monitor_snapshot(
    *,
    db_path: str | Path = None,
    user_id: str = "default",
    trade_date: str | None = None,
    output_dir: str | Path = OUTPUT_DIR,
    thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
) -> MonitorCollectionResult:
    result = build_system_monitor_snapshot(
        db_path=db_path,
        user_id=user_id,
        trade_date=trade_date,
        output_dir=output_dir,
        thresholds_path=thresholds_path,
    )
    repo = SystemMonitorRepository(db_path)
    repo.upsert_snapshot(result.snapshot)
    for alert in result.alerts:
        repo.upsert_alert(alert)
    stored = repo.get_snapshot(result.snapshot["snapshot_id"]) or result.snapshot
    return MonitorCollectionResult(snapshot=stored, alerts=repo.list_alerts(snapshot_id=result.snapshot["snapshot_id"]))


def list_system_monitor_history(
    db_path: str | Path = None,
    user_id: str = "default",
    limit: int = 30,
) -> list[dict[str, Any]]:
    return list(reversed(SystemMonitorRepository(db_path).list_snapshots(user_id=user_id, limit=limit)))


def list_system_monitor_alerts(
    db_path: str | Path = None,
    snapshot_id: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    return SystemMonitorRepository(db_path).list_alerts(snapshot_id=snapshot_id, user_id=user_id, limit=limit)
