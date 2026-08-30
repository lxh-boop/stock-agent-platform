from __future__ import annotations

import os
import json
import shutil
import sys
from pathlib import Path


APP_DIR_NAME = "StockDailyApp"
SEED_DIR_NAME = "bundled_seed"
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_SENSITIVE_SEED_FILENAMES = {
    ".env",
    "local_app_config.json",
    "local_config.json",
}
_SENSITIVE_SEED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "logs",
    "runtime",
    "config",
}
_DATABASE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_project_root() -> Path:
    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return _SOURCE_ROOT


def get_resource_root() -> Path:
    if is_frozen_app():
        return Path(getattr(sys, "_MEIPASS", get_project_root())).resolve()
    return _SOURCE_ROOT


def get_user_data_root() -> Path:
    if not is_frozen_app():
        return _SOURCE_ROOT
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def get_cache_dir() -> Path:
    return get_user_data_root() / "cache" if is_frozen_app() else _SOURCE_ROOT / "data"


def get_data_dir() -> Path:
    return get_cache_dir() / "data" if is_frozen_app() else _SOURCE_ROOT / "data"


def get_database_dir() -> Path:
    return get_user_data_root() / "database" if is_frozen_app() else _SOURCE_ROOT / "data"


def get_outputs_dir() -> Path:
    return get_user_data_root() / "outputs" if is_frozen_app() else _SOURCE_ROOT / "outputs"


def get_logs_dir() -> Path:
    return get_user_data_root() / "logs" if is_frozen_app() else _SOURCE_ROOT / "logs"


def get_config_dir() -> Path:
    return get_user_data_root() / "config" if is_frozen_app() else _SOURCE_ROOT


def get_models_dir() -> Path:
    return get_user_data_root() / "models" if is_frozen_app() else _SOURCE_ROOT / "models"


def get_runtime_dir() -> Path:
    return get_user_data_root() / "runtime" if is_frozen_app() else _SOURCE_ROOT / "runtime"


def get_local_config_path() -> Path:
    return get_config_dir() / "local_app_config.json"


def ensure_runtime_directories() -> None:
    for path in [
        get_database_dir(),
        get_outputs_dir(),
        get_logs_dir(),
        get_cache_dir(),
        get_data_dir(),
        get_runtime_dir(),
        get_config_dir(),
        get_models_dir(),
    ]:
        path.mkdir(parents=True, exist_ok=True)
    seed_user_data_from_bundle()


def get_bundled_seed_root() -> Path:
    return get_resource_root() / SEED_DIR_NAME


def _skip_seed_path(path: Path) -> bool:
    lowered_name = path.name.lower()
    if lowered_name in _SENSITIVE_SEED_FILENAMES:
        return True
    if path.suffix.lower() in _DATABASE_FILE_SUFFIXES:
        return True
    return any(part.lower() in _SENSITIVE_SEED_DIRS for part in path.parts)


def _copy_missing_tree(source: Path, target: Path, skip_names: set[str] | None = None) -> int:
    if not source.exists():
        return 0

    skip_names = {name.lower() for name in (skip_names or set())}
    copied = 0
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        if _skip_seed_path(rel):
            continue
        if item.name.lower() in skip_names:
            continue

        destination = target / rel
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue

        if destination.exists():
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied += 1
    return copied


def seed_user_data_from_bundle() -> None:
    if not is_frozen_app():
        return

    seed_root = get_bundled_seed_root()
    if not seed_root.exists():
        return

    copied = {
        "data": 0,
        "models": 0,
        "outputs": 0,
    }

    # PostgreSQL-only: bundled seed may contain non-database demo/cache data,
    # but local SQLite database files are never restored into user data.
    copied["data"] += _copy_missing_tree(
        seed_root / "data",
        get_data_dir(),
    )
    copied["models"] += _copy_missing_tree(seed_root / "models", get_models_dir())
    copied["outputs"] += _copy_missing_tree(seed_root / "outputs", get_outputs_dir())

    marker_path = get_runtime_dir() / "bundled_seed_state.json"
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps(
            {
                "seed_root": str(seed_root),
                "copied": copied,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


ROOT_DIR = get_project_root()
DATA_DIR = get_data_dir()
MODELS_DIR = get_models_dir()
OUTPUTS_DIR = get_outputs_dir()
LOGS_DIR = get_logs_dir()
RUNTIME_DIR = get_runtime_dir()
CONFIG_DIR = get_config_dir()
DATABASE_DIR = get_database_dir()
RESOURCE_DIR = get_resource_root()
EXTERNAL_REPOS_DIR = ROOT_DIR / "external_repos"

MODEL_DISCOVERY_DIR = OUTPUTS_DIR / "model_discovery"
MODEL_CANDIDATES_PATH = MODEL_DISCOVERY_DIR / "model_candidates.csv"
MODEL_CANDIDATES_JSON_PATH = MODEL_DISCOVERY_DIR / "model_candidates.json"
MODEL_DISCOVERY_REPORT_PATH = MODEL_DISCOVERY_DIR / "model_discovery_report.md"
MODEL_DOWNLOAD_LOG_PATH = MODEL_DISCOVERY_DIR / "model_download_log.csv"
MODEL_TRAIN_LOG_PATH = MODEL_DISCOVERY_DIR / "model_train_log.csv"
MODEL_DISCOVERY_ERRORS_PATH = MODEL_DISCOVERY_DIR / "errors.csv"

BACKTEST_DIR = OUTPUTS_DIR / "backtests"
BACKTEST_DAILY_RETURNS_DIR = BACKTEST_DIR / "daily_returns"
BACKTEST_PREDICTIONS_DIR = BACKTEST_DIR / "predictions"
BACKTEST_METRICS_DIR = BACKTEST_DIR / "metrics"
BACKTEST_MASTER_TABLE_PATH = BACKTEST_DIR / "backtest_master_table.csv"

MODEL_SEARCH_DIR = OUTPUTS_DIR / "model_search"
MODEL_SEARCH_RESULTS_PATH = MODEL_SEARCH_DIR / "search_results.csv"
MODEL_SEARCH_ERRORS_PATH = MODEL_SEARCH_DIR / "search_errors.csv"
MODEL_SEARCH_REPORT_PATH = MODEL_SEARCH_DIR / "model_search_report.md"
SELECTED_STRATEGY_PATH = MODEL_SEARCH_DIR / "selected_strategy.json"


def project_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT_DIR / path
