from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@pytest.fixture
def fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"


@pytest.fixture
def sample_ranking_df(fixtures_dir: Path) -> pd.DataFrame:
    return pd.read_csv(fixtures_dir / "sample_ranking.csv", dtype={"code": str}, encoding="utf-8-sig")


@pytest.fixture
def sample_backtest_master_df(fixtures_dir: Path) -> pd.DataFrame:
    return pd.read_csv(fixtures_dir / "sample_backtest_master_table.csv", encoding="utf-8-sig")


@pytest.fixture
def sample_daily_returns_df(fixtures_dir: Path) -> pd.DataFrame:
    return pd.read_csv(fixtures_dir / "sample_daily_returns.csv", encoding="utf-8-sig")


@pytest.fixture
def sample_search_results_df(fixtures_dir: Path) -> pd.DataFrame:
    return pd.read_csv(fixtures_dir / "sample_search_results.csv", encoding="utf-8-sig")


@pytest.fixture
def sample_prediction_df() -> pd.DataFrame:
    rows = []
    dates = pd.date_range("2026-01-01", periods=10, freq="D")
    stocks = [
        ("000001", "A", 0.30, 0.10),
        ("000002", "B", 0.20, 0.05),
        ("000003", "C", 0.10, -0.02),
    ]
    for date in dates:
        for code, name, prediction, future_return in stocks:
            rows.append(
                {
                    "date": date,
                    "code": code,
                    "name": name,
                    "pred_5d_ret": prediction,
                    "raw_score": prediction,
                    "score": prediction,
                    "future_5d_ret": future_return,
                }
            )
    return pd.DataFrame(rows)


def pytest_collection_modifyitems(config, items):
    del config
    for item in items:
        path_text = str(item.path).replace("\\", "/").lower()
        name = item.name.lower()
        if "/tests/e2e/" in path_text:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)
        elif "/tests/integration/" in path_text:
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.fast)

        if any(token in path_text or token in name for token in ["external", "live_api", "tushare", "network_api"]):
            item.add_marker(pytest.mark.external)
        if any(token in path_text or token in name for token in ["playwright", "starts_on_temp_port"]):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)
        if any(token in path_text or token in name for token in ["slow", "timeout", "long_running"]):
            item.add_marker(pytest.mark.slow)
