from __future__ import annotations

from core.user_scope import owned_path

import json
import time
from pathlib import Path

import pandas as pd

from application.support.file_loader import safe_read_csv, safe_read_json
from core.config.paths import (
    BACKTEST_MASTER_TABLE_PATH,
    MODEL_CANDIDATES_PATH,
    MODEL_DISCOVERY_REPORT_PATH,
    MODEL_SEARCH_ERRORS_PATH,
    MODEL_SEARCH_RESULTS_PATH,
    SELECTED_STRATEGY_PATH,
    project_path,
)


BACKTEST_DISCLAIMER = "回测结果仅代表历史数据上的模型表现，不代表未来收益，不构成投资建议。"


def load_table_file(path_text: str | Path) -> pd.DataFrame:
    result = safe_read_csv(path_text, dtype={"code": str})
    return result.data if result.ok else pd.DataFrame()


def load_selected_strategy() -> dict:
    result = safe_read_json(SELECTED_STRATEGY_PATH)
    return result.data if result.ok and isinstance(result.data, dict) else {}


def save_selected_strategy(strategy: dict) -> None:
    path = owned_path(SELECTED_STRATEGY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(strategy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def resolve_output_path(value) -> Path | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return owned_path(project_path(text))


def load_daily_returns_for_strategy(strategy_or_row: dict | pd.Series) -> pd.DataFrame:
    value = strategy_or_row.get("daily_returns_csv") if hasattr(strategy_or_row, "get") else None
    path = resolve_output_path(value)
    if path is None or not path.exists():
        return pd.DataFrame()
    result = safe_read_csv(path, parse_dates=["date"])
    return result.data if result.ok else pd.DataFrame()


def make_strategy_from_row(row: pd.Series) -> dict:
    def _value(key, default=""):
        value = row.get(key, default)
        if pd.isna(value):
            return default
        if isinstance(value, pd.Timestamp):
            return value.strftime("%Y-%m-%d")
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value

    return {
        "run_id": str(_value("run_id")),
        "model_name": str(_value("model_name")),
        "model_source": str(_value("model_source")),
        "model_category": str(_value("model_category")),
        "checkpoint_path": str(_value("checkpoint_path")),
        "topk": int(float(_value("topk", 0) or 0)),
        "holding_days": int(float(_value("holding_days", 0) or 0)),
        "rank_by": str(_value("rank_by", "score")),
        "daily_returns_csv": str(_value("daily_returns_csv")),
        "prediction_csv": str(_value("prediction_csv")),
        "selected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def format_strategy_option(row: pd.Series) -> str:
    model_name = str(row.get("model_name", "未知模型"))
    topk = row.get("topk", "")
    holding_days = row.get("holding_days", "")
    annual_return = pd.to_numeric(row.get("annual_return"), errors="coerce")
    target_hit = str(row.get("target_hit", "")).lower() == "true"
    hit_text = "达标" if target_hit else "未达标"
    return_text = f"{annual_return:.2%}" if pd.notna(annual_return) else "N/A"
    run_id = str(row.get("run_id", ""))[-8:]
    return f"{model_name} | TopK {topk} | 持有 {holding_days} | 年化 {return_text} | {hit_text} | {run_id}"
