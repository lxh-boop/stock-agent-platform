from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from alpha158 import add_alpha158_features
from config import LATEST_RAW_DATA_PATH, RAW_DATA_PATH, TRAIN_RAW_DATA_PATH


DEFAULT_RECENT_TRADING_DAYS = max(
    1,
    int(os.environ.get("STOCK_ALPHA158_RECENT_TRADING_DAYS", "120")),
)
_ALPHA_INPUT_NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "vwap")


def _normalise_raw(
    frame: pd.DataFrame,
    *,
    source_numeric_dtype: str | None = None,
) -> pd.DataFrame:
    out = frame.copy()
    required = {"date", "code", *_ALPHA_INPUT_NUMERIC_COLUMNS}
    missing = sorted(required.difference(out.columns))
    if missing:
        raise RuntimeError(f"raw market data missing columns: {missing}")

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["code"] = out["code"].astype(str).str.zfill(6)

    for col in _ALPHA_INPUT_NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")

    # Historical training Alpha158 was produced directly from Qlib's float32
    # DataFrame before RAW_DATA_PATH was serialized to CSV. Reading that CSV
    # back with pandas promotes those values to float64. Restore the original
    # Qlib arithmetic only for the training provenance that is known to have
    # come from RAW_DATA_PATH. Do not globally down-cast latest/Tushare data.
    if source_numeric_dtype:
        dtype = str(source_numeric_dtype).strip().lower()
        if dtype != "float32":
            raise ValueError(f"unsupported source_numeric_dtype: {source_numeric_dtype}")
        for col in _ALPHA_INPUT_NUMERIC_COLUMNS:
            out[col] = out[col].astype("float32")

    out = out.dropna(
        subset=["date", "code", *_ALPHA_INPUT_NUMERIC_COLUMNS]
    )
    return out.sort_values(["code", "date"]).reset_index(drop=True)


def _output_dates(
    raw: pd.DataFrame,
    output_trading_days: int | None,
) -> set[pd.Timestamp] | None:
    if not output_trading_days:
        return None
    dates = sorted(
        pd.to_datetime(raw["date"], errors="coerce")
        .dropna()
        .dt.normalize()
        .unique()
    )
    keep = max(1, int(output_trading_days))
    return {pd.Timestamp(d) for d in dates[-keep:]}


def build_alpha158(
    raw_data: pd.DataFrame,
    *,
    output_trading_days: int | None = None,
    source_numeric_dtype: str | None = None,
) -> pd.DataFrame:
    """Recompute Alpha158 from the full current raw history for every call.

    The raw history is never truncated before Alpha158 calculation. An optional
    output slice is applied only after all factors have been recomputed.

    ``source_numeric_dtype`` is a provenance restoration knob, not a factor
    cache. It is used only when a persisted raw CSV is known to have originated
    from a different in-memory numeric dtype. Current training RAW_DATA_PATH is
    the observed case: it was written from a Qlib float32 frame after the old
    training factors had already been calculated from that frame.
    """
    raw = _normalise_raw(
        raw_data,
        source_numeric_dtype=source_numeric_dtype,
    )
    output_dates = _output_dates(raw, output_trading_days)
    features = add_alpha158_features(raw)
    features["date"] = pd.to_datetime(features["date"], errors="coerce")
    features["code"] = features["code"].astype(str).str.zfill(6)
    if output_dates is not None:
        normalised_dates = pd.to_datetime(features["date"]).dt.normalize()
        features = features.loc[normalised_dates.isin(output_dates)].copy()
    return features.reset_index(drop=True)


def load_raw_data(
    path: str | Path,
    *,
    source_numeric_dtype: str | None = None,
) -> pd.DataFrame:
    raw_path = Path(path)
    if not raw_path.is_file():
        raise FileNotFoundError(f"raw market data not found: {raw_path}")
    frame = pd.read_csv(
        raw_path,
        dtype={"code": str},
        encoding="utf-8-sig",
    )
    return _normalise_raw(
        frame,
        source_numeric_dtype=source_numeric_dtype,
    )


def alpha158_from_file(
    path: str | Path,
    *,
    output_trading_days: int | None = None,
    source_numeric_dtype: str | None = None,
) -> pd.DataFrame:
    """Read authoritative raw data and recompute Alpha158 for this call.

    Derived Alpha158 values are never persisted and never reused through an
    in-process factor cache.
    """
    raw = load_raw_data(
        path,
        source_numeric_dtype=source_numeric_dtype,
    )
    return build_alpha158(
        raw,
        output_trading_days=output_trading_days,
    )


def latest_alpha158(
    *,
    recent_trading_days: int = DEFAULT_RECENT_TRADING_DAYS,
) -> pd.DataFrame:
    # Latest cache is produced by the daily CSV/Tushare merge path and must
    # retain pandas' native float64 CSV-read semantics observed in production.
    return alpha158_from_file(
        LATEST_RAW_DATA_PATH,
        output_trading_days=recent_trading_days,
    )


def training_raw_path() -> Path:
    train = Path(TRAIN_RAW_DATA_PATH)
    if train.is_file():
        return train
    raw = Path(RAW_DATA_PATH)
    if raw.is_file():
        return raw
    raise FileNotFoundError(f"training raw data not found: {train} or {raw}")


def _same_configured_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)
    except Exception:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )


def training_source_numeric_dtype(path: str | Path) -> str | None:
    """Return the observed source dtype that must be restored for training.

    Current project evidence shows RAW_DATA_PATH is written from the Qlib frame
    returned by data_local.load_local_qlib_data(), while the legacy training
    Alpha158 CSV was computed from that same frame before the CSV round-trip.
    Therefore only this known fallback source restores float32. A future
    TRAIN_RAW_DATA_PATH is left native unless its provenance is established.
    """
    if _same_configured_path(path, RAW_DATA_PATH):
        return "float32"
    return None


def training_alpha158() -> pd.DataFrame:
    path = training_raw_path()
    return alpha158_from_file(
        path,
        output_trading_days=None,
        source_numeric_dtype=training_source_numeric_dtype(path),
    )


@dataclass(frozen=True)
class Alpha158Provider:
    """Compatibility layer placed before existing downstream consumers."""

    def latest(
        self,
        *,
        recent_trading_days: int = DEFAULT_RECENT_TRADING_DAYS,
    ) -> pd.DataFrame:
        return latest_alpha158(recent_trading_days=recent_trading_days)

    def training(self) -> pd.DataFrame:
        return training_alpha158()

    def from_raw(
        self,
        raw_data: pd.DataFrame,
        *,
        output_trading_days: int | None = None,
        source_numeric_dtype: str | None = None,
    ) -> pd.DataFrame:
        return build_alpha158(
            raw_data,
            output_trading_days=output_trading_days,
            source_numeric_dtype=source_numeric_dtype,
        )
