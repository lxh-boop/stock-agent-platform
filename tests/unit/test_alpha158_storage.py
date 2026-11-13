"""无需网络/数据库的因子算法、窗口、补数与失败保护测试。"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import json
import numpy as np
import pandas as pd
import pytest

from core.alpha158_contract import FEATURE_COLUMNS, LOOKBACK_ROWS, retention_cutoff
from factor_storage import refresh_alpha158, load_stored_alpha158
from factor_provider import build_alpha158
from factor_history import sync_factor_raw_history


def raw_frame(n=180, start='2025-01-01', codes=('000001',)):
    frames = []
    for index, code in enumerate(codes):
        rng = np.random.default_rng(42 + index)
        close = 10 + rng.normal(0, .1, n).cumsum()
        frames.append(pd.DataFrame({'date': pd.bdate_range(start, periods=n), 'code': code, 'name': code,
            'open': close * .99, 'high': close * 1.02, 'low': close * .98, 'close': close,
            'volume': rng.integers(1000, 9000, n).astype(float), 'vwap': close * 1.001, 'amount': close * 5000}))
    return pd.concat(frames, ignore_index=True)


class MemoryStore:
    def __init__(self):
        self.rows, self.asof, self.cutoff = {}, None, None
        self.fail_finish = False
    @contextmanager
    def update(self, asof):
        before = deepcopy((self.rows, self.asof, self.cutoff))
        if self.asof and asof < self.asof:
            raise ValueError('alpha158_stale_signal_date')
        self.day, self.cutoff, self.written = asof, retention_cutoff(asof), 0
        try:
            yield self
        except Exception:
            self.rows, self.asof, self.cutoff = before
            raise
    def fingerprints(self, code):
        return {day: row['hash'] for (stock, day), row in self.rows.items() if stock == code}
    def stage(self, features, hashes):
        for row in features.to_dict('records'):
            day = row['date'].date()
            self.rows[(row['code'], day)] = {'hash': hashes[day], 'row': row}
            self.written += 1
    def finish(self):
        if self.fail_finish:
            raise RuntimeError('injected_failure')
        old = [key for key in self.rows if key[1] < self.cutoff]
        for key in old:
            del self.rows[key]
        self.asof = self.day
        return {'rows_written': self.written, 'rows_deleted': len(old), 'retention_cutoff': str(self.cutoff)}
    def read(self, **kwargs):
        return pd.DataFrame([x['row'] for x in self.rows.values()])


def test_contract_and_calendar_years():
    assert len(FEATURE_COLUMNS) == len(set(FEATURE_COLUMNS)) == 158
    assert retention_cutoff('2026-11-13') == date(2021, 11, 13)
    assert retention_cutoff('2024-02-29') == date(2019, 2, 28)
    assert retention_cutoff('2025-03-01') == date(2020, 3, 1)


def test_incremental_matches_full_and_only_computes_new_rows():
    raw = raw_frame(codes=('000001','000002'))
    day = raw['date'].max()
    store = MemoryStore()
    refresh_alpha158(raw[raw.date < day], as_of=raw[raw.date < day].date.max(), store=store)
    stats = refresh_alpha158(raw, as_of=day, store=store)
    assert stats['rows_written'] == 2
    assert stats['calculation_input_rows'] == 2 * (LOOKBACK_ROWS + 1)
    expected = build_alpha158(raw)
    actual = load_stored_alpha158(store=store)
    expected = expected.sort_values(['code','date']).reset_index(drop=True)
    np.testing.assert_allclose(actual[list(FEATURE_COLUMNS)], expected[list(FEATURE_COLUMNS)], rtol=1e-8, atol=1e-8, equal_nan=True)
    assert refresh_alpha158(raw, as_of=day, store=store)['rows_written'] == 0


def test_historical_correction_invalidates_only_dependent_windows():
    raw = raw_frame(n=180)
    store = MemoryStore()
    refresh_alpha158(raw, as_of=raw.date.max(), store=store)
    corrected = raw.copy()
    corrected.loc[80, 'close'] += 1
    stats = refresh_alpha158(corrected, as_of=raw.date.max(), store=store)
    assert stats['rows_written'] == LOOKBACK_ROWS + 1
    expected = build_alpha158(corrected)
    actual = load_stored_alpha158(store=store)
    np.testing.assert_allclose(actual[list(FEATURE_COLUMNS)], expected[list(FEATURE_COLUMNS)], rtol=1e-7, atol=1e-7, equal_nan=True)


def test_prune_boundary_and_failed_write_preserve_previous_data():
    raw = raw_frame(n=180, start='2021-01-01')
    store = MemoryStore()
    refresh_alpha158(raw, as_of=raw.date.max(), store=store)
    latest = raw.iloc[[-1]].assign(date=pd.Timestamp('2026-11-13'))
    next_raw = pd.concat([raw, latest], ignore_index=True)
    before = deepcopy(store.rows)
    store.fail_finish = True
    with pytest.raises(RuntimeError, match='injected_failure'):
        refresh_alpha158(next_raw, as_of='2026-11-13', store=store)
    assert set(before) == set(store.rows)
    store.fail_finish = False
    stats = refresh_alpha158(next_raw, as_of='2026-11-13', store=store)
    assert stats['rows_deleted'] == len(raw)
    assert min(day for _, day in store.rows) >= date(2021,11,13)


def test_invalid_or_missing_latest_data_never_prunes():
    raw = raw_frame()
    store = MemoryStore()
    refresh_alpha158(raw, as_of=raw.date.max(), store=store)
    before = set(store.rows)
    with pytest.raises(RuntimeError, match='missing_no_prune'):
        refresh_alpha158(raw, as_of=raw.date.max() + pd.Timedelta(days=1), store=store)
    assert set(store.rows) == before
    with pytest.raises(ValueError, match='stale_signal'):
        refresh_alpha158(raw, as_of=raw.date.iloc[-2], store=store)


def test_changed_future_data_cannot_change_past_factors():
    raw = raw_frame()
    one, two = MemoryStore(), MemoryStore()
    day = raw.date.iloc[120]
    refresh_alpha158(raw, as_of=day, store=one)
    raw.loc[raw.date > day, 'close'] *= 10
    refresh_alpha158(raw, as_of=day, store=two)
    np.testing.assert_allclose(load_stored_alpha158(store=one)[list(FEATURE_COLUMNS)],
                               load_stored_alpha158(store=two)[list(FEATURE_COLUMNS)], equal_nan=True)


def test_gap_dates_are_inserted_and_database_read_does_not_recompute(monkeypatch):
    raw, store = raw_frame(), MemoryStore()
    refresh_alpha158(raw.drop(index=100), as_of=raw.date.max(), store=store)
    stats = refresh_alpha158(raw, as_of=raw.date.max(), store=store)
    assert stats['rows_written'] == 61  # 新增行及其后 60 行依赖窗口。
    import alpha158
    monkeypatch.setattr(alpha158, 'add_alpha158_features', lambda *a: pytest.fail('unexpected recompute'))
    assert len(load_stored_alpha158(store=store)) == len(raw)


def test_history_checkpoint_and_missing_batch_protection(tmp_path):
    raw = raw_frame(n=20)
    days = list(raw.date.dt.strftime('%Y%m%d'))
    class Pro:
        def trade_cal(self, **kwargs):
            return pd.DataFrame({'cal_date':days,'is_open':1})
    calls = []
    def fetcher(**kwargs):
        calls.append(kwargs['trade_dates'])
        assert kwargs['require_complete']
        result = raw[raw.date.dt.strftime('%Y%m%d').isin(kwargs['trade_dates'])].copy()
        result.attrs['completed_trade_dates'] = kwargs['trade_dates']
        return result
    def merge(old, new, pool):
        return pd.concat([old,new]).drop_duplicates(['date','code'],keep='last').sort_values(['code','date'])
    args = dict(pro=Pro(), token='test-token', stock_pool={'000001':'test'}, as_of=raw.date.max(),
                raw_path=tmp_path/'raw.csv', fetcher=fetcher, normalize=lambda f,p:f, merge=merge, batch_size=7)
    result, stats = sync_factor_raw_history(old_raw=pd.DataFrame(), **args)
    assert stats['requested_dates'] == 20
    assert len(calls) == 3
    assert 'test-token' not in (tmp_path/'raw.alpha158-coverage.json').read_text()
    calls.clear()
    result, stats = sync_factor_raw_history(old_raw=result, **args)
    assert stats['requested_dates'] == 10
    args['fetcher'] = lambda **kwargs: pd.DataFrame()
    with pytest.raises(RuntimeError, match='incomplete_batch'):
        sync_factor_raw_history(old_raw=result, **args)


def test_live_store_requires_authenticated_scope():
    from core.user_scope import user_scope, system_scope
    from database.alpha158_store import Alpha158Store
    with system_scope(), pytest.raises(ValueError, match='authenticated_user'):
        Alpha158Store()
    with user_scope('alice'):
        store = Alpha158Store()
    with user_scope('bob'), pytest.raises(ValueError, match='owner_mismatch'):
        store.read()


def test_strict_downloader_rejects_partial_dates(monkeypatch):
    import data_tushare as module
    monkeypatch.setattr(module, 'init_tushare_pro', lambda _: object())
    monkeypatch.setattr(module, 'resolve_daily_data_end_date', lambda **kwargs: ('20261113','test'))
    monkeypatch.setattr(module, '_fetch_one_trade_date_daily_fast', lambda **kwargs: (kwargs['trade_date'],pd.DataFrame(),'empty_daily'))
    with pytest.raises(RuntimeError, match='下载不完整'):
        module.fetch_stock_pool_recent_daily_fast('test',stock_pool={'000001':'test'},
            end_date='20261113', trade_dates=['20261113'], require_complete=True,
            max_workers=1, sleep_seconds=0)
    monkeypatch.setattr(module, '_fetch_one_trade_date_daily_fast', lambda **kwargs: (kwargs['trade_date'],pd.DataFrame(),'empty_stock_pool'))
    result = module.fetch_stock_pool_recent_daily_fast('test',stock_pool={'000001':'test'},
        end_date='20261113', trade_dates=['20261113'], require_complete=True,
        max_workers=1, sleep_seconds=0)
    assert result.empty and result.attrs['completed_trade_dates'] == ['20261113']


def test_oldest_retained_day_uses_pre_window_raw_history():
    raw, store = raw_frame(n=1600, start='2020-01-01'), MemoryStore()
    day = raw.date.max()
    cutoff = pd.Timestamp(retention_cutoff(day))
    refresh_alpha158(raw, as_of=day, store=store)
    actual = load_stored_alpha158(store=store)
    assert actual.date.min() >= cutoff
    expected = build_alpha158(raw).loc[lambda f:f.date >= cutoff].reset_index(drop=True)
    assert actual.MA60.iloc[0] == pytest.approx(expected.MA60.iloc[0])
    assert not pd.isna(actual.MA60.iloc[0])
    np.testing.assert_allclose(actual[list(FEATURE_COLUMNS)],expected[list(FEATURE_COLUMNS)], rtol=1e-7, atol=1e-7,equal_nan=True)


def test_history_resume_does_not_redownload_committed_batches(tmp_path):
    raw = raw_frame(n=30)
    days = list(raw.date.dt.strftime('%Y%m%d'))
    class Pro:
        def trade_cal(self, **kwargs):
            return pd.DataFrame({'cal_date':days,'is_open':'1'})
    attempts = []
    failed = [False]
    def fetcher(**kwargs):
        attempts.append(kwargs['trade_dates'])
        if len(attempts) == 2 and not failed[0]:
            failed[0] = True
            raise RuntimeError('network_unavailable')
        result = raw[raw.date.dt.strftime('%Y%m%d').isin(kwargs['trade_dates'])].copy()
        result.attrs['completed_trade_dates'] = kwargs['trade_dates']
        return result
    def merge(old,new,pool):
        return pd.concat([old,new]).drop_duplicates(['date','code'],keep='last')
    args=dict(pro=Pro(),token='x',stock_pool={'000001':'x'},as_of=raw.date.max(),
              raw_path=tmp_path/'raw.csv',fetcher=fetcher, normalize=lambda f,p:f,merge=merge,batch_size=10)
    with pytest.raises(RuntimeError,match='network_unavailable'):
        sync_factor_raw_history(old_raw=pd.DataFrame(),**args)
    cached = pd.read_csv(tmp_path/'raw.csv',dtype={'code':str},parse_dates=['date'])
    assert len(cached)==10
    attempts.clear()
    result, stats = sync_factor_raw_history(old_raw=cached,**args)
    assert stats['requested_dates']==20
    assert not set(days[:10]).intersection(day for batch in attempts for day in batch)
    assert len(result)==30


def test_provider_database_error_does_not_silently_recompute(monkeypatch):
    import factor_storage as storage
    from factor_provider import latest_alpha158
    def unavailable():
        raise RuntimeError('database_unavailable')
    monkeypatch.setattr(storage,'Alpha158Store',unavailable)
    with pytest.raises(RuntimeError,match='database_unavailable'):
        latest_alpha158()
