"""因子研究与每日选股；没有交易账户写入，也不会生成伪随机收益。"""
from collections import defaultdict, deque
import hashlib
import json
import math

import numpy as np
from backtesting.contracts import AnalysisSpec, ScreenSpec, DISCLAIMER, ENGINE_VERSION, MAX_DAYS
from backtesting.engine import validate_rows
from backtesting.signals import SignalState, adjusted_return, finite, members, spearman, target_weights


def _hash(digest, day, rows, market=None):
    digest.update(json.dumps([str(day), rows, market or {}], sort_keys=True,
                             ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode())


def _groups(values: dict[str, float], count: int) -> list[list[str]]:
    codes = sorted(values, key=lambda c: (-values[c], c))
    q, r = divmod(len(codes), count)
    offset, out = 0, []
    for i in range(count):
        n = q + int(i < r)
        out.append(codes[offset:offset+n]); offset += n
    return out


def analyze(spec: AnalysisSpec, days, *, markets=None, on_progress=lambda _: None,
            cancelled=lambda: False) -> dict:
    digest = hashlib.sha256()
    samples, previous, day_index, last_day = deque(), None, 0, None
    ic_rows, segment_rows, ls_rows, decay_rows, correlation_rows = [], [], [], [], []
    correlations, decays = defaultdict(list), defaultdict(list)
    long_nav = {f.field: 1. for f in spec.factors}
    missing_pairs = 0

    def compare(before, end_day, rows):
        nonlocal missing_pairs
        start_day, sections, prices = before
        returns = {c: adjusted_return(old, rows[c]) for c, old in prices.items() if c in rows}
        returns = {c: v for c, v in returns.items() if v is not None}
        missing_pairs += len(prices) - len(returns)
        for factor in spec.factors:
            values = sections[factor.field]
            paired = sorted(set(values) & set(returns))
            ic_rows.append({'date': str(start_day), 'end_date': str(end_day), 'factor': factor.field,
                            'ic': spearman([values[c] for c in paired], [returns[c] for c in paired]),
                            'pairs': len(paired)})
            period_returns = []
            for i, codes in enumerate(_groups(values, spec.segment_count)):
                usable = [returns[c] for c in codes if c in returns]
                value = float(np.mean(usable)) if usable else None
                period_returns.append(value)
                segment_rows.append({'date': str(start_day), 'end_date': str(end_day), 'factor': factor.field,
                                     'segment': i + 1, 'return': value, 'count': len(usable),
                                     'signal_count': len(codes)})
            first, final = period_returns[0], period_returns[-1]
            spread = first - final if first is not None and final is not None else None
            if spread is not None:
                long_nav[factor.field] *= 1 + spread
            ls_rows.append({'date': str(end_day), 'factor': factor.field, 'return': spread,
                            'nav': long_nav[factor.field] if spread is not None else None})

    for day, raw in days:
        if cancelled(): raise InterruptedError('因子分析已取消')
        if day > spec.end_date or (last_day and day <= last_day): raise ValueError('分析交易日顺序无效')
        last_day = day
        if day < spec.start_date: continue
        if day_index >= MAX_DAYS: raise ValueError('分析区间超过交易日上限')
        rows = validate_rows(day, raw)
        _hash(digest, day, rows)
        # 过去采样截面 vs 今天已兑现收益：用于持有1~N个交易日的IC衰减。
        for sample_index, sample in list(samples):
            lag = day_index - sample_index
            if lag > spec.decay_days: continue
            _, sections, prices = sample
            returns = {c: adjusted_return(p, rows[c]) for c, p in prices.items() if c in rows}
            for factor in spec.factors:
                values = sections[factor.field]
                paired = sorted(set(values) & set(returns))
                value = spearman([values[c] for c in paired], [returns[c] for c in paired])
                if value is not None: decays[(factor.field, lag)].append(value)
        while samples and day_index - samples[0][0] >= spec.decay_days: samples.popleft()
        if day_index % spec.interval_days == 0:
            pool = members(spec, day, rows)
            sections = {f.field: {c: float(rows[c][f.field]) * f.direction for c in pool
                                  if finite(rows[c].get(f.field)) is not None} for f in spec.factors}
            sample = (day, sections, {c: rows[c] for c in pool})
            if previous is not None: compare(previous, day, rows)
            previous = sample
            samples.append((day_index, sample))
            for i, left in enumerate(spec.factors):
                for right in spec.factors[i:]:
                    paired = sorted(set(sections[left.field]) & set(sections[right.field]))
                    value = spearman([sections[left.field][c] for c in paired], [sections[right.field][c] for c in paired])
                    if value is not None: correlations[(left.field, right.field)].append((value, len(paired)))
        day_index += 1
        if day_index % 10 == 0: on_progress(day_index)
    if day_index < 2 or not ic_rows: raise ValueError('有效交易日不足以形成一个分析区间，请扩大日期或缩短分析间隔')
    factor_metrics = {}
    for f in spec.factors:
        values = [r['ic'] for r in ic_rows if r['factor'] == f.field and r['ic'] is not None]
        group_means, cumulative = [], []
        for i in range(1, spec.segment_count + 1):
            returns = [r['return'] for r in segment_rows if r['factor'] == f.field and r['segment'] == i and r['return'] is not None]
            group_means.append(float(np.mean(returns)) if returns else None)
            cumulative.append(float(np.prod([1+r for r in returns])-1) if returns else None)
        pairs = [(i, r) for i, r in enumerate(group_means) if r is not None]
        # 2组也能判定方向；通用Spearman要求3样本，研究单调性单独容许2组。
        rho = spearman(*zip(*pairs)) if len(pairs) >= 3 else (
            math.copysign(1., pairs[1][1]-pairs[0][1]) if len(pairs) == 2 and pairs[1][1] != pairs[0][1] else None)
        std = float(np.std(values, ddof=1)) if len(values) >= 2 else 0.
        factor_metrics[f.field] = {'ic_mean': float(np.mean(values)) if values else None,
            'ic_ir': float(np.mean(values)) / std if std > 1e-12 else None,
            'ic_positive_ratio': sum(v > 0 for v in values) / len(values) if values else None,
            'period_count': len(values), 'segment_mean_returns': group_means, 'segment_cumulative_returns': cumulative,
            'monotonicity_rho': rho, 'monotonicity_r2': rho*rho if rho is not None else None,
            'long_short_spread': group_means[0]-group_means[-1] if group_means[0] is not None and group_means[-1] is not None else None}
        for lag in range(1, spec.decay_days + 1):
            observations = decays[(f.field, lag)]
            decay_rows.append({'factor': f.field, 'lag': lag, 'ic': float(np.mean(observations)) if observations else None,
                               'period_count': len(observations)})
        for g in spec.factors:
            key = (f.field, g.field) if (f.field, g.field) in correlations else (g.field, f.field)
            observations = correlations[key]
            correlation_rows.append({'left': f.field, 'right': g.field,
                'correlation': float(np.mean([v for v, _ in observations])) if observations else None,
                'period_count': len(observations), 'mean_pairs': float(np.mean([n for _, n in observations])) if observations else 0})
    if cancelled(): raise InterruptedError('因子分析已取消')
    return {'kind': 'analysis', 'engine_version': ENGINE_VERSION, 'spec': spec.model_dump(mode='json'),
        'data_hash': digest.hexdigest(), 'metrics': {'days': day_index, 'factor_count': len(spec.factors),
            'period_count': max((v['period_count'] for v in factor_metrics.values()), default=0)},
        'factor_metrics': factor_metrics, 'ic_series': ic_rows, 'segments': segment_rows, 'long_short': ls_rows,
        'ic_decay': decay_rows, 'correlation': correlation_rows,
        'diagnostics': {'missing_return_pairs': missing_pairs},
        'assumptions': ['IC与因子相关性采用方向调整后的Spearman平均秩，缺值配对剔除；不足3只或零方差显示空值。',
            '分层为信号日排序连续均分，第一层为优先层；收益采用下一采样日复权收盘收益，等权且不计费用。',
            '多空净值为首尾层收益差复合的研究序列，不是可直接交易的组合；不等同账户回测。',
            'IR为有效IC均值除样本标准差，不年化；单调性同时报告方向rho与r²。',
            '缺行情的收益配对剔除并计数，不默认为零；没有重建未提供的历史成分。'], 'disclaimer': DISCLAIMER}


def screen(spec: ScreenSpec, days, *, markets=None, on_progress=lambda _: None,
           cancelled=lambda: False) -> dict:
    state, digest, latest = SignalState(spec), hashlib.sha256(), None
    markets = markets or {}
    previous_day = None
    for day, raw in days:
        if cancelled(): raise InterruptedError('选股已取消')
        if day > spec.end_date or (previous_day and day <= previous_day): raise ValueError('选股日期顺序无效')
        previous_day = day
        rows = validate_rows(day, raw)
        market = markets.get(day, {})
        _hash(digest, day, rows, market)
        state.observe(day, rows)
        exposure, votes = state.timing_step(market)
        if day >= spec.start_date:
            latest = (day, rows, state.rank(day, rows, market), exposure, votes)
    if latest is None: raise ValueError('指定日期没有本人因子数据')
    day, rows, ranked, exposure, votes = latest
    selected = ranked['ranked'][:spec.max_positions]
    weights = target_weights(spec, selected, rows)
    selections = [{'code': c, 'rank': i+1, 'score': ranked['scores'][c], 'weight': weights.get(c, 0)*exposure,
                   **{f.field: rows[c][f.field] for f in spec.factors}} for i, c in enumerate(selected)]
    return {'kind': 'screen', 'engine_version': ENGINE_VERSION, 'spec': spec.model_dump(mode='json'),
        'data_hash': digest.hexdigest(), 'metrics': {'selected_count': len(selected), 'pool_count': ranked['pool_count'],
            'valid_count': ranked['valid_count'], 'filtered_count': ranked['filtered_count']},
        'selections': selections, 'signals': [{'date': str(day), 'weights': ranked['weights'], 'next_exposure': exposure, **votes}],
        'diagnostics': {}, 'assumptions': ['仅输出指定日收盘后的筛选和目标比例，不下单、不修改模拟盘；与回测共享排序和权重逻辑。'],
        'disclaimer': DISCLAIMER}
