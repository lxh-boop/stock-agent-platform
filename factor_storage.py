"""日更 Alpha158 增量计算与数据库读取；纯计算训练接口仍由 factor_provider 提供。"""
from __future__ import annotations

import hashlib
import numpy as np
import pandas as pd

from alpha158 import add_alpha158_for_one_stock, add_future_score_label
from core.alpha158_contract import (
    FEATURE_COLUMNS, RAW_COLUMNS, CONTEXT_COLUMNS, LOOKBACK_ROWS,
    as_date, retention_cutoff,
)
from config import PRED_HORIZON
from database.alpha158_store import Alpha158Store


def dependency_hashes(group: pd.DataFrame) -> dict:
    """指纹包含本行及前 60 行；历史修订会使受影响的后续窗口失效。"""
    columns = ['date', 'code', *RAW_COLUMNS]
    values = pd.util.hash_pandas_object(group[columns], index=False).to_numpy(dtype='<u8')
    metadata = [name for name in CONTEXT_COLUMNS if name in group and name not in ('ret_5', 'ret_20', 'vol_20', 'drawdown_20')]
    meta_frame = group[metadata].copy()
    for key in metadata:
        if key != 'name':
            meta_frame[key] = pd.to_numeric(meta_frame[key], errors='coerce').astype('float64')
    row_meta = pd.util.hash_pandas_object(meta_frame, index=False).to_numpy(dtype='<u8')
    return {as_date(day): hashlib.sha256(
        values[max(0, index - LOOKBACK_ROWS):index + 1].tobytes() + row_meta[index:index + 1].tobytes()
    ).hexdigest() for index, day in enumerate(group['date'])}


def refresh_alpha158(raw_data: pd.DataFrame, *, as_of: object, store=None) -> dict:
    """单次提交：补缺/重算变更窗口 → UPSERT → 清理五年前因子 → 更新水位。"""
    from factor_provider import _normalise_raw
    if len(FEATURE_COLUMNS) != 158:
        raise ValueError('alpha158_contract_requires_158_columns')
    day = as_date(as_of)
    raw = _normalise_raw(raw_data)
    raw = raw.loc[raw['date'].dt.date <= day].drop_duplicates(['code', 'date'], keep='last').copy()
    raw[list(RAW_COLUMNS)] = raw[list(RAW_COLUMNS)].astype('float64')
    if raw.empty or raw['date'].max().date() != day:
        raise RuntimeError('alpha158_target_date_missing_no_prune')
    if not np.isfinite(raw[list(RAW_COLUMNS)].to_numpy()).all():
        raise ValueError('alpha158_nonfinite_raw_input')
    cutoff = retention_cutoff(day)
    repository = store if store is not None else Alpha158Store()
    input_rows, calculated_rows, reused_rows = 0, 0, 0
    with repository.update(day) as writer:
        for code, group in raw.groupby('code', sort=True):
            group = group.sort_values('date').reset_index(drop=True)
            fingerprints = dependency_hashes(group)
            stored = writer.fingerprints(str(code))
            group_dates = list(group['date'].dt.date)
            eligible = [index for index, day_value in enumerate(group_dates) if day_value >= cutoff]
            dirty = [index for index in eligible if stored.get(group_dates[index]) != fingerprints[group_dates[index]]]
            reused_rows += len(eligible) - len(dirty)
            if not dirty:
                continue
            # 相邻依赖区间合并，既不遗漏修订影响，也不每日重算五年历史。
            ranges = []
            for index in dirty:
                left = max(0, index - LOOKBACK_ROWS)
                if ranges and left <= ranges[-1][1] + 1:
                    ranges[-1][1] = index
                    ranges[-1][2].append(index)
                else:
                    ranges.append([left, index, [index]])
            for left, right, indices in ranges:
                part = group.iloc[left:right + 1].copy()
                input_rows += len(part)
                computed = add_alpha158_for_one_stock(part).replace([np.inf, -np.inf], np.nan)
                dates = set(group.iloc[indices]['date'])
                changed = computed.loc[computed['date'].isin(dates)]
                writer.stage(changed, fingerprints)
                calculated_rows += len(changed)
        stats = writer.finish()
    return {**stats, 'calculated_rows': calculated_rows, 'calculation_input_rows': input_rows, 'reused_rows': reused_rows}


def load_stored_alpha158(*, recent_trading_days: int | None = 120, end_date: object | None = None,
                         stock_codes: list[str] | None = None, store=None) -> pd.DataFrame:
    """因子值只从数据库读取；需要的未来标签在返回时计算，不写进因子表。"""
    repository = store if store is not None else Alpha158Store()
    data = repository.read(recent_trading_days=recent_trading_days, end_date=end_date, stock_codes=stock_codes)
    if data.empty:
        raise RuntimeError('alpha158_not_initialized_run_daily_update_first')
    data = data.sort_values(['code', 'date']).reset_index(drop=True)
    close = pd.to_numeric(data['close'], errors='coerce')
    data['future_5d_ret'] = close.groupby(data['code']).shift(-PRED_HORIZON) / close - 1
    data['future_5d_up'] = (data['future_5d_ret'] > 0).astype(int)
    return add_future_score_label(data)
