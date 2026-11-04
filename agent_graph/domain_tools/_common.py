from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from scheduler.trading_calendar import get_latest_trading_day


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in [None, ""]:
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except Exception:
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value in [None, ""]:
            return default
        return int(float(value))
    except Exception:
        return default


def normalize_stock_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.split(".")[0]
    digits = re.sub(r"\D", "", text)
    return digits[-6:].zfill(6) if digits else ""


def parse_jsonish(value: Any, default: Any = None) -> Any:
    if value in [None, ""]:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        if "," in text:
            return [item.strip() for item in text.split(",") if item.strip()]
        return default


def to_serializable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): to_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_serializable(item) for item in value]
    return value


def load_csv_dataframe(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str)


def dataframe_records(path: str | Path) -> list[dict[str, Any]]:
    df = load_csv_dataframe(path)
    if df.empty:
        return []
    return df.fillna("").to_dict("records")


def first_present(row: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in [None, ""]:
            return value
    return default


def latest_trade_date(records: list[dict[str, Any]]) -> str:
    dates = [
        str(first_present(item, ["trade_date", "date", "signal_date"], "")).strip()[:10]
        for item in records
    ]
    dates = [item for item in dates if item]
    if dates:
        return sorted(dates)[-1]
    return get_latest_trading_day(datetime.now()).strftime("%Y-%m-%d")


def portfolio_user_dir(output_dir: str | Path, user_id: str) -> Path:
    return Path(output_dir) / "portfolio" / str(user_id)


def is_valid_agent_price(value: Any) -> bool:
    price = safe_float(value, 0.0)
    return price > 0 and abs(price - 1.0) > 1e-9


def action_is_hard_risk(action: Any, risk_warning: Any = "") -> bool:
    text = f"{action or ''} {risk_warning or ''}".lower()
    return any(token in text for token in ["exclude", "risk_alert", "hard", "forced"])


def cap_weight_by_risk_level(risk_level: str | None) -> float:
    text = str(risk_level or "C3").upper()
    if "C1" in text:
        return 0.03
    if "C2" in text:
        return 0.05
    if "C4" in text:
        return 0.10
    if "C5" in text:
        return 0.15
    return 0.08


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
