"""Bounded, past-only factor expressions. User text is interpreted, never executed."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import math
import operator
from typing import Any

import numpy as np
import pandas as pd

from alpha158 import days_since_max, days_since_min, rolling_rank_last, rolling_resi, rolling_rsqr, rolling_slope
from config import ALPHA_WINDOWS, EPS
from core.alpha158_contract import FEATURE_COLUMNS


FIELDS = ("open", "high", "low", "close", "volume", "vwap")
MAX_LENGTH = 2000
MAX_NODES = 256
MAX_DEPTH = 32
MAX_WINDOW = 252
MAX_LOOKBACK = 504
MAX_ROWS = 6000
MAX_WORK = 4_000_000
MAX_LITERAL = 1e12

# A signature describes numeric-series operands, followed by literal parameters.
_ROLLING = {"mean", "std", "sum", "ts_min", "ts_max", "slope", "rsqr", "resi", "rank", "days_since_max", "days_since_min", "argmax", "argmin"}
_HEAVY = {"slope", "rsqr", "resi", "rank", "days_since_max", "days_since_min", "argmax", "argmin", "quantile"}
_UNARY = {"abs", "log", "sqrt", "sign"}
_BINARY = {"safe_div", "maximum", "minimum"}
_SHIFT = {"delay", "shift", "delta"}
_FUNCTIONS = _ROLLING | _UNARY | _BINARY | _SHIFT | {"corr", "quantile"}
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: np.divide, ast.Pow: np.power}
_COMPARE = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt, ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}


@dataclass(frozen=True)
class _Expression:
    tree: ast.Expression
    normalized: str
    fields: tuple[str, ...]
    lookback: int
    work: int


def _literal(node: ast.AST) -> float:
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if not isinstance(node, ast.Constant) or type(node.value) not in (int, float):
        raise ValueError("窗口、分位数和指数参数必须是数值常量")
    # Check large integer literals before converting to float (which can overflow).
    if abs(node.value) > MAX_LITERAL or not math.isfinite(node.value):
        raise ValueError("数值常量超出允许范围")
    return sign * float(node.value)


def _window(node: ast.AST) -> int:
    value = _literal(node)
    if not value.is_integer() or not 1 <= value <= MAX_WINDOW:
        raise ValueError("窗口和滞后期必须是 1 到 252 的整数，禁止引用未来数据")
    return int(value)


def _parse(expression: str) -> _Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("请填写一个因子表达式")
    if len(expression) > MAX_LENGTH:
        raise ValueError("表达式过长，最多允许 2000 个字符")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError("表达式语法不正确，请使用单个表达式") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError("表达式过于复杂，请减少运算和函数数量")
    fields: set[str] = set()

    def visit(node: ast.AST, depth: int = 0) -> tuple[int, int]:
        if depth > MAX_DEPTH:
            raise ValueError("表达式嵌套过深")
        if isinstance(node, ast.Name):
            if node.id not in FIELDS:
                raise ValueError("表达式只能引用允许的行情字段和函数")
            fields.add(node.id)
            return 0, 1
        if isinstance(node, ast.Constant):
            _literal(node)
            return 0, 1
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            lag, work = visit(node.operand, depth + 1)
            return lag, work + 1
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            if isinstance(node.op, ast.Pow) and abs(_literal(node.right)) > 8:
                raise ValueError("幂指数必须是 -8 到 8 的数值常量")
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            return max(left[0], right[0]), left[1] + right[1] + 1
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _COMPARE:
            left, right = visit(node.left, depth + 1), visit(node.comparators[0], depth + 1)
            return max(left[0], right[0]), left[1] + right[1] + 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in _FUNCTIONS or node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
                raise ValueError("仅允许白名单函数和位置参数")
            count = 1 if name in _UNARY else 3 if name in {"corr", "quantile"} else 2
            if len(node.args) != count:
                raise ValueError("函数参数数量不正确，请参考表达式模板")
            operands = 2 if name in _BINARY or name == "corr" else 1
            parts = [visit(arg, depth + 1) for arg in node.args[:operands]]
            lag = max(part[0] for part in parts)
            work = sum(part[1] for part in parts) + 1
            if name in _SHIFT | _ROLLING | {"corr", "quantile"}:
                window = _window(node.args[2] if name == "corr" else node.args[1])
                lag += window if name in _SHIFT else window - 1
                work += window if name in _HEAVY else 1
            if name == "quantile" and not 0 <= _literal(node.args[2]) <= 1:
                raise ValueError("分位数必须在 0 到 1 之间")
            if lag > MAX_LOOKBACK:
                raise ValueError("累计回看范围不能超过 504 个交易日")
            return lag, work
        raise ValueError("表达式包含不允许的语法，禁止属性访问、索引、赋值和代码执行")

    lookback, work = visit(tree.body)
    if not fields:
        raise ValueError("表达式至少需要引用一个行情字段")
    return _Expression(tree, ast.unparse(tree.body), tuple(field for field in FIELDS if field in fields), lookback, work)


def validate_expression(expression: str) -> dict[str, Any]:
    parsed = _parse(expression)
    return {"expression": parsed.normalized, "fields": list(parsed.fields), "lookback_rows": parsed.lookback}


def evaluate_expression(expression: str, frame: pd.DataFrame) -> pd.Series:
    """Evaluate one stock in ascending date order without changing its input data.

    lookback_rows is the number of preceding rows needed by the expression.
    Rolling functions require their entire window; unavailable/invalid results
    are NaN. They are never filled with future observations or arbitrary zeros.
    """
    parsed = _parse(expression)
    if not isinstance(frame, pd.DataFrame) or not frame.columns.is_unique:
        raise ValueError("预览行情必须是字段不重复的数据表")
    if len(frame) > MAX_ROWS or len(frame) * parsed.work > MAX_WORK:
        raise ValueError("预览计算量超出限制，请缩短表达式或减少行情行数")
    if any(field not in frame for field in parsed.fields):
        raise ValueError("预览行情缺少表达式需要的字段")
    if not frame.index.is_unique or ("code" in frame and frame["code"].nunique(dropna=False) > 1):
        raise ValueError("每次预览只能包含单只股票且行索引不能重复")
    if "date" in frame:
        dates = pd.to_datetime(frame["date"], errors="coerce")
        if dates.isna().any() or not dates.is_monotonic_increasing or dates.duplicated().any():
            raise ValueError("预览行情日期必须有效、不重复并按时间升序排列")
    try:
        values = {field: pd.to_numeric(frame[field], errors="coerce").astype("float64") for field in parsed.fields}
    except (ValueError, TypeError, OverflowError):
        raise ValueError("预览行情必须包含可转换为数值的字段") from None

    def series(value: Any) -> pd.Series:
        return value if isinstance(value, pd.Series) else pd.Series(value, index=frame.index, dtype="float64")

    def calculate(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.UnaryOp):
            result = calculate(node.operand)
            return -result if isinstance(node.op, ast.USub) else result
        if isinstance(node, ast.BinOp):
            return _OPS[type(node.op)](calculate(node.left), calculate(node.right))
        if isinstance(node, ast.Compare):
            return _COMPARE[type(node.ops[0])](calculate(node.left), calculate(node.comparators[0]))
        name = node.func.id
        args = [calculate(arg) for arg in node.args]
        value = args[0]
        if name in _UNARY:
            return {"abs": np.abs, "log": np.log, "sqrt": np.sqrt, "sign": np.sign}[name](value)
        if name in _BINARY:
            if name == "safe_div":
                return np.divide(value, args[1] + EPS)
            return (np.maximum if name == "maximum" else np.minimum)(value, args[1])
        data = series(value)
        if name in _SHIFT:
            return data.diff(int(args[1])) if name == "delta" else data.shift(int(args[1]))
        if name == "corr":
            return data.rolling(int(args[2])).corr(series(args[1]))
        rolling = data.rolling(int(args[1]))
        if name == "quantile":
            return rolling.quantile(float(args[2]))
        methods = {"mean": "mean", "std": "std", "sum": "sum", "ts_min": "min", "ts_max": "max"}
        if name in methods:
            return getattr(rolling, methods[name])()
        functions = {"slope": rolling_slope, "rsqr": rolling_rsqr, "resi": rolling_resi,
                     "rank": rolling_rank_last, "days_since_max": days_since_max, "argmax": days_since_max,
                     "days_since_min": days_since_min, "argmin": days_since_min}
        return rolling.apply(functions[name], raw=True)

    try:
        with np.errstate(divide="ignore", invalid="ignore", over="ignore", under="ignore"):
            result = series(calculate(parsed.tree.body)).astype("float64")
        return result.replace([np.inf, -np.inf], np.nan).rename("factor_value")
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        raise ValueError("表达式无法在给定行情上计算，请检查函数参数和数值范围") from None


def expression_templates() -> list[dict[str, str]]:
    return [
        {"name": "5日收益率", "expression": "close / delay(close, 5) - 1", "description": "当前收盘价相对 5 个交易日前的涨跌幅"},
        {"name": "20日均线偏离", "expression": "safe_div(close, mean(close, 20)) - 1", "description": "收盘价相对 20 日平均收盘价的偏离"},
        {"name": "20日波动率", "expression": "std(close / delay(close, 1) - 1, 20)", "description": "最近 20 个交易日收益率的样本标准差"},
        {"name": "5日量比", "expression": "safe_div(volume, mean(volume, 5))", "description": "成交量相对最近 5 日均量的比值"},
        {"name": "20日价量相关", "expression": "corr(close, log(volume + 1), 20)", "description": "最近 20 日价格与对数成交量的相关系数"},
    ]


def expression_syntax() -> dict[str, Any]:
    functions = [
        ("delay(x, n)", "向过去取 n 个交易日前的值；shift 为同义函数"),
        ("delta(x, n)", "当前值减去 n 个交易日前的值"),
        ("mean(x, n)", "最近 n 日均值"), ("std(x, n)", "最近 n 日样本标准差（ddof=1）"),
        ("sum(x, n)", "最近 n 日总和"), ("ts_min(x, n)", "最近 n 日最小值"), ("ts_max(x, n)", "最近 n 日最大值"),
        ("corr(x, y, n)", "最近 n 日 Pearson 相关系数"), ("quantile(x, n, q)", "最近 n 日 q 分位数，q 在 0 到 1 之间"),
        ("slope(x, n)", "最近 n 日相对时间序号的线性回归斜率"), ("rsqr(x, n)", "最近 n 日线性回归决定系数"),
        ("resi(x, n)", "最近 n 日回归在当前日的残差"), ("rank(x, n)", "窗口中小于等于当前值的观测占比"),
        ("days_since_max(x, n)", "距窗口首个最大值的交易日数；argmax 为同义函数"),
        ("days_since_min(x, n)", "距窗口首个最小值的交易日数；argmin 为同义函数"),
        ("safe_div(x, y)", "x / (y + 1e-12)，与 Alpha158 保持一致"),
        ("maximum(x, y)", "逐行取较大值"), ("minimum(x, y)", "逐行取较小值"),
        ("abs(x)", "绝对值"), ("log(x)", "自然对数"), ("sqrt(x)", "平方根"), ("sign(x)", "符号：-1、0 或 1"),
    ]
    return {"fields": list(FIELDS), "functions": [{"signature": signature, "description": description} for signature, description in functions],
            "operators": ["+", "-", "*", "/", "**", ">", ">=", "<", "<=", "==", "!="],
            "max_window": MAX_WINDOW, "max_length": MAX_LENGTH, "max_lookback_rows": MAX_LOOKBACK,
            "max_nodes": MAX_NODES, "max_depth": MAX_DEPTH, "max_preview_rows": MAX_ROWS,
            "lookback_definition": "lookback_rows 表示当前行之前需要的历史行数；窗口不足或数值无效时结果为空"}


def alpha158_definitions() -> list[dict[str, Any]]:
    """The exact 13 + 29 * 5 past-only features in core.alpha158_contract."""
    definitions: dict[str, tuple[str, str, str]] = {
        "KMID": ("safe_div(close - open, open)", "candlestick", "实体相对开盘价"),
        "KLEN": ("safe_div(high - low, open)", "candlestick", "振幅相对开盘价"),
        "KMID2": ("safe_div(close - open, high - low)", "candlestick", "实体占振幅比例"),
        "KUP": ("safe_div(high - maximum(open, close), open)", "candlestick", "上影线相对开盘价"),
        "KUP2": ("safe_div(high - maximum(open, close), high - low)", "candlestick", "上影线占振幅比例"),
        "KLOW": ("safe_div(minimum(open, close) - low, open)", "candlestick", "下影线相对开盘价"),
        "KLOW2": ("safe_div(minimum(open, close) - low, high - low)", "candlestick", "下影线占振幅比例"),
        "KSFT": ("safe_div(2 * close - high - low, open)", "candlestick", "收盘位置相对开盘价"),
        "KSFT2": ("safe_div(2 * close - high - low, high - low)", "candlestick", "收盘位置相对振幅"),
        "OPEN0": ("safe_div(open, close)", "price", "开盘价相对收盘价"),
        "HIGH0": ("safe_div(high, close)", "price", "最高价相对收盘价"),
        "LOW0": ("safe_div(low, close)", "price", "最低价相对收盘价"),
        "VWAP0": ("safe_div(vwap, close)", "price", "成交均价相对收盘价"),
    }
    for window in ALPHA_WINDOWS:
        n = str(window)
        up = f"mean(close > delay(close, 1), {n})"
        down = f"mean(close < delay(close, 1), {n})"
        high = f"ts_max(high, {n})"
        low = f"ts_min(low, {n})"
        price_up = f"sum(maximum(delta(close, 1), 0), {n})"
        price_down = f"sum(maximum(-delta(close, 1), 0), {n})"
        price_abs = f"sum(abs(delta(close, 1)), {n})"
        vol_up = f"sum(maximum(delta(volume, 1), 0), {n})"
        vol_down = f"sum(maximum(-delta(volume, 1), 0), {n})"
        vol_abs = f"sum(abs(delta(volume, 1)), {n})"
        weighted = "abs(close / delay(close, 1) - 1) * volume"
        rolling = {
            "ROC": (f"safe_div(delay(close, {n}), close)", "price", "历史收盘价相对当前价"),
            "MA": (f"safe_div(mean(close, {n}), close)", "price", "均价相对当前价"),
            "STD": (f"safe_div(std(close, {n}), close)", "price", "价格标准差相对当前价"),
            "BETA": (f"safe_div(slope(close, {n}), close)", "trend", "回归斜率相对当前价"),
            "RSQR": (f"rsqr(close, {n})", "trend", "价格线性回归决定系数"),
            "RESI": (f"safe_div(resi(close, {n}), close)", "trend", "回归残差相对当前价"),
            "MAX": (f"safe_div({high}, close)", "price", "窗口最高价相对当前价"),
            "MIN": (f"safe_div({low}, close)", "price", "窗口最低价相对当前价"),
            "QTLU": (f"safe_div(quantile(close, {n}, 0.8), close)", "price", "收盘价上分位数相对当前价"),
            "QTLD": (f"safe_div(quantile(close, {n}, 0.2), close)", "price", "收盘价下分位数相对当前价"),
            "RANK": (f"rank(close, {n})", "price", "当前收盘价在窗口中的分位"),
            "RSV": (f"safe_div(close - {low}, {high} - {low})", "price", "当前收盘价的窗口相对位置"),
            "IMAX": (f"days_since_max(high, {n}) / {n}", "trend", "距最高价天数占窗口比例"),
            "IMIN": (f"days_since_min(low, {n}) / {n}", "trend", "距最低价天数占窗口比例"),
            "IMXD": (f"days_since_max(high, {n}) / {n} - days_since_min(low, {n}) / {n}", "trend", "高低点距离差占窗口比例"),
            "CORR": (f"corr(close, log(volume + 1), {n})", "correlation", "价格与对数成交量相关性"),
            "CORD": (f"corr(safe_div(close, delay(close, 1)), log(safe_div(volume, delay(volume, 1)) + 1), {n})", "correlation", "价量变化比率相关性"),
            "CNTP": (up, "trend", "上涨交易日占比"),
            "CNTN": (down, "trend", "下跌交易日占比"),
            "CNTD": (f"{up} - {down}", "trend", "上涨与下跌交易日占比之差"),
            "SUMP": (f"safe_div({price_up}, {price_abs})", "trend", "上涨价格变化占总变化比例"),
            "SUMN": (f"safe_div({price_down}, {price_abs})", "trend", "下跌价格变化占总变化比例"),
            "SUMD": (f"safe_div({price_up} - {price_down}, {price_abs})", "trend", "价格净变化占总变化比例"),
            "VMA": (f"safe_div(mean(volume, {n}), volume)", "volume", "均量相对当前成交量"),
            "VSTD": (f"safe_div(std(volume, {n}), volume)", "volume", "成交量标准差相对当前量"),
            "WVMA": (f"safe_div(std({weighted}, {n}), mean({weighted}, {n}))", "volume", "成交量加权绝对收益的变异系数"),
            "VSUMP": (f"safe_div({vol_up}, {vol_abs})", "volume", "成交量正变化占总变化比例"),
            "VSUMN": (f"safe_div({vol_down}, {vol_abs})", "volume", "成交量负变化占总变化比例"),
            "VSUMD": (f"safe_div({vol_up} - {vol_down}, {vol_abs})", "volume", "成交量净变化占总变化比例"),
        }
        definitions.update({f"{prefix}{window}": (expression, category, f"{window} 日{description}")
                            for prefix, (expression, category, description) in rolling.items()})
    if set(definitions) != set(FEATURE_COLUMNS) or len(definitions) != 158:
        raise RuntimeError("Alpha158 定义与已有特征契约不一致")
    result = []
    for name in FEATURE_COLUMNS:
        expression, category, description = definitions[name]
        validated = validate_expression(expression)
        result.append({"name": name, **validated, "category": category, "description": description})
    return result


__all__ = ["alpha158_definitions", "validate_expression", "evaluate_expression", "expression_templates", "expression_syntax"]
