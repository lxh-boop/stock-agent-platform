"""按交易日分批补齐滚动历史，下载成功才前移缓存水位。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Callable
import pandas as pd
from core.alpha158_contract import as_date, raw_history_start


def _replace_file(path: Path, write: Callable) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    os.close(handle)
    try:
        write(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_factor_raw_history(*, pro, token: str, stock_pool: dict, old_raw: pd.DataFrame,
                            as_of: object, raw_path: str | Path, fetcher: Callable,
                            normalize: Callable, merge: Callable, fetch_workers: int | None = None,
                            batch_size: int = 40) -> tuple[pd.DataFrame, dict]:
    """首轮补五年+行情预热；以后补交易日缺口并复查最近十日，不截断停更缺口。"""
    end, start = as_date(as_of), raw_history_start(as_of)
    calendar = pro.trade_cal(exchange='SSE', start_date=start.strftime('%Y%m%d'),
                             end_date=end.strftime('%Y%m%d'), is_open='1', fields='cal_date,is_open')
    if calendar is None or calendar.empty:
        raise RuntimeError('alpha158_history_calendar_unavailable')
    days = sorted({str(value).replace('-', '')[:8] for value in calendar.loc[
        calendar['is_open'].astype(str).eq('1'), 'cal_date']})
    if not days or days[-1] != end.strftime('%Y%m%d'):
        raise RuntimeError('alpha158_history_calendar_target_missing')
    cache = Path(raw_path)
    progress_path = cache.with_suffix('.alpha158-coverage.json')
    saved = {}
    if progress_path.is_file():
        try:
            saved = json.loads(progress_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    codes = sorted(str(code) for code in stock_pool)
    raw = old_raw.copy()
    if not raw.empty and saved.get('stock_codes') == codes:
        covered = set(saved.get('completed_dates') or [])
    elif not raw.empty and not saved and set(codes).issubset(set(raw['code'])):
        covered = set(pd.to_datetime(raw['date']).dt.strftime('%Y%m%d'))
    else:
        # 新增股票或更换股票池，重新核验历史，但下载进度仍按批次保存。
        covered = set()
    needed = sorted((set(days) - covered) | set(days[-10:]))
    print(f'[Alpha158 History] pending_dates={len(needed)}, keep_from={start}, target={end}')
    fetched_rows = 0
    for offset in range(0, len(needed), max(1, batch_size)):
        requested = needed[offset:offset + max(1, batch_size)]
        fresh = fetcher(token=token, stock_pool=stock_pool, end_date=end.strftime('%Y%m%d'),
                        include_adj_factor=True, max_workers=fetch_workers,
                        trade_dates=requested, require_complete=True)
        completed = set(fresh.attrs.get('completed_trade_dates') or [])
        if not set(requested).issubset(completed):
            raise RuntimeError('alpha158_history_incomplete_batch_no_factor_prune')
        fetched_rows += len(fresh)
        if not fresh.empty:
            raw = merge(raw, normalize(fresh, stock_pool), stock_pool)
        if not raw.empty:
            _replace_file(cache, lambda path: raw.to_csv(path, index=False, encoding='utf-8-sig'))
        covered.update(completed)
        payload = {'stock_codes': codes, 'completed_dates': sorted(covered & set(days))}
        _replace_file(progress_path, lambda path: path.write_text(json.dumps(payload), encoding='utf-8'))
        print(f'[Alpha158 History] checkpoint={min(offset + len(requested), len(needed))}/{len(needed)}')
    if raw.empty or end not in set(pd.to_datetime(raw['date']).dt.date):
        raise RuntimeError('alpha158_history_target_missing_no_factor_prune')
    dates = pd.to_datetime(raw['date']).dt.date
    raw = raw.loc[(dates >= start) & (dates <= end)].copy().reset_index(drop=True)
    _replace_file(cache, lambda path: raw.to_csv(path, index=False, encoding='utf-8-sig'))
    return raw, {'requested_dates': len(needed), 'downloaded_rows': fetched_rows, 'raw_history_start': str(start)}
