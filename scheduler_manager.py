import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from local_config import load_local_config, save_local_config
from scheduler.runtime_scheduler import reload_runtime_scheduler, start_runtime_scheduler
from storage_governance.lifecycle import append_bounded_text_log

from runtime_paths import (
    ensure_runtime_directories,
    get_logs_dir,
    get_resource_root,
    get_user_data_root,
    is_frozen_app,
)

ensure_runtime_directories()
BASE_DIR = get_resource_root()
RUN_CWD = get_user_data_root() if is_frozen_app() else BASE_DIR
ROLLING_UPDATE_SCRIPT = BASE_DIR / "daily_incremental_update.py"
LOG_DIR = get_logs_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_EXTERNAL_BACKEND = "zoo:chronos_bolt_small"
DFT_UNET_BACKEND = "dft_unet_external"


def write_log(text: str):
    log_path = LOG_DIR / "auto_retrain.log"
    # Keep the original scheduler log owner and path; only bound its lifecycle.
    append_bounded_text_log(log_path, text)


def run_command(cmd):
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        cmd,
        cwd=str(RUN_CWD),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=child_env,
    )

    return result


def mask_sensitive_command(cmd):
    masked = []

    for i, part in enumerate(cmd):
        if i > 0 and cmd[i - 1] == "--token":
            masked.append("***")
        else:
            masked.append(part)

    return masked


def auto_retrain_job(
    token: str,
    version: str = "latest",
    model_backend: str = DEFAULT_EXTERNAL_BACKEND,
    checkpoint_path: str | None = None,
):
    """
    每日自动任务：基于已有模型执行增量更新并刷新最新排名。
    """

    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    write_log("=" * 100)
    write_log(f"[Auto Daily Update Start] {start_time}")

    if not token:
        write_log("[Error] Tushare token is empty. Skip auto daily update.")
        return

    update_cmd = [sys.executable]
    if is_frozen_app():
        update_cmd.append("--daily-update-child")
    else:
        update_cmd.append(str(ROLLING_UPDATE_SCRIPT))
    update_cmd.extend([
        "--token",
        token,
        "--base-version",
        version,
        "--model-backend",
        model_backend,
    ])
    if model_backend == DFT_UNET_BACKEND and checkpoint_path:
        update_cmd.extend(["--checkpoint-path", checkpoint_path])

    write_log(f"[Run] {' '.join(mask_sensitive_command(update_cmd))}")

    update_result = run_command(update_cmd)

    write_log("[Daily Update STDOUT]")
    write_log(update_result.stdout)

    if update_result.stderr:
        write_log("[Daily Update STDERR]")
        write_log(update_result.stderr)

    if update_result.returncode != 0:
        write_log(
            f"[Error] daily_incremental_update.py failed, "
            f"returncode={update_result.returncode}"
        )
        return

    write_log("[Daily Update Success]")

    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_log(f"[Auto Daily Update Finished] {end_time}")
    write_log("=" * 100)


def create_scheduler():
    """兼容旧调用：返回 FastAPI 进程内的唯一常驻调度器。"""

    return start_runtime_scheduler()


def set_daily_retrain_job(
    scheduler: BackgroundScheduler | None,
    token: str,
    hour: int,
    minute: int,
    enabled: bool,
    model_backend: str = DEFAULT_EXTERNAL_BACKEND,
    checkpoint_path: str | None = None,
):
    """兼容旧调用并把计划写入统一本地配置。

    任务实际执行时重新读取 Token，避免把敏感值固化到 APScheduler Job。
    """

    config = load_local_config()
    config.update(
        {
            "auto_retrain_enabled": bool(enabled),
            "auto_retrain_hour": max(0, min(23, int(hour))),
            "auto_retrain_minute": max(0, min(59, int(minute))),
            "auto_retrain_timezone": "Asia/Shanghai",
            "auto_retrain_catch_up": True,
            "model_backend": str(model_backend or DEFAULT_EXTERNAL_BACKEND),
            "dft_unet_checkpoint_path": str(checkpoint_path or ""),
        }
    )
    if str(token or "").strip():
        config["tushare_token"] = str(token).strip()
    save_local_config(config)
    return reload_runtime_scheduler()


def get_scheduler_jobs(scheduler: BackgroundScheduler | None):
    jobs = []
    if scheduler is None:
        return jobs

    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "next_run_time": str(job.next_run_time),
            "name": job.name,
        })

    return jobs


def read_auto_retrain_log(max_chars: int = 12000) -> str:
    log_path = LOG_DIR / "auto_retrain.log"

    if not log_path.exists():
        return ""

    text = log_path.read_text(encoding="utf-8", errors="ignore")

    if len(text) > max_chars:
        return text[-max_chars:]

    return text
