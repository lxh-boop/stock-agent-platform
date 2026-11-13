"""Alpha158 存储契约：固定 158 个字段、五个自然年、只使用过去行情。"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import pandas as pd
from config import ALPHA_WINDOWS, EPS

BASE_FEATURES = ('KMID', 'KLEN', 'KMID2', 'KUP', 'KUP2', 'KLOW', 'KLOW2', 'KSFT', 'KSFT2',
                 'OPEN0', 'HIGH0', 'LOW0', 'VWAP0')
ROLLING_FEATURES = ('ROC', 'MA', 'STD', 'BETA', 'RSQR', 'RESI', 'MAX', 'MIN', 'QTLU', 'QTLD',
                    'RANK', 'RSV', 'IMAX', 'IMIN', 'IMXD', 'CORR', 'CORD', 'CNTP', 'CNTN',
                    'CNTD', 'SUMP', 'SUMN', 'SUMD', 'VMA', 'VSTD', 'WVMA', 'VSUMP', 'VSUMN', 'VSUMD')
FEATURE_COLUMNS = (*BASE_FEATURES, *(f'{prefix}{window}' for window in ALPHA_WINDOWS for prefix in ROLLING_FEATURES))
CONTEXT_COLUMNS = ('name', 'open', 'high', 'low', 'close', 'volume', 'vwap', 'amount',
                   'pct_chg', 'turnover', 'adj_factor', 'ret_5', 'ret_20', 'vol_20', 'drawdown_20')
RAW_COLUMNS = ('open', 'high', 'low', 'close', 'volume', 'vwap')
LOOKBACK_ROWS = max(ALPHA_WINDOWS)  # ROC60 和差分滚动项需要当日前的 60 条行情。
RETENTION_YEARS = 5


def as_date(value: object) -> date:
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError('alpha158_invalid_date')
    return result.date()


def retention_cutoff(as_of: object) -> date:
    """保留 [信号日减五个自然年, 信号日]；闰日回退到目标年二月末。"""
    return (pd.Timestamp(as_date(as_of)) - pd.DateOffset(years=RETENTION_YEARS)).date()


def raw_history_start(as_of: object) -> date:
    # 原始行情多保留一年作预热；该额外区间不保存因子值。
    return (pd.Timestamp(retention_cutoff(as_of)) - pd.DateOffset(years=1)).date()


@lru_cache(maxsize=1)
def feature_version() -> str:
    """公式或窗口变化会自动改变版本，避免静默复用旧公式的结果。"""
    formula = (Path(__file__).resolve().parents[1] / 'alpha158.py').read_bytes()
    spec = json.dumps({'windows': ALPHA_WINDOWS, 'eps': EPS, 'columns': FEATURE_COLUMNS,
                       'arithmetic': 'live-float64-v1'}, sort_keys=True).encode()
    return 'alpha158-' + hashlib.sha256(formula + spec).hexdigest()[:24]
