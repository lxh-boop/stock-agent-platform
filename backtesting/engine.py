"""逐日研究回测：当日收盘信号、次日执行；定期和条件触发共用一个账户内核。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from typing import Callable, Iterable

import numpy as np
from backtesting.contracts import BacktestSpec, DISCLAIMER, ENGINE_VERSION, MAX_DAYS, MAX_TRADES, MAX_STOCKS
from backtesting.signals import SignalState, adjusted_return, finite, matches, members, target_weights


@dataclass
class Position:
    shares: float
    cost: float
    peak: float
    opened: int
    locked: float = 0
    cost_with_fees: float = 0


def rank_candidates(rows: dict[str, dict], spec: BacktestSpec) -> list[str]:
    return SignalState(spec).rank(spec.start_date, rows)['ranked']


def validate_rows(day: date, raw_rows: dict) -> dict:
    rows = {}
    for code in sorted(raw_rows):
        row = dict(raw_rows[code])
        for field in ('open', 'close', 'adj_factor', 'volume'):
            row[field] = finite(row.get(field))
            if row[field] is None or row[field] < 0 or (field != 'volume' and row[field] == 0):
                raise ValueError(f'{day} {code} 缺少有效 {field}，请先修复个人行情数据')
        # 仅因子/辅助字段允许缺值；哈希和 JSON 不接受 NaN 或 Infinity。
        for key, value in list(row.items()):
            if key != 'input_hash' and not isinstance(value, (str, type(None), bool)):
                row[key] = finite(value)
        rows[code] = row
    return rows


def calculate_metrics(equity: list[dict], initial_cash: float, trades: list[dict]) -> dict:
    values = np.asarray([initial_cash, *[p['equity'] for p in equity]], dtype=float)
    returns = values[1:] / values[:-1] - 1
    days = len(equity)
    exponent = math.log(values[-1] / initial_cash) * 252 / max(days, 1)
    if abs(exponent) > 700:
        raise ValueError('年化结果溢出，请检查行情复权口径或延长回测区间')
    std = float(np.std(returns, ddof=1)) if days > 1 else 0.
    sells = [t for t in trades if t['side'] == 'sell']
    return {
        'total_return': float(values[-1] / initial_cash - 1), 'annual_return': math.expm1(exponent),
        'max_drawdown': float(np.min(values / np.maximum.accumulate(values) - 1)),
        'volatility': std * math.sqrt(252),
        'sharpe': float(np.mean(returns) / std * math.sqrt(252)) if std > 1e-12 else None,
        'positive_day_ratio': float(np.mean(returns > 0)) if days else None,
        'sell_win_rate': sum(t.get('realized_pnl', 0) > 0 for t in sells) / len(sells) if sells else None,
        'total_fees': sum(t['fee'] for t in trades), 'trade_count': len(trades),
        'days': days, 'final_equity': float(values[-1]),
        'average_cash_ratio': float(np.mean([p['cash'] / p['equity'] for p in equity])) if days else None,
        'annual_turnover': sum(t['amount'] for t in trades) / 2 / float(np.mean(values)) * 252 / max(days, 1),
        'benchmark_return': equity[-1]['benchmark_nav'] - 1 if equity else None,
    }


def simulate(spec: BacktestSpec, days: Iterable[tuple[date, dict[str, dict]]], *,
             markets: dict | None = None, on_progress: Callable[[int], None] = lambda _: None,
             cancelled: Callable[[], bool] = lambda: False) -> dict:
    cash, book, last = spec.initial_cash, {}, {}
    pending_exits: dict[str, tuple[str, str]] = {}
    pending_target = None
    trades, equity, positions, skipped, signals = [], [], [], [], []
    digest, state = hashlib.sha256(), SignalState(spec)
    benchmark, target_exposure = 1., spec.exposure
    stale_valuations = adjustment_count = omitted_skips = 0
    previous_day = None
    markets = markets or {}
    previous_benchmark = None
    skipped_days = 0

    def record_skip(day, code, reason, signal):
        nonlocal omitted_skips
        if len(skipped) < MAX_TRADES:
            skipped.append({'date': str(day), 'code': code, 'reason': reason, 'signal_date': signal})
        else:
            omitted_skips += 1

    def execution_price(day, rows, code, side, signal):
        row = rows.get(code)
        if row is None or row['volume'] <= 0:
            record_skip(day, code, '无可交易行情，退出顺延' if side == 'sell' else '无可交易行情，跳过买入', signal)
            return None
        px = row[spec.execution] * (1 + (1 if side == 'buy' else -1) * spec.slippage_bps / 10000)
        if spec.limit_pct is not None:
            reference = finite(row.get('pre_close'))
            if reference is None:
                change = finite(row.get('pct_chg'))
                if change is not None and change > -100:
                    reference = row['close'] / (1 + change / 100)
            if reference is None and code in last:
                reference = last[code]['close']  # 已按复权事件换到当前价格尺度。
            if reference is None or reference <= 0:
                record_skip(day, code, '缺少涨跌幅参考价，拒绝成交', signal)
                return None
            blocked = px >= reference * (1 + spec.limit_pct) - 1e-8 if side == 'buy' else px <= reference * (1 - spec.limit_pct) + 1e-8
            if blocked:
                record_skip(day, code, '涨幅限制跳过买入' if side == 'buy' else '跌幅限制退出顺延', signal)
                return None
        return px

    def nav(rows, close=False):
        return cash + sum(p.shares * (rows.get(c, last[c])['close'] if close else
                         (rows.get(c, last[c])[spec.execution] if c in rows and rows[c]['volume'] > 0 else last[c]['close']))
                         for c, p in book.items())

    def sell(day, rows, code, qty, reason, signal):
        nonlocal cash
        pos = book[code]
        qty = min(qty, max(0., pos.shares - pos.locked))
        if qty <= 1e-8: return
        px = execution_price(day, rows, code, 'sell', signal)
        if px is None: return
        amount, fee = qty * px, qty * px * spec.sell_fee
        cash += amount - fee
        pnl = amount - fee - qty * pos.cost_with_fees
        pos.shares -= qty
        trades.append({'date': str(day), 'signal_date': signal, 'code': code, 'side': 'sell',
                       'price': px, 'shares': qty, 'amount': amount, 'fee': fee, 'reason': reason, 'realized_pnl': pnl})
        if pos.shares < 1e-8: del book[code]

    def buy(day, idx, rows, code, budget, reason, signal):
        nonlocal cash
        px = execution_price(day, rows, code, 'buy', signal)
        if px is None: return
        qty = math.floor(min(budget, cash) / (px * (1 + spec.buy_fee)) / 100) * 100
        if qty < 100 or qty * px < spec.min_position * nav(rows):
            record_skip(day, code, '资金不足一手或低于最小建仓比例', signal)
            return
        amount, fee = qty * px, qty * px * spec.buy_fee
        cash -= amount + fee
        if cash < -1e-7: raise RuntimeError('回测现金不能为负')
        cash = max(cash, 0.)
        if code in book:
            pos = book[code]
            total = pos.shares + qty
            pos.cost_with_fees = (pos.cost_with_fees * pos.shares + amount + fee) / total
            pos.cost = (pos.cost * pos.shares + amount) / total
            pos.shares, pos.locked = total, pos.locked + qty
        else:
            book[code] = Position(qty, px, px, idx, qty, px * (1 + spec.buy_fee))
            last.setdefault(code, dict(rows[code]))
        trades.append({'date': str(day), 'signal_date': signal, 'code': code, 'side': 'buy',
                       'price': px, 'shares': qty, 'amount': amount, 'fee': fee, 'reason': reason})

    def resize(day, idx, rows, exposure, signal, add):
        """总仓位风险管理不受不卖条件保护；所有成交仍统一校验。"""
        total = nav(rows)
        invested = total - cash
        desired = total * exposure
        if invested <= 0 or (abs(desired - invested) <= total * spec.position_band and exposure != 0): return
        original = {c: p.shares * (rows.get(c, last[c])[spec.execution] if c in rows else last[c]['close']) for c, p in book.items()}
        for code, mv in original.items():
            px = rows.get(code, last[code])[spec.execution]
            delta = (desired - invested) * mv / invested
            if delta < 0:
                qty = book[code].shares if exposure == 0 else math.floor(-delta / px / 100) * 100
                sell(day, rows, code, qty, '择时减仓' if spec.timing else '仓位偏离减持', signal)
            elif add and code not in pending_exits:
                room = max(0., nav(rows) * exposure - (nav(rows) - cash))
                buy(day, idx, rows, code, min(delta, room), '择时加仓' if spec.timing else '仓位偏离补仓', signal)

    for day, raw_rows in days:
        if cancelled(): raise InterruptedError('回测已取消')
        if previous_day is not None and day <= previous_day: raise ValueError('回测交易日必须严格递增')
        if day > spec.end_date or not raw_rows: raise ValueError('回测日或行情截面无效')
        valuation_day, previous_day = previous_day, day
        rows, market = validate_rows(day, raw_rows), markets.get(day, {})
        digest.update(json.dumps([str(day), rows, market], sort_keys=True, ensure_ascii=False,
                                 allow_nan=False, separators=(',', ':')).encode())
        state.observe(day, rows)
        if day < spec.start_date:
            last = rows
            target_exposure, _ = state.timing_step(market)
            continue
        idx = len(equity)
        if idx >= MAX_DAYS: raise ValueError(f'最多回测 {MAX_DAYS} 个交易日')
        for pos in book.values(): pos.locked = 0.
        daily_returns = []
        # 每日只给上一个结算点已知的股票分配基准，不用未来的上市名单回填过去。
        previous_members = members(spec, valuation_day or day, last)
        for code, old in list(last.items()):
            row = rows.get(code)
            if code in previous_members:
                value = adjusted_return(old, row) if row else None
                daily_returns.append(value if value is not None else 0.)
            if row is None: continue
            ratio = row['adj_factor'] / old['adj_factor']
            if abs(ratio - 1) > 1e-10:
                if code in book:
                    pos = book[code]
                    pos.shares *= ratio
                    pos.cost /= ratio
                    pos.cost_with_fees /= ratio
                    pos.peak /= ratio
                    adjustment_count += 1
                last[code] = {**old, 'close': old['close'] / ratio, 'adj_factor': row['adj_factor']}
        if spec.benchmark != 'pool':
            value = finite(market.get(spec.benchmark, {}).get('close'))
            if value is None or value <= 0: raise ValueError(f'{day} 缺少真实基准行情，不能用零收益代替')
            if previous_benchmark is not None: benchmark *= value / previous_benchmark
            previous_benchmark = value
        elif idx and daily_returns:
            benchmark *= 1 + sum(daily_returns) / len(daily_returns)

        paused = any(r.start <= day <= r.end for r in spec.exclude_ranges)
        if paused:
            pending_target, pending_exits = None, {}
            skipped_days += 1
        else:
            for code, (reason, signal) in list(pending_exits.items()):
                if code in book: sell(day, rows, code, book[code].shares, reason, signal)
                if code not in book: pending_exits.pop(code, None)
            # 择时降仓优先，即使有不卖条件也不能阻挡组合风险控制。
            if spec.timing or spec.rebalance_days == 0:
                resize(day, idx, rows, target_exposure, str(equity[-1]['date']) if equity else str(day), spec.rebalance_days == 0)
            if pending_target is not None:
                signal, weights, protected = pending_target
                target = set(weights)
                total = nav(rows)
                # 一轮先完成所有减持，再买入，避免顺序导致现金不足。
                for code, pos in list(book.items()):
                    if code in protected or code in pending_exits: continue
                    if spec.rebalance_days > 0:
                        wanted = total * target_exposure * weights.get(code, 0.)
                        px = rows.get(code, last[code])[spec.execution]
                        if code not in target or wanted <= 0:
                            pending_exits[code] = ('调仓退出', signal)
                            sell(day, rows, code, pos.shares, '调仓退出', signal)
                            if code not in book: pending_exits.pop(code, None)
                        else:
                            qty = math.floor(max(0., pos.shares - wanted / px) / 100) * 100
                            sell(day, rows, code, qty, '调仓减持', signal)
                for code, proportion in weights.items():
                    if code in pending_exits or code in protected or proportion <= 0: continue
                    if code not in book and len(book) >= spec.max_positions:
                        record_skip(day, code, '持仓名额已满', signal); continue
                    if spec.rebalance_days == 0 and code in book: continue
                    if code not in rows:
                        record_skip(day, code, '无可交易行情，跳过买入', signal); continue
                    current = book[code].shares * rows[code][spec.execution] if code in book else 0.
                    total_now = nav(rows)
                    room = max(0., total_now * target_exposure - (total_now - cash))
                    gap = min(max(0., total * target_exposure * proportion - current), room)
                    if gap > 0:
                        buy(day, idx, rows, code, gap, '调仓买入' if spec.rebalance_days else '条件买入', signal)
                pending_target = None
            elif spec.keep_invested or spec.timing or spec.rebalance_days == 0:
                resize(day, idx, rows, target_exposure, str(equity[-1]['date']) if equity else str(day), True)

        last.update(rows)
        total = nav(rows, close=True)
        if not math.isfinite(total) or total <= 0: raise ValueError('回测权益无效')
        for code, pos in book.items():
            px, stale = last[code]['close'], code not in rows
            stale_valuations += int(stale)
            pos.peak = max(pos.peak, px)
            positions.append({'date': str(day), 'code': code, 'shares': pos.shares, 'cost': pos.cost,
                              'close': px, 'market_value': px * pos.shares, 'weight': px * pos.shares / total,
                              'sellable_shares': max(0., pos.shares - pos.locked), 'stale': stale})
        equity.append({'date': str(day), 'nav': total / spec.initial_cash, 'equity': total, 'cash': cash,
                       'exposure': 1 - cash / total, 'target_exposure': target_exposure, 'benchmark_nav': benchmark})
        if len(trades) > MAX_TRADES: raise ValueError(f'交易笔数超过 {MAX_TRADES}，请缩小区间或减少调仓')

        if not paused:
            ranking = state.rank(day, rows, market)
            ranked = ranking['ranked']
            ranks = {code: i + 1 for i, code in enumerate(ranked)}
            protected = {c for c in book if c in rows and any(matches(rows[c], r) for r in spec.keep_rules)}
            for code in protected: pending_exits.pop(code, None)
            for code, pos in book.items():
                if code in protected or code not in rows: continue
                close, reason = rows[code]['close'], None
                if spec.stop_loss is not None and close / pos.cost - 1 <= -spec.stop_loss: reason = '止损'
                elif spec.take_profit is not None and close / pos.cost - 1 >= spec.take_profit: reason = '止盈'
                elif spec.trailing_stop is not None and close / pos.peak - 1 <= -spec.trailing_stop: reason = '持仓回撤退出'
                elif spec.max_hold_days is not None and idx - pos.opened + 1 >= spec.max_hold_days: reason = '持有期限退出'
                elif any(matches(rows[code], r) for r in spec.sell_filters): reason = '字段卖出条件'
                elif spec.rank_out and ranking['valid_count'] and ranks.get(code, MAX_STOCKS + 1) > spec.rank_out: reason = '排名跌出'
                if reason: pending_exits[code] = (reason, str(day))
            if spec.rebalance_days == 0 or idx % spec.rebalance_days == 0:
                if ranking['valid_count']:
                    available = max(0, spec.max_positions - len(protected))
                    candidates = [c for c in ranked if c not in protected and c not in pending_exits][:available]
                    weighted_codes = [*sorted(protected), *candidates]
                    pending_target = (str(day), target_weights(spec, weighted_codes, rows), protected)
                else:
                    record_skip(day, '', '截面排序因子全部缺失，不将数据缺口解释为清仓信号', str(day))
            next_exposure, votes = state.timing_step(market)
            signals.append({'date': str(day), 'pool_count': ranking['pool_count'], 'valid_count': ranking['valid_count'],
                            'filtered_count': ranking['filtered_count'], 'weights': ranking['weights'],
                            'candidates': ranked[:spec.max_positions], 'next_exposure': next_exposure, **votes})
            target_exposure = next_exposure
        if idx % 10 == 0: on_progress(idx + 1)

    if len(equity) < 2: raise ValueError('至少需要两个有数据的交易日')
    if cancelled(): raise InterruptedError('回测已取消')
    metrics = calculate_metrics(equity, spec.initial_cash, trades)
    # 每月区间汇总含完整交易统计，不截断后50笔。暂停期间仍按真实收盘估值。
    periods, previous = [], {'nav': 1., 'benchmark_nav': 1.}
    for month in dict.fromkeys(r['date'][:7] for r in equity):
        rows = [r for r in equity if r['date'].startswith(month)]
        fills = [t for t in trades if t['date'].startswith(month)]
        periods.append({'period': month, 'start': rows[0]['date'], 'end': rows[-1]['date'],
                        'return': rows[-1]['nav'] / previous['nav'] - 1,
                        'benchmark_return': rows[-1]['benchmark_nav'] / previous['benchmark_nav'] - 1,
                        'trade_count': len(fills), 'fees': sum(t['fee'] for t in fills),
                        'mean_exposure': float(np.mean([r['exposure'] for r in rows]))})
        previous = rows[-1]
    return {'kind': 'backtest', 'engine_version': ENGINE_VERSION, 'spec': spec.model_dump(mode='json'),
            'data_hash': digest.hexdigest(), 'metrics': metrics, 'equity': equity, 'trades': trades,
            'positions': positions, 'skipped_orders': skipped, 'signals': signals, 'periods': periods,
            'diagnostics': {'stale_valuations': stale_valuations, 'adjustment_events': adjustment_count,
                            'unfilled_exits': len(pending_exits), 'omitted_skip_rows': omitted_skips,
                            'paused_days': skipped_days, 'missing_timing_votes': state.missing_votes,
                            'bear_days': sum(s['regime'] == 'bear' for s in signals)},
            'assumptions': [
                'T日收盘信号在下一有数据的交易日执行；末日信号不成交，期末不强制平仓。',
                '原始OHLC成交；复权变化按等效股数再投资近似，不是实际分红送转到账；含费成本计算卖出笔盈亏。',
                '新增买入按100股整手，只有当日新增数量不可卖；账户不借款、不做空。',
                '缺个股行情不推断停牌：不交易并以前次收盘估值；报告标记stale；完整交易所日历缺口另需数据核验。',
                'limit_pct为可选固定比例涨跌幅近似，所有交易统一检查；未模拟按板块/ST/上市日差异、冲击和退市清算。',
                '动态IC只使用已兑现的一日收益；无有效正IC时回退等权并记录实际权重。',
                '择时减仓优先于不卖条件；暂停区间只暂停交易，不冻结净值、不补做跳过的调仓。',
                '同池基准为研究曲线（非交易基金）；选择指数时必须有真实个人指数数据，缺失报错。',
                '历史成分仅使用显式提供的as_of成员快照；默认股票池来自个人库，可能有幸存者偏差。',
            ], 'disclaimer': DISCLAIMER}
