from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

import config as app_config
from agent.mcp.config import build_mcp_context_from_local_config, mcp_sdk_version
from agent.mcp.discovery import discover_mcp_tools, reset_discovery_cache
from application.support.backtest_display import build_display_date_options, is_prediction_only_date
from application.support.model_search_results import (
    BACKTEST_DISCLAIMER,
    BACKTEST_MASTER_TABLE_PATH,
    MODEL_CANDIDATES_PATH,
    MODEL_SEARCH_RESULTS_PATH,
    SELECTED_STRATEGY_PATH,
    load_daily_returns_for_strategy,
    load_selected_strategy,
)
from backtest import run_latest_t1_backtest
from backtest_rebalance import calculate_topk_rebalance
from config import (
    ANNOUNCEMENT_CACHE_PATH,
    BACKTEST_DAILY_PREDICTIONS_PATH,
    BACKTEST_METRICS_PATH,
    BACKTEST_NAV_PATH,
    BACKTEST_TRADES_PATH,
    DEFAULT_DFT_UNET_CHECKPOINT_PATH,
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    LATEST_RAW_DATA_PATH,
    ACTIVE_RANKING_MODEL_METRICS_PATH,
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    MARKET_CONTEXT_FEATURE_CACHE_PATH,
    MARKET_CONTEXT_INDEX_DAILY_CACHE_PATH,
    METRICS_PATH,
    NEWS_CACHE_PATH,
    OUTPUT_DIR,
    RAG_DOCUMENTS_PATH,
    RAG_INDEX_PATH,
    RANKING_LATEST_PATH,
    UNIVERSE,
)
from core.llm import LLMService
from core.llm.ollama_manager import (
    PROJECT_MODEL as OLLAMA_PROJECT_MODEL,
    PROJECT_MODELFILE_NAME as OLLAMA_PROJECT_MODELFILE_NAME,
    RECOMMENDED_BASE_MODEL,
    create_project_model,
    get_ollama_version,
    list_local_models,
    pull_model,
    validate_local_model,
)
from core.llm.runtime_settings import resolve_active_llm_settings
from data_tushare import A_SHARE_DAILY_DATA_READY_TIME, validate_tushare_token
from event_rules import classify_event_title
from llm_explainer import (
    build_stock_explanation_prompt,
    explain_prompt_with_llm,
    load_cached_ai_explanation,
)
from local_config import load_local_config, save_local_config
from ranking_runtime.settings import (
    ACTIVE_MODEL_BACKEND,
    ACTIVE_MODEL_NAME,
    ACTIVE_MODEL_VERSION,
    validate_active_model_assets,
)
from market_context import MARKET_CONTEXT_COLUMNS, ensure_market_context_for_feature_data
from runtime_paths import (
    ensure_runtime_directories,
    get_logs_dir,
    get_resource_root,
    get_runtime_dir,
    get_user_data_root,
    is_frozen_app,
)
from scheduler_manager import (
    create_scheduler,
    get_scheduler_jobs,
    read_auto_retrain_log,
    set_daily_retrain_job,
)

ENABLE_NEWS_FEATURES = getattr(app_config, "ENABLE_NEWS_FEATURES", True)
ENABLE_RAG = getattr(app_config, "ENABLE_RAG", True)
ENABLE_LLM_EXPLAINER = getattr(app_config, "ENABLE_LLM_EXPLAINER", True)


# Stage 6 freezes these callable names. They expose only the registered active
# ranker; concrete algorithm names stay inside the model manifest.
def downloaded_zoo_backends() -> dict[str, str]:
    return {ACTIVE_MODEL_NAME: ACTIVE_MODEL_BACKEND}


def registered_zoo_backends() -> dict[str, str]:
    return downloaded_zoo_backends()


def list_model_names() -> list[str]:
    return [ACTIVE_MODEL_NAME]


def is_zoo_backend(model_backend: str | None) -> bool:
    return False


def zoo_model_name_from_backend(model_backend: str) -> str:
    if str(model_backend or "").strip() != ACTIVE_MODEL_BACKEND:
        raise ValueError(f"非主动模型后端：{model_backend}")
    return ACTIVE_MODEL_NAME


