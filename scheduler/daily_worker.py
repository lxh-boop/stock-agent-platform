from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from config import ACTIVE_RANKING_MODEL_NAME, NEWS_EVENT_LOOKBACK_DAYS
from database.repositories import NewsRepository, PredictionRepository
from scheduler.job_lock import JobLock, JobLockError
from scheduler.job_state import run_recorded_step, save_job_status
from scheduler.schemas import JobStatus, SchedulerStatus, make_run_id, now_text
from scheduler.trading_calendar import get_latest_trading_day, is_trading_day, parse_date
from scheduler.user_job_runner import get_active_user_ids, run_user_daily_job


PUBLIC_MARKER_DIR = Path("runtime") / "jobs" / "public_tasks"
SCHEDULER_LOG_DIR = Path("logs") / "scheduler"
PUBLIC_TASK_VERSION = "news_rag_fulltext_v5_daily_pipeline"


def _csv_signal_date(path: str | Path) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle), None) or {}
        return str(row.get("date") or row.get("signal_date") or "")[:10]
    except Exception:
        return ""


def _ranking_signal_date(db_path: str | Path | None) -> str:
    rows = PredictionRepository(db_path).list_latest_predictions(
        model_name=ACTIVE_RANKING_MODEL_NAME,
        limit=1,
    )
    return str((rows[0] if rows else {}).get("trade_date") or "")[:10]


