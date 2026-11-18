from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from alpha158 import add_alpha158_for_one_stock
from core.alpha158_contract import FEATURE_COLUMNS
from core.factor_expressions import (
    MAX_LENGTH, MAX_ROWS, alpha158_definitions, evaluate_expression,
    expression_syntax, expression_templates, validate_expression,
)


def market_frame(rows=95, *, flat=False):
    rng = np.random.default_rng(12)
    close = np.full(rows, 12.0) if flat else 12 + rng.normal(0, .15, rows).cumsum()
    volume = np.full(rows, 800.0) if flat else rng.integers(100, 2000, rows).astype(float)
    return pd.DataFrame({
        "date": pd.bdate_range("2025-01-01", periods=rows), "code": "000001",
        "open": close * .995, "high": close * 1.015, "low": close * .985,
        "close": close, "volume": volume, "vwap": close * 1.001,
    }, index=pd.Index(range(100, 100 + rows), name="row"))


def test_user_expression_retains_index_and_matches_independent_return_calculation():
    frame = market_frame()
    original = frame.copy(deep=True)
    assert validate_expression("  close / delay(close, 5)-1 ") == {
        "expression": "close / delay(close, 5) - 1", "fields": ["close"], "lookback_rows": 5,
    }
    result = evaluate_expression("close / delay(close, 5) - 1", frame)
    pd.testing.assert_series_equal(result, (frame.close / frame.close.shift(5) - 1).rename("factor_value"))
    pd.testing.assert_frame_equal(frame, original)
    assert result.iloc[:5].isna().all()


@pytest.mark.parametrize("flat", [False, True])
def test_all_158_definitions_match_the_existing_alpha158_implementation(flat):
    definitions = alpha158_definitions()
    assert [item["name"] for item in definitions] == list(FEATURE_COLUMNS)
    assert len({item["name"] for item in definitions}) == 158
    assert not any("future" in item["expression"] for item in definitions)
    frame = market_frame(flat=flat)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", pd.errors.PerformanceWarning)
        expected = add_alpha158_for_one_stock(frame).replace([np.inf, -np.inf], np.nan)
    for item in definitions:
        result = evaluate_expression(item["expression"], frame)
        np.testing.assert_allclose(result, expected[item["name"]], rtol=1e-10, atol=1e-10, equal_nan=True,
                                   err_msg=item["name"])
        # Metadata must supply sufficient preceding observations for a preview.
        recent = frame.iloc[-(item["lookback_rows"] + 1):]
        np.testing.assert_allclose(evaluate_expression(item["expression"], recent).iloc[-1], result.iloc[-1],
                                   rtol=1e-8, atol=1e-8, equal_nan=True, err_msg=item["name"])


def test_no_future_observation_changes_an_existing_value():
    expression = "corr(close, log(volume + 1), 20) + std(delta(close, 1), 10)"
    frame = market_frame()
    prefix = frame.iloc[:55]
    full = evaluate_expression(expression, frame)
    pd.testing.assert_series_equal(evaluate_expression(expression, prefix), full.iloc[:55])
    changed = frame.copy()
    changed.loc[changed.index[55:], ["close", "volume"]] *= 100
    pd.testing.assert_series_equal(evaluate_expression(expression, changed).iloc[:55], full.iloc[:55])
    assert validate_expression("mean(delay(close, 5), 20)")["lookback_rows"] == 24


@pytest.mark.parametrize("expression", [
    "__import__('os').system('PRIVATE_MARKER')", "open('PRIVATE_MARKER').read()",
    "close.__class__", "close.iloc[-1]", "close['PRIVATE_MARKER']", "(lambda: close)()",
    "[x for x in close]", "(x for x in close)", "{x: x for x in close}", "(x := close)",
    "close; close", "import os", "getattr(close, 'PRIVATE_MARKER')", "np.log(close)",
    "mean(close, n=5)", "mean(*close)", "mean(close, **{})", "close if close > 1 else volume",
    "close and volume", "close > 1 > 0", "future_5d_ret", "mean(close, close)",
    "delay(close, -1)", "shift(close, -1)", "delta(close, -5)", "delay(close, 0)",
    "mean(close, 253)", "mean(close, 2.5)", "mean(close, True)", "quantile(close, 5, 2)",
    "close ** close", "close ** (2 ** 8)", "close ** 9", "close ** -9", "close + 1e309",
    "close + 1000000000001", "close + 'PRIVATE_MARKER'", "close + None", "1", "True",
    "delay(delay(delay(close, 252), 252), 1)",
])
def test_unsafe_or_unbounded_inputs_fail_closed_with_fixed_messages(expression):
    with pytest.raises(ValueError) as raised:
        validate_expression(expression)
    assert "PRIVATE_MARKER" not in str(raised.value)