@dataclass
class RollingUpdateJob:
    process: subprocess.Popen[Any]
    log_file: Any
    log_path: Path
    masked_command: list[str]

    def poll(self) -> int | None:
        return self.process.poll()

    def kill(self) -> None:
        self.process.kill()

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def write_log(self, text: str) -> None:
        self.log_file.write(str(text))
        self.log_file.flush()

    def close(self) -> None:
        try:
            self.log_file.close()
        except Exception:
            pass


class DashboardApplicationService:
    """Application boundary for the main Streamlit dashboard."""

    def __init__(self) -> None:
        ensure_runtime_directories()
        self.base_dir = get_resource_root()
        self.run_cwd = get_user_data_root() if is_frozen_app() else self.base_dir
        self.rolling_update_script = self.base_dir / "daily_incremental_update.py"
        self.progress_history_path = (
            get_runtime_dir() / "rolling_update_time_history.json"
            if is_frozen_app()
            else self.base_dir / "rolling_update_time_history.json"
        )
        self.log_dir = get_logs_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.rolling_update_log_path = self.log_dir / "rolling_update_app.log"

    @staticmethod
    def path_cache_version(path: str | Path) -> tuple[str, int, int]:
        resolved = Path(path)
        try:
            stat = resolved.stat()
            size = stat.st_size if resolved.is_file() else 0
            return str(resolved), int(stat.st_mtime_ns), int(size)
        except OSError:
            return str(resolved), 0, 0

    def load_time_estimate(self, default_seconds: int = 300) -> int:
        if not self.progress_history_path.exists():
            return int(default_seconds)
        try:
            data = json.loads(self.progress_history_path.read_text(encoding="utf-8"))
            durations = list(data.get("durations") or [])
            if not durations:
                return int(default_seconds)
            recent = durations[-3:]
            return max(int(sum(recent) / len(recent)), 60)
        except Exception:
            return int(default_seconds)

    def save_time_cost(self, seconds: float) -> None:
        data: dict[str, Any] = {"durations": []}
        if self.progress_history_path.exists():
            try:
                data = json.loads(self.progress_history_path.read_text(encoding="utf-8"))
            except Exception:
                data = {"durations": []}
        durations = list(data.get("durations") or [])
        durations.append(float(seconds))
        data["durations"] = durations[-10:]
        self.progress_history_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_history_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def read_log_tail(path: str | Path, max_chars: int = 2000) -> str:
        log_path = Path(path)
        if not log_path.exists():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            keywords = [
                "Traceback", "RuntimeError", "ImportError", "Error", "Failed",
                "失败", "错误", "pip install", "Preflight", "Return Code",
            ]
            hits = [
                idx for idx, line in enumerate(lines)
                if any(keyword in line for keyword in keywords)
            ]
            if hits:
                text = "\n".join(lines[max(hits[-1] - 20, 0):])
            return text[-int(max_chars):]
        except Exception:
            return ""

    @staticmethod
    def get_ranking_file_snapshot(path: str | Path = RANKING_LATEST_PATH) -> dict[str, Any]:
        from database.repositories import PredictionRepository

        del path
        rows = PredictionRepository().list_latest_predictions(
            model_name=ACTIVE_MODEL_NAME,
        )
        if not rows:
            return {
                "exists": False,
                "path": "database/model_prediction",
                "mtime": 0.0,
                "mtime_text": "无数据",
                "rows": 0,
                "signal_date": "",
                "prediction_date": "",
            }
        first = rows[0]
        updated_at = str(first.get("updated_at") or first.get("created_at") or "")
        updated_time = pd.to_datetime(updated_at, errors="coerce")
        mtime = float(updated_time.timestamp()) if pd.notna(updated_time) else 0.0
        snapshot: dict[str, Any] = {
            "exists": True,
            "path": "database/model_prediction",
            "mtime": mtime,
            "mtime_text": (
                updated_time.strftime("%Y-%m-%d %H:%M:%S")
                if pd.notna(updated_time)
                else ""
            ),
            "rows": len(rows),
            "signal_date": str(first.get("trade_date") or first.get("date") or ""),
            "prediction_date": str(
                first.get("prediction_for_date")
                or first.get("prediction_date")
                or ""
            ),
        }
        return snapshot

    def start_rolling_update_job(
        self,
        *,
        token: str,
        base_version: str,
        model_backend: str,
        checkpoint_path: str | None,
    ) -> RollingUpdateJob:
        cmd = [sys.executable]
        if is_frozen_app():
            cmd.append("--daily-update-child")
        else:
            cmd.append(str(self.rolling_update_script))
        model_backend = ACTIVE_MODEL_BACKEND
        cmd.extend([
            "--token", str(token),
            "--base-version", str(base_version),
            "--model-backend", str(model_backend),
        ])
        masked = ["***" if i > 0 and cmd[i - 1] == "--token" else part for i, part in enumerate(cmd)]
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        log_file = self.rolling_update_log_path.open("w", encoding="utf-8", errors="ignore")
        log_file.write("=" * 100 + "\n")
        log_file.write(f"[APP Rolling Update Start] {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        log_file.write(f"[Command] {' '.join(masked)}\n")
        log_file.write("=" * 100 + "\n")
        log_file.flush()
        process = subprocess.Popen(
            cmd,
            cwd=str(self.run_cwd),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            env=child_env,
        )
        return RollingUpdateJob(
            process=process,
            log_file=log_file,
            log_path=self.rolling_update_log_path,
            masked_command=masked,
        )

    @staticmethod
    def load_event_cache() -> pd.DataFrame:
        from news_data import load_event_cache
        result = load_event_cache()
        return result if isinstance(result, pd.DataFrame) else pd.DataFrame(result or [])

    @staticmethod
    def retrieve_stock_context(
        code: str,
        query: str,
        top_k: int = 5,
        force_rebuild: bool = False,
    ) -> pd.DataFrame:
        """Frozen transport adapter to the one canonical hybrid RAG service."""

        del force_rebuild
        from agent.services.evidence_service import evidence_service

        result = evidence_service.search_documents(
            query,
            stock_code=str(code or ""),
            top_k=top_k,
            output_dir=OUTPUT_DIR,
        )
        records = list((result.get("data") or {}).get("records") or [])
        return pd.DataFrame(records)

    @staticmethod
    def load_ranking(path: str | Path = RANKING_LATEST_PATH) -> pd.DataFrame | None:
        from database.repositories import PredictionRepository

        del path
        rows = PredictionRepository().list_latest_predictions(
            model_name=ACTIVE_MODEL_NAME,
        )
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        if "prediction_date" not in frame.columns and "date" in frame.columns:
            dates = pd.to_datetime(frame["date"], errors="coerce")
            frame["prediction_date"] = (dates + pd.offsets.BDay(1)).dt.strftime("%Y-%m-%d")
        return frame

    @staticmethod
    def load_metrics(path: str | Path | None = None) -> Any:
        file_path = Path(path or ACTIVE_RANKING_MODEL_METRICS_PATH)
        if not file_path.exists():
            return None
        try:
            if file_path.suffix.lower() == ".json":
                return json.loads(file_path.read_text(encoding="utf-8"))
            return joblib.load(file_path)
        except Exception:
            return None

    @staticmethod
    def load_json_file(path: str | Path) -> dict[str, Any] | None:
        file_path = Path(path)
        if not file_path.exists():
            return None
        try:
            value = json.loads(file_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    @staticmethod
    def load_backtest_outputs() -> tuple[Any, Any, Any, Any]:
        nav_df = trades_df = predictions_df = None
        metrics_data = None
        if Path(BACKTEST_NAV_PATH).exists():
            nav_df = pd.read_csv(BACKTEST_NAV_PATH)
            if "date" in nav_df.columns:
                nav_df["date"] = pd.to_datetime(nav_df["date"])
        if Path(BACKTEST_TRADES_PATH).exists():
            trades_df = pd.read_csv(BACKTEST_TRADES_PATH, dtype={"code": str})
            trades_df["code"] = trades_df["code"].astype(str).str.zfill(6)
        if Path(BACKTEST_METRICS_PATH).exists():
            try:
                metrics_data = json.loads(Path(BACKTEST_METRICS_PATH).read_text(encoding="utf-8"))
            except Exception:
                metrics_data = None
        if Path(BACKTEST_DAILY_PREDICTIONS_PATH).exists():
            predictions_df = pd.read_csv(BACKTEST_DAILY_PREDICTIONS_PATH, dtype={"code": str})
            predictions_df["code"] = predictions_df["code"].astype(str).str.zfill(6)
            if "date" in predictions_df.columns:
                predictions_df["date"] = pd.to_datetime(predictions_df["date"])
        return nav_df, metrics_data, trades_df, predictions_df

    @staticmethod
    def load_model_zoo_table() -> pd.DataFrame:
        report = validate_active_model_assets()
        return pd.DataFrame(
            [{
                "name": ACTIVE_MODEL_NAME,
                "backend": ACTIVE_MODEL_BACKEND,
                "status": "ready" if report.get("ready") else "missing",
                "version": report.get("model_version") or ACTIVE_MODEL_VERSION,
                "prediction_output": "下一交易日截面排序分",
                "ranking_source": "两个注册成员的当日截面百分位秩融合",
            }]
        )

    @staticmethod
    def load_external_backtest_summary() -> pd.DataFrame:
        path = Path(OUTPUT_DIR) / "backtests" / "backtest_summary.csv"
        if not path.exists():
            return pd.DataFrame()
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
            if "model_name" in frame.columns:
                frame = frame[frame["model_name"].astype(str).eq(ACTIVE_MODEL_NAME)]
            return frame
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def load_external_daily_returns(model_name: str, topk: int) -> pd.DataFrame:
        if str(model_name or "").strip() != ACTIVE_MODEL_NAME:
            return pd.DataFrame()
        path = Path(OUTPUT_DIR) / "backtests" / f"{model_name}_top{int(topk)}_daily_returns.csv"
        if not path.exists():
            return pd.DataFrame()
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
            if "date" in frame.columns:
                frame["date"] = pd.to_datetime(frame["date"])
            return frame
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def load_latest_raw_data() -> pd.DataFrame:
        path = Path(LATEST_RAW_DATA_PATH)
        if not path.exists():
            return pd.DataFrame()
        frame = pd.read_csv(path, dtype={"code": str})
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"])
        return frame

    @staticmethod
    def load_news_events_for_app() -> pd.DataFrame:
        if not ENABLE_NEWS_FEATURES:
            return pd.DataFrame()
        try:
            events = DashboardApplicationService.load_event_cache()
            if events.empty:
                return events
            if "date" in events.columns:
                events["date"] = pd.to_datetime(events["date"], errors="coerce")
            return events
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def file_status_rows(items: list[tuple[str, str | Path]]) -> list[dict[str, str]]:
        return [
            {
                "项目": str(label),
                "状态": "已生成" if Path(path).exists() else "未生成",
                "路径": str(Path(path)),
            }
            for label, path in items
        ]

    @staticmethod
    def rag_ready() -> bool:
        return Path(RAG_DOCUMENTS_PATH).exists() and Path(RAG_INDEX_PATH).exists()

    @staticmethod
    def load_external_model_status(
        checkpoint_path: str,
        ranking_df: pd.DataFrame | None = None,
    ) -> dict[str, Any]:
        return {
            "backend": ACTIVE_MODEL_BACKEND,
            "model_name": ACTIVE_MODEL_NAME,
            "status": "ready" if validate_active_model_assets().get("ready") else "missing",
            "prediction_output": "下一交易日截面排序分",
            "ranking_source": "两个注册成员的当日截面百分位秩融合",
            "assets": validate_active_model_assets(),
        }

        # Historical DFT implementation remains unreachable below because the
        # Stage 6 method signature is frozen for transport compatibility.
        path = Path(checkpoint_path)
        summary_path = path.parent / "summary.json"
        summary: dict[str, Any] = {}
        if summary_path.exists():
            try:
                value = json.loads(summary_path.read_text(encoding="utf-8"))
                summary = value if isinstance(value, dict) else {}
            except Exception as exc:
                summary = {"summary_error": f"{type(exc).__name__}: {exc}"}
        args = summary.get("args") or {}
        seq_len = int(args.get("seq_len", 8))
        d_feat = int(args.get("d_feat", 158))
        gate_start = int(args.get("gate_input_start_index", 158))
        gate_end = int(args.get("gate_input_end_index", 221))
        ranking_date = "未知"
        ranking_rows = 0
        ranking_model_name = ""
        if ranking_df is not None and not ranking_df.empty:
            ranking_rows = len(ranking_df)
            if "date" in ranking_df.columns:
                ranking_date = str(ranking_df["date"].iloc[0])[:10]
            if "model_name" in ranking_df.columns:
                names = ranking_df["model_name"].dropna().astype(str).unique().tolist()
                ranking_model_name = ", ".join(names[:3])

        def cache_status(cache_path: str | Path, usecols: list[str], *, index: bool = False) -> dict[str, Any]:
            result: dict[str, Any] = {
                "exists": Path(cache_path).exists(),
                "path": str(cache_path),
                "rows": 0,
                "date_min": "",
                "date_max": "",
            }
            if index:
                result["index_count"] = 0
            if not result["exists"]:
                return result
            try:
                frame = pd.read_csv(cache_path, usecols=usecols)
                frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
                frame = frame.dropna(subset=["date"])
                result.update({
                    "rows": int(len(frame)),
                    "date_min": str(frame["date"].min().date()) if not frame.empty else "",
                    "date_max": str(frame["date"].max().date()) if not frame.empty else "",
                })
                if index:
                    result["index_count"] = int(frame["index_code"].nunique()) if "index_code" in frame.columns else 0
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            return result

        context_status = cache_status(MARKET_CONTEXT_FEATURE_CACHE_PATH, ["date"])
        context_status["columns"] = len(MARKET_CONTEXT_COLUMNS)
        index_status = cache_status(
            MARKET_CONTEXT_INDEX_DAILY_CACHE_PATH,
            ["date", "index_code"],
            index=True,
        )
        return {
            "backend": "External DFT_UNET",
            "checkpoint_path": str(path),
            "checkpoint_exists": path.exists(),
            "checkpoint_size_mb": round(path.stat().st_size / 1024 / 1024, 2) if path.exists() else None,
            "summary_json_found": summary_path.exists(),
            "model_name": summary.get("model_name", "DFT_UNET"),
            "run_name": summary.get("run_name", ""),
            "best_epoch": summary.get("best_epoch"),
            "best_score": summary.get("best_score"),
            "prediction_sign": summary.get("prediction_sign", 1.0),
            "input_shape": f"[N, {seq_len}, {gate_end}]",
            "seq_len": seq_len,
            "stock_feature_count": d_feat,
            "market_context_count": max(0, gate_end - gate_start),
            "ranking_date": ranking_date,
            "ranking_rows": ranking_rows,
            "ranking_model_name": ranking_model_name,
            "market_context_cache": context_status,
            "market_index_cache": index_status,
            "summary_error": summary.get("summary_error", ""),
        }

    @staticmethod
    def run_external_backend_ranking(
        model_backend: str,
        checkpoint_path: str,
        token: str | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        if model_backend != ACTIVE_MODEL_BACKEND:
            raise RuntimeError(f"仅支持主动模型后端 {ACTIVE_MODEL_BACKEND}")
        if not token:
            raise RuntimeError("运行每日排名更新需要 Tushare Token")
        from daily_incremental_update import active_ranker_daily_update

        ranking_df, backend_report = active_ranker_daily_update(token=token)
        return ranking_df, backend_report


    @staticmethod
    def test_neo4j_connection(local_config: dict[str, Any]) -> dict[str, Any]:
        from agent.graph.settings import Neo4jSettings
        from agent.graph.store import Neo4jFinancialGraphStore

        graph_settings = Neo4jSettings.from_env(local_config=dict(local_config))
        graph_store = Neo4jFinancialGraphStore(graph_settings)
        try:
            graph_store.verify_connectivity()
            graph_store.ensure_schema()
            count_rows = graph_store.execute_read("MATCH (n) RETURN count(n) AS node_count")
            node_count = int((count_rows[0] if count_rows else {}).get("node_count") or 0)
            return {"success": True, "node_count": node_count}
        finally:
            graph_store.close()

    @staticmethod
    def inspect_model(model_backend: str, checkpoint_path: str, zoo_table: pd.DataFrame) -> dict[str, Any]:
        if model_backend != ACTIVE_MODEL_BACKEND:
            raise RuntimeError(f"非主动模型后端：{model_backend}")
        return {
            "kind": ACTIVE_MODEL_BACKEND,
            "model_name": ACTIVE_MODEL_NAME,
            "report": validate_active_model_assets(),
        }


dashboard_service = DashboardApplicationService()

# Runtime paths exposed as immutable application-level values for the UI.
BASE_DIR = dashboard_service.base_dir
RUN_CWD = dashboard_service.run_cwd
ROLLING_UPDATE_SCRIPT = dashboard_service.rolling_update_script
PROGRESS_HISTORY_PATH = dashboard_service.progress_history_path
LOG_DIR = dashboard_service.log_dir
ROLLING_UPDATE_LOG_PATH = dashboard_service.rolling_update_log_path


__all__ = [name for name in globals() if not name.startswith("_")]
