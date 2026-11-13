"""无数据库副作用的截面、动态权重、仓位和择时函数。"""
from __future__ import annotations

from collections import deque
from datetime import date
import math
from typing import Any
import numpy as np
import pandas as pd

from backtesting.contracts import BacktestSpec, DataSpec, Predicate, Timing


def finite(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def matches(row: dict, rule: Predicate) -> bool:
    value = finite(row.get(rule.field))
    if value is None:
        return False
    if rule.op == 'gt': return value > rule.value
    if rule.op == 'lt': return value < rule.value
    if rule.op == 'ge': return value >= rule.value
    if rule.op == 'le': return value <= rule.value
    if rule.op == 'eq': return value == rule.value
    return rule.value <= value <= rule.upper


def spearman(xs, ys) -> float | None:
    pairs = [(finite(x), finite(y)) for x, y in zip(xs, ys)]
    pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    a, b = (pd.Series(v, dtype=float).rank(method='average').to_numpy() for v in zip(*pairs))
    a, b = a - a.mean(), b - b.mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator > 1e-12 else None


def members(spec: DataSpec, day: date, codes) -> list[str]:
    allowed = set(spec.stock_codes) if spec.stock_codes else set(codes)
    if spec.pool_schedule:
        applicable = [s for s in spec.pool_schedule if s.as_of <= day]
        allowed &= set(applicable[-1].stock_codes) if applicable else set()
    return sorted(allowed & set(codes))


def adjusted_return(old: dict, new: dict) -> float | None:
    values = [finite(r.get(k)) for r, k in ((old, 'close'), (old, 'adj_factor'),
                                          (new, 'close'), (new, 'adj_factor'))]
    if any(v is None or v <= 0 for v in values):
        return None
    before, old_adj, after, new_adj = values
    return after * new_adj / (before * old_adj) - 1


class SignalState:
    """只把已兑现的一日收益计入 Rank IC；T 日决策永远看不到 T+1 收益。"""
    def __init__(self, spec: BacktestSpec):
        self.spec = spec
        self.previous: dict = {}
        self.previous_day: date | None = None
        self.ics = {f.field: deque(maxlen=spec.weighting.window) for f in spec.factors}
        self.regime = 'bull'
        self.missing_votes = 0

    def observe(self, day: date, rows: dict[str, dict]):
        if self.previous and self.spec.weighting.method == 'rank_ic':
            codes = members(self.spec, self.previous_day, self.previous)
            codes = [c for c in codes if c in rows]
            returns = [adjusted_return(self.previous[c], rows[c]) for c in codes]
            for factor in self.spec.factors:
                values = [finite(self.previous[c].get(factor.field)) for c in codes]
                signed = [v * factor.direction if v is not None else None for v in values]
                self.ics[factor.field].append(spearman(signed, returns))
        self.previous, self.previous_day = rows, day

    def weights(self) -> list[float]:
        if self.spec.weighting.method == 'fixed':
            raw = [f.weight for f in self.spec.factors]
        else:
            raw = []
            for factor in self.spec.factors:
                observed = [v for v in self.ics[factor.field] if v is not None]
                raw.append(max(float(np.mean(observed)), 0.) if observed else 0.)
        total = sum(raw)
        return [v / total for v in raw] if total > 1e-12 else [1 / len(raw)] * len(raw)

    def rank(self, day: date, rows: dict[str, dict], market: dict[str, dict] | None = None) -> dict:
        market = market or {}
        base = members(self.spec, day, rows)
        complete = [c for c in base if all(finite(rows[c].get(f.field)) is not None for f in self.spec.factors)]
        allowed = all(matches(market.get(r.index, {}), r) for r in self.spec.market_filters)
        codes = [c for c in complete if allowed and all(matches(rows[c], r) for r in self.spec.entry_filters)]
        weights = self.weights()
        scores = {c: 0. for c in codes}
        for factor, weight in zip(self.spec.factors, weights):
            if not codes: break
            values = pd.Series([float(rows[c][factor.field]) * factor.direction for c in codes], index=codes)
            for code, percentile in values.rank(method='average', pct=True).items():
                scores[code] += float(percentile) * weight
        ranked = sorted(codes, key=lambda c: (-scores[c], c))
        return {'ranked': ranked, 'scores': scores,
                'weights': {f.field: w for f, w in zip(self.spec.factors, weights)},
                'pool_count': len(base), 'valid_count': len(complete), 'filtered_count': len(codes),
                'market_allowed': allowed}

    def timing_step(self, market: dict[str, dict]) -> tuple[float, dict]:
        timing = self.spec.timing
        if not timing:
            return self.spec.exposure, {'regime': 'bull', 'bull': 0, 'bear': 0, 'abstain': 0}
        votes = {'bull': 0, 'bear': 0, 'abstain': 0}
        for rule in timing.rules:
            row = market.get(rule.index, {})
            if finite(row.get(rule.field)) is None:
                votes['abstain'] += 1
            else:
                votes['bull' if matches(row, rule) else 'bear'] += 1
        needed = lambda n: len(timing.rules) if n == -1 else n
        if self.regime == 'bull' and votes['bear'] >= needed(timing.exit_votes):
            self.regime = 'bear'
        elif self.regime == 'bear' and votes['bull'] >= needed(timing.enter_votes):
            self.regime = 'bull'
        self.missing_votes += votes['abstain']
        exposure = self.spec.exposure * (1 if self.regime == 'bull' else timing.bear_exposure)
        return exposure, {**votes, 'regime': self.regime}


def target_weights(spec: BacktestSpec, candidates: list[str], rows: dict[str, dict]) -> dict[str, float]:
    """结果为总仓位中的比例；未用完的名额/固定比例留现金，不误把每股预算当总预算。"""
    sizing = spec.sizing
    if sizing.method == 'factor':
        values = [max(finite(rows.get(c, {}).get(sizing.field)) or 0., 0.) for c in candidates]
        total = sum(values)
        return {c: v / total if total else 0. for c, v in zip(candidates, values)}
    if sizing.method == 'fixed':
        weights = list(sizing.proportions)
        total = sum(weights)
        if total > 1:
            weights = [v / total for v in weights]
        missing = spec.max_positions - len(weights)
        if missing:
            weights += [max(0., 1 - sum(weights)) / missing] * missing
    else:
        weights = [1 / spec.max_positions] * spec.max_positions
    return {c: weights[i] for i, c in enumerate(candidates)}