def run_market_update_from_local_config(
    *,
    trade_date: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    root: str | Path = ".",
    force: bool = False,
    dry_run: bool = False,
    skip_market_update: bool = False,
) -> dict[str, Any]:
    """先下载行情并生成目标交易日排名，再进入用户级任务。

    Token 只从本地配置或环境变量读取，不写入状态文件和命令日志。
    """

    if dry_run or skip_market_update:
        return {
            "status": SchedulerStatus.SKIPPED,
            "warnings": ["market update skipped by scheduler option."],
            "metadata": {"signal_date": _ranking_signal_date(db_path)},
        }

    current_signal_date = _ranking_signal_date(db_path)
    if current_signal_date == trade_date and not force:
        return {
            "status": SchedulerStatus.SKIPPED,
            "warnings": [f"ranking already updated for {trade_date}."],
            "metadata": {"signal_date": current_signal_date, "already_current": True},
        }

    from local_config import load_local_config

    config = load_local_config()
    from ranking_runtime.settings import ACTIVE_MODEL_BACKEND, ACTIVE_MODEL_VERSION

    configured_backend = str(config.get("model_backend") or "").strip()
    model_backend = ACTIVE_MODEL_BACKEND
    if configured_backend and configured_backend != ACTIVE_MODEL_BACKEND:
        print(
            f"[Scheduler] ignore inactive model backend {configured_backend}; "
            f"use {ACTIVE_MODEL_BACKEND}."
        )
    model_version = ACTIVE_MODEL_VERSION
    timeout_seconds = max(1, int(config.get("scheduler_market_update_timeout_seconds") or 997200))

    project_root = Path(root).resolve()
    script = project_root / "daily_incremental_update.py"
    if not script.exists():
        raise FileNotFoundError(f"daily_incremental_update.py not found: {script}")

    command = [
        sys.executable,
        str(script),
        "--base-version",
        model_version,
        "--model-backend",
        model_backend,
    ]

    log_dir = project_root / SCHEDULER_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stdout_path = log_dir / f"market_update_{stamp}.out.log"
    stderr_path = log_dir / f"market_update_{stamp}.err.log"
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"daily_incremental_update failed with return code {completed.returncode}; "
            f"see {stderr_path}"
        )

    signal_date = _ranking_signal_date(db_path)
    if signal_date != trade_date:
        raise RuntimeError(
            f"ranking signal date mismatch: expected={trade_date}, actual={signal_date or 'missing'}"
        )

    return {
        "status": SchedulerStatus.SUCCESS,
        "metadata": {
            "signal_date": signal_date,
            "model_backend": model_backend,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    }


def _date_text(value: Any) -> str:
    return parse_date(value).strftime("%Y-%m-%d")


def _job_log_path(trade_date: str) -> Path:
    SCHEDULER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return SCHEDULER_LOG_DIR / f"daily_worker_{str(trade_date).replace('-', '')}.log"


def _append_log(trade_date: str, text: str) -> None:
    path = _job_log_path(trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"[{now_text()}] {text}\n")


def _public_marker_path(trade_date: str, root: str | Path = ".") -> Path:
    return Path(root) / PUBLIC_MARKER_DIR / f"public_{str(trade_date).replace('-', '')}.json"


def _public_already_done(trade_date: str, root: str | Path = ".") -> bool:
    path = _public_marker_path(trade_date, root)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (
            data.get("status") == SchedulerStatus.SUCCESS
            and data.get("public_task_version") == PUBLIC_TASK_VERSION
            and data.get("news_refresh_attempted") is True
            and data.get("public_data_ready") is True
            and data.get("public_data_healthy") is True
        )
    except Exception:
        return False


def _write_public_marker(trade_date: str, payload: dict[str, Any], root: str | Path = ".") -> None:
    path = _public_marker_path(trade_date, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _copy_ranking_to_shared(
    output_dir: str | Path,
    dry_run: bool,
    db_path: str | Path | None,
) -> str:
    from database.runtime_data_import import import_ranking_file

    root = Path(output_dir)
    source = root / "ranking_latest.csv"
    shared = root / "shared" / "ranking_latest.csv"
    if not source.exists():
        raise FileNotFoundError(f"ranking_latest.csv not found: {source}")
    import_ranking_file(
        source,
        db_path=db_path,
        dry_run=dry_run,
    )
    if not dry_run:
        shared.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, shared)
    return str(shared if not dry_run else source)


def _count_news(db_path: str | Path | None) -> tuple[int, int]:
    try:
        repo = NewsRepository(db_path)
        return len(repo.list_news_events()), len(repo.list_news_chunks())
    except Exception:
        return 0, 0


def _latest_news_publish_time(db_path: str | Path | None = None) -> str:
    del db_path
    from database.postgres_store import PostgresStore
    row = PostgresStore().fetch_one("SELECT COALESCE(MAX(publish_time),'') AS value FROM news_event")
    return str((row or {}).get("value") or "")


def _resolve_news_refresh_range(
    trade_date: str,
    db_path: str | Path | None,
) -> tuple[str, str, str]:
    """Catch up from the current corpus head, with one-day overlap for updates."""

    end_day = parse_date(trade_date)
    latest = _latest_news_publish_time(db_path)
    if latest:
        try:
            latest_day = parse_date(latest[:10])
            start_day = min(end_day, latest_day - timedelta(days=1))
        except Exception:
            start_day = end_day - timedelta(days=max(1, int(NEWS_EVENT_LOOKBACK_DAYS)))
    else:
        start_day = end_day - timedelta(days=max(1, int(NEWS_EVENT_LOOKBACK_DAYS)))
    return start_day.strftime("%Y-%m-%d"), end_day.strftime("%Y-%m-%d"), latest


def _resolved_output_path(root: str | Path, output_dir: str | Path) -> Path:
    path = Path(output_dir)
    return path if path.is_absolute() else Path(root).resolve() / path


def run_news_refresh_and_rebuild(
    *,
    trade_date: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    root: str | Path = ".",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one full-text-first public ingestion, then rebuild production RAG indexes once.

    The public step fetches metadata, acquires/validates article bodies before active DB persistence,
    removes legacy title-only RAG rows, writes structured chunk metadata, and finally rebuilds BM25/Dense.
    Ranking-model inputs remain market/model features only.
    """

    project_root = Path(root).resolve()
    resolved_db = None
    resolved_output = _resolved_output_path(project_root, output_dir)
    start_date, end_date, latest_before = _resolve_news_refresh_range(trade_date, resolved_db)
    before_events, before_chunks = _count_news(resolved_db)

    if dry_run:
        return {
            "status": SchedulerStatus.SKIPPED,
            "metadata": {
                "refresh_attempted": False,
                "dry_run": True,
                "start_date": start_date,
                "end_date": end_date,
                "latest_news_before": latest_before,
                "news_event_count_before": before_events,
                "news_chunk_count_before": before_chunks,
            },
        }

    script = project_root / "scripts" / "refresh_news_rag_fulltext.py"
    if not script.exists():
        raise FileNotFoundError(f"full-text news/RAG refresh script not found: {script}")

    from data_tushare import get_token
    from local_config import load_local_config

    local_config = load_local_config()
    timeout_seconds = max(1, int(local_config.get("scheduler_news_refresh_timeout_seconds") or 7200))
    full_text_workers = max(1, min(16, int(local_config.get("scheduler_news_full_text_workers") or 6)))
    full_text_timeout = max(5, int(local_config.get("scheduler_news_full_text_timeout_seconds") or 15))
    full_text_retries = max(0, min(5, int(local_config.get("scheduler_news_full_text_retries") or 1)))
    token = str(get_token() or "").strip()

    report_dir = project_root / "runtime" / "jobs" / "news_rag_refresh"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"news_rag_refresh_{trade_date.replace('-', '')}.json"

    log_dir = project_root / SCHEDULER_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stdout_path = log_dir / f"news_rag_refresh_{stamp}.out.log"
    stderr_path = log_dir / f"news_rag_refresh_{stamp}.err.log"

    command = [
        sys.executable,
        str(script),
        "--output-dir",
        str(resolved_output),
        "--start-date",
        start_date,
        "--end-date",
        end_date,
        "--workers",
        str(full_text_workers),
        "--timeout",
        str(full_text_timeout),
        "--retries",
        str(full_text_retries),
        "--report-path",
        str(report_path),
    ]
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    if token:
        # Keep credentials out of command arguments and scheduler state/logs.
        environment["TUSHARE_TOKEN"] = token

    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        completed = subprocess.run(
            command,
            cwd=str(project_root),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout_seconds,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"news/RAG refresh failed with return code {completed.returncode}; see {stderr_path}"
        )

    after_events, after_chunks = _count_news(resolved_db)
    latest_after = _latest_news_publish_time(resolved_db)
    if not latest_after:
        raise RuntimeError("news_refresh_business_empty: no news_event records after refresh")

    try:
        latest_after_day = parse_date(latest_after[:10])
        end_day = parse_date(end_date)
        max_staleness_days = max(7, int(NEWS_EVENT_LOOKBACK_DAYS))
        if (end_day - latest_after_day).days > max_staleness_days:
            raise RuntimeError(
                "news_refresh_stale_after_attempt: "
                f"requested={start_date}..{end_date}, latest_db_news={latest_after}, "
                f"max_staleness_days={max_staleness_days}"
            )
    except ValueError:
        raise RuntimeError(f"news_refresh_invalid_latest_timestamp: {latest_after}")

    report_payload: dict[str, Any] = {}
    if report_path.exists():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                report_payload = loaded
        except Exception:
            report_payload = {}
    ingestion_meta = dict(report_payload.get("ingestion") or {})

    index_dir = resolved_output / "rag_indexes"
    bm25_path = index_dir / "news_bm25.pkl"
    dense_path = index_dir / "news_dense.pkl"
    missing = [path.name for path in (bm25_path, dense_path) if not path.exists()]
    if missing:
        raise RuntimeError(f"rag_index_rebuild_missing_output: {', '.join(missing)}")

    ordinary_status = str(ingestion_meta.get("ordinary_news_status") or "unknown")
    ordinary_listing_rows = int(ingestion_meta.get("ordinary_news_listing_rows") or 0)
    ordinary_full_text_written = int(ingestion_meta.get("ordinary_news_full_text_written") or 0)
    source_diagnostics = dict(ingestion_meta.get("source_diagnostics") or {})
    warnings: list[str] = []
    step_status = SchedulerStatus.SUCCESS
    if ordinary_status in {"provider_failed", "partial"}:
        step_status = SchedulerStatus.PARTIAL_SUCCESS
        warnings.append(
            "ordinary_news_provider_degraded: "
            f"status={ordinary_status}, listing_rows={ordinary_listing_rows}, "
            f"full_text_written={ordinary_full_text_written}"
        )
    elif ordinary_status == "business_empty":
        warnings.append(
            "ordinary_news_business_result_empty: provider calls completed but no ordinary stock-news rows "
            f"matched {start_date}..{end_date}."
        )
    elif ordinary_status not in {"success", "not_attempted"}:
        warnings.append(f"ordinary_news_status_unknown:{ordinary_status}")

    return {
        "status": step_status,
        "warnings": warnings,
        "metadata": {
            "refresh_attempted": True,
            "start_date": start_date,
            "end_date": end_date,
            "latest_news_before": latest_before,
            "latest_news_after": latest_after,
            "news_event_count_before": before_events,
            "news_event_count_after": after_events,
            "news_chunk_count_before": before_chunks,
            "news_chunk_count_after": after_chunks,
            "full_text_only_policy": True,
            "full_text_articles_written": int(ingestion_meta.get("full_text_articles_written") or 0),
            "ordinary_news_status": ordinary_status,
            "ordinary_news_listing_rows": ordinary_listing_rows,
            "ordinary_news_full_text_written": ordinary_full_text_written,
            "announcement_full_text_written": int(ingestion_meta.get("announcement_full_text_written") or 0),
            "source_diagnostics": source_diagnostics,
            "title_only_events_deleted": int(ingestion_meta.get("title_only_events_deleted") or 0),
            "title_only_chunks_deleted": int(ingestion_meta.get("title_only_chunks_deleted") or 0),
            "structured_chunks_updated": int(ingestion_meta.get("structured_chunks_updated") or 0),
            "bm25_index_path": str(bm25_path),
            "dense_index_path": str(dense_path),
            "bm25_index_mtime_ns": bm25_path.stat().st_mtime_ns,
            "dense_index_mtime_ns": dense_path.stat().st_mtime_ns,
            "report_path": str(report_path),
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "public_data_ready": True,
            "public_data_healthy": step_status == SchedulerStatus.SUCCESS,
        },
    }


def run_public_daily_tasks(
    trade_date: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    skip_training: bool = False,
    skip_news: bool = False,
    root: str | Path = ".",
) -> dict[str, Any]:
    warnings: list[str] = []
    source_signal_date = _ranking_signal_date(db_path)
    shared_signal_date = _csv_signal_date(Path(output_dir) / "shared" / "ranking_latest.csv")
    marker_dates_are_current = (
        source_signal_date == trade_date and shared_signal_date == trade_date
    ) or (not source_signal_date and not shared_signal_date)
    if (
        _public_already_done(trade_date, root)
        and not force
        and marker_dates_are_current
    ):
        marker_meta: dict[str, Any] = {}
        try:
            marker_meta = json.loads(_public_marker_path(trade_date, root).read_text(encoding="utf-8"))
        except Exception:
            marker_meta = {}
        return {
            "status": SchedulerStatus.SKIPPED,
            "warnings": [f"public tasks already completed for {trade_date}."],
            "metadata": {
                **marker_meta,
                "public_task_once": True,
                "signal_date": source_signal_date or str(marker_meta.get("signal_date") or ""),
            },
        }

    ranking_path = _copy_ranking_to_shared(
        output_dir,
        dry_run=dry_run,
        db_path=db_path,
    )
    news_event_count = 0
    news_chunk_count = 0
    news_refresh: dict[str, Any] = {}
    if skip_training:
        warnings.append("training/model refresh skipped by scheduler option.")
    if skip_news:
        warnings.append("news download/ingestion and RAG index refresh skipped by scheduler option.")
    else:
        news_refresh = run_news_refresh_and_rebuild(
            trade_date=trade_date,
            output_dir=output_dir,
            db_path=db_path,
            root=root,
            dry_run=dry_run,
        )
        warnings.extend(list(news_refresh.get("warnings") or []))
        news_event_count, news_chunk_count = _count_news(db_path)
        if news_event_count == 0:
            warnings.append("news refresh completed but no news_event records exist; scoring will keep news adjustment neutral.")

    refresh_meta = news_refresh.get("metadata") or {}
    public_status = str(news_refresh.get("status") or SchedulerStatus.SUCCESS) if not skip_news else SchedulerStatus.SUCCESS
    if public_status not in {SchedulerStatus.SUCCESS, SchedulerStatus.PARTIAL_SUCCESS}:
        public_status = SchedulerStatus.SUCCESS
    payload = {
        "status": public_status,
        "public_task_version": PUBLIC_TASK_VERSION,
        "trade_date": trade_date,
        "ranking_output_path": ranking_path,
        "signal_date": source_signal_date,
        "news_refresh_attempted": bool(refresh_meta.get("refresh_attempted")),
        "news_refresh_start_date": str(refresh_meta.get("start_date") or ""),
        "news_refresh_end_date": str(refresh_meta.get("end_date") or ""),
        "news_latest_publish_time": str(refresh_meta.get("latest_news_after") or _latest_news_publish_time(db_path)),
        "news_event_count": news_event_count,
        "news_chunk_count": news_chunk_count,
        "rag_bm25_index_path": str(refresh_meta.get("bm25_index_path") or ""),
        "rag_dense_index_path": str(refresh_meta.get("dense_index_path") or ""),
        "news_refresh_report_path": str(refresh_meta.get("report_path") or ""),
        "ordinary_news_status": str(refresh_meta.get("ordinary_news_status") or ("skipped" if skip_news else "unknown")),
        "ordinary_news_listing_rows": int(refresh_meta.get("ordinary_news_listing_rows") or 0),
        "ordinary_news_full_text_written": int(refresh_meta.get("ordinary_news_full_text_written") or 0),
        "announcement_full_text_written": int(refresh_meta.get("announcement_full_text_written") or 0),
        "public_data_ready": bool(refresh_meta.get("public_data_ready", skip_news)),
        "public_data_healthy": bool(refresh_meta.get("public_data_healthy", skip_news)),
        "dry_run": dry_run,
        "finished_at": now_text(),
    }
    if not dry_run:
        _write_public_marker(trade_date, payload, root)
    return {
        "status": public_status,
        "warnings": warnings,
        "metadata": payload,
    }


def _resolve_trade_date(trade_date: str | None, run_time: str | datetime | None, force: bool) -> tuple[str, bool]:
    base = parse_date(trade_date) if trade_date else parse_date(run_time)
    requested_is_trading = is_trading_day(base)
    if trade_date:
        return base.strftime("%Y-%m-%d"), requested_is_trading
    if requested_is_trading:
        return base.strftime("%Y-%m-%d"), True
    latest = get_latest_trading_day(base)
    return latest.strftime("%Y-%m-%d"), False if not force else is_trading_day(latest)


def run_scheduled_daily_update(
    trade_date: str | None = None,
    run_time: str | datetime | None = None,
    user_ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    skip_training: bool = False,
    skip_news: bool = False,
    skip_paper_trading: bool = False,
    skip_market_update: bool = False,
    source: str = "manual",
    top_k: int = 50,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    root: str | Path = ".",
    market_update_runner: Callable[..., dict[str, Any]] | None = None,
    public_task_runner: Callable[..., dict[str, Any]] | None = None,
    user_task_runner: Callable[..., dict[str, Any]] | None = None,
) -> JobStatus:
    started = perf_counter()
    run_time = run_time or datetime.now()
    resolved_trade_date, requested_is_trading = _resolve_trade_date(trade_date, run_time, force=force)
    job = JobStatus(
        job_id=f"daily_update_{resolved_trade_date}",
        run_id=make_run_id(str(source or "manual")),
        trade_date=resolved_trade_date,
        execution_source=str(source or "manual"),
        overall_status=SchedulerStatus.RUNNING,
        is_trading_day=is_trading_day(resolved_trade_date),
    )
    save_job_status(job, root)
    _append_log(resolved_trade_date, f"job started run_id={job.run_id} dry_run={dry_run} force={force}")

    if not requested_is_trading and not force:
        job.overall_status = SchedulerStatus.SKIPPED
        job.finished_at = now_text()
        job.duration_seconds = round(perf_counter() - started, 3)
        job.warnings.append("requested date is not an A-share trading day; skipped.")
        save_job_status(job, root)
        _append_log(resolved_trade_date, "job skipped because requested date is not trading day")
        return job

    lock = JobLock(
        lock_path=Path(root) / "runtime" / "locks" / "daily_update.lock",
        job_id=job.job_id,
        trade_date=resolved_trade_date,
        force=force,
    )
    try:
        with lock:
            # 单元测试常注入 public_task_runner；只有真实链路或显式注入
            # market_update_runner 时才执行行情下载，避免测试触网。
            should_run_market_update = market_update_runner is not None or public_task_runner is None
            if should_run_market_update:
                market_update_runner = market_update_runner or run_market_update_from_local_config
                market_result = run_recorded_step(
                    job,
                    "market_update",
                    lambda: market_update_runner(
                        trade_date=resolved_trade_date,
                        output_dir=output_dir,
                        db_path=db_path,
                        root=root,
                        force=force,
                        dry_run=dry_run,
                        skip_market_update=skip_market_update,
                    ),
                    root=root,
                )
                market_meta = market_result.get("metadata") or {}
                job.latest_signal_date = str(market_meta.get("signal_date") or "")

            public_task_runner = public_task_runner or run_public_daily_tasks
            public_result = run_recorded_step(
                job,
                "public_tasks",
                lambda: public_task_runner(
                    trade_date=resolved_trade_date,
                    output_dir=output_dir,
                    db_path=db_path,
                    force=force,
                    dry_run=dry_run,
                    skip_training=skip_training,
                    skip_news=skip_news,
                    root=root,
                ),
                root=root,
            )
            job.public_task_status = public_result
            public_step_status = str(public_result.get("status") or SchedulerStatus.SUCCESS)
            meta = public_result.get("metadata") or {}
            job.ranking_output_path = str(meta.get("ranking_output_path") or "")
            job.news_count = int(meta.get("news_event_count") or 0)

            selected_users = user_ids or get_active_user_ids(db_path=db_path, output_dir=output_dir)
            if not selected_users:
                selected_users = ["default"]
                job.warnings.append("no active users found; default user was used for dry scheduler validation.")
            user_task_runner = user_task_runner or run_user_daily_job
            user_results: dict[str, Any] = {}
            for user_id in selected_users:
                def _run_user(user_id=user_id):
                    return user_task_runner(
                        user_id=user_id,
                        trade_date=resolved_trade_date,
                        output_dir=output_dir,
                        db_path=db_path,
                        top_k=top_k,
                        dry_run=dry_run,
                        skip_news=skip_news,
                        skip_paper_trading=skip_paper_trading,
                        force=force,
                        job_id=job.job_id,
                        run_id=job.run_id,
                        execution_source=job.execution_source,
                    )

                result = run_recorded_step(job, f"user:{user_id}", _run_user, root=root, allow_failure=True)
                user_results[str(user_id)] = result
                if result.get("status") == SchedulerStatus.FAILED:
                    continue
                job.recommendation_count += int(result.get("recommendation_count") or 0)
                job.paper_order_count += int(result.get("paper_order_count") or 0)
                job.position_count += int(result.get("position_count") or 0)
                if result.get("report_path"):
                    job.report_path = str(result["report_path"])
            job.user_task_status = user_results
            failed_users = [uid for uid, item in user_results.items() if item.get("status") == SchedulerStatus.FAILED]
            if failed_users:
                job.overall_status = SchedulerStatus.PARTIAL_SUCCESS if len(failed_users) < len(user_results) else SchedulerStatus.FAILED
            elif public_step_status == SchedulerStatus.PARTIAL_SUCCESS:
                job.overall_status = SchedulerStatus.PARTIAL_SUCCESS
            else:
                job.overall_status = SchedulerStatus.SUCCESS
    except JobLockError as exc:
        job.overall_status = SchedulerStatus.SKIPPED
        job.failed_steps.append("job_lock")
        job.warnings.append(str(exc))
        _append_log(resolved_trade_date, f"lock skipped: {exc}")
    except Exception as exc:
        job.overall_status = SchedulerStatus.FAILED
        if str(exc) not in job.warnings:
            job.warnings.append(str(exc))
        _append_log(resolved_trade_date, f"job failed: {type(exc).__name__}: {exc}")
    finally:
        job.finished_at = now_text()
        job.duration_seconds = round(perf_counter() - started, 3)
        save_job_status(job, root)
        _append_log(resolved_trade_date, f"job finished status={job.overall_status}")
    return job