@pytest.mark.parametrize("expression", [
    "close + " + "9" * 1000,
    "mean(close, " + "9" * 500 + ")",
    "close ** " + "9" * 500,
])
def test_large_integer_literals_use_a_fixed_validation_error(expression):
    with pytest.raises(ValueError) as raised:
        validate_expression(expression)
    assert str(raised.value) == "数值常量超出允许范围"


def test_expression_size_depth_and_work_are_bounded():
    with pytest.raises(ValueError, match="过长"):
        validate_expression("close" + " " * MAX_LENGTH)
    with pytest.raises(ValueError, match="复杂"):
        validate_expression(" + ".join(["close"] * 90))
    with pytest.raises(ValueError, match="嵌套"):
        validate_expression("abs(" * 34 + "close" + ")" * 34)
    with pytest.raises(ValueError, match="计算量"):
        evaluate_expression("close", market_frame(MAX_ROWS + 1))
    # Each rolling regression is individually allowed, but their combined work is not.
    expression = "slope(close, 252) + slope(high, 252) + slope(low, 252)"
    with pytest.raises(ValueError, match="计算量"):
        evaluate_expression(expression, market_frame(MAX_ROWS))


def test_invalid_domain_and_zero_denominators_become_missing_not_infinity():
    frame = market_frame(10)
    result = evaluate_expression("close / (volume - volume)", frame)
    assert result.isna().all()
    assert evaluate_expression("log(-close)", frame).isna().all()
    assert evaluate_expression("sqrt(-close)", frame).isna().all()
    np.testing.assert_allclose(evaluate_expression("safe_div(close, 0)", frame), frame.close / 1e-12)


def test_window_conventions_and_aliases_are_explicit():
    frame = pd.DataFrame({"close": [1., 2., 3., 3., 2.]})
    assert evaluate_expression("rank(close, 5)", frame).iloc[-1] == 3 / 5
    assert evaluate_expression("days_since_max(close, 5)", frame).iloc[-1] == 2
    assert evaluate_expression("argmax(close, 5)", frame).iloc[-1] == 2
    assert evaluate_expression("argmin(close, 5)", frame).iloc[-1] == 4
    assert evaluate_expression("quantile(close, 5, 0.5)", frame).iloc[-1] == 2
    pd.testing.assert_series_equal(evaluate_expression("delay(close, 1)", frame), evaluate_expression("shift(close, 1)", frame))
    np.testing.assert_allclose(evaluate_expression("std(close, 5)", frame).iloc[-1], frame.close.std(ddof=1))


def test_malformed_market_frames_do_not_mix_stocks_or_reorder_observations():
    frame = market_frame(10)
    with pytest.raises(ValueError, match="缺少"):
        evaluate_expression("close + volume", frame.drop(columns="volume"))
    with pytest.raises(ValueError, match="单只股票"):
        evaluate_expression("delay(close, 1)", frame.assign(code=["000001"] * 5 + ["000002"] * 5))
    with pytest.raises(ValueError, match="升序"):
        evaluate_expression("delay(close, 1)", frame.iloc[::-1])
    with pytest.raises(ValueError, match="重复"):
        evaluate_expression("delay(close, 1)", frame.assign(date=frame.date.iloc[0]))
    with pytest.raises(ValueError, match="字段不重复"):
        evaluate_expression("close", pd.concat([frame, frame[["close"]]], axis=1))


def test_api_metadata_and_templates_are_usable_without_a_database():
    syntax = expression_syntax()
    assert set(syntax["fields"]) == {"open", "high", "low", "close", "volume", "vwap"}
    assert syntax["max_window"] == 252 and syntax["max_lookback_rows"] == 504
    assert syntax["functions"] and all(item["signature"] and item["description"] for item in syntax["functions"])
    frame = market_frame()
    for template in expression_templates():
        assert validate_expression(template["expression"])["fields"]
        assert evaluate_expression(template["expression"], frame).notna().any()
