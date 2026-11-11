"""所有登录用户共用同一业务入口，但只能提交/执行本人的日更。"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Any

from core.user_scope import current_owner, user_scope, validate_owner, workspace
from database.connection import get_connection

DAILY_TASK_TYPE = "paper-profile.scheduler-manual"


@contextmanager
def account_lock(user_id: str, purpose: str):
    """数据库会话锁，进程退出后自动释放；force 不能绕过正在执行的任务。"""
    conn = get_connection(scoped=False)
    lock_name = f"account-daily:{purpose}:{validate_owner(user_id)}"
    try:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired", (lock_name,)
        ).fetchone()
        conn.commit()
        if not row or not row["acquired"]:
            raise RuntimeError("你的日更任务已在运行，请先查看任务进度。")
        yield
    finally:
        # 关闭连接即释放 session advisory lock，不依赖事务提交。
        conn.close()


def submit_personal_daily(user_id: str, *, source: str = "manual", slot: str = "",
                          session_id: str = "", force: bool = True) -> dict[str, Any]:
    user = validate_owner(user_id)
    from application.user_credentials_service import get_user_tushare_token
    from server.api.tasks import task_manager
    with user_scope(user), account_lock(user, "submit"):
        # 尽早报告缺失凭据，但 Token 不写入 task request 或 metadata。
        if source == "manual":
            get_user_tushare_token(user)
        active = task_manager.store.list(owner_id=user, task_type=DAILY_TASK_TYPE, active_only=True, limit=1)
        if active:
            return active[0]
        if slot:
            previous = task_manager.store.list(owner_id=user, task_type=DAILY_TASK_TYPE, limit=200)
            for task in previous:
                if (task.get("metadata") or {}).get("schedule_slot") == slot:
                    return task
        return task_manager.submit(
            task_type=DAILY_TASK_TYPE,
            args=[], kwargs={"user_id": user, "all_users": False, "force": bool(force), "source": source},
            owner_id=user, session_id=str(session_id),
            metadata={"scope": "account", "source": source, "schedule_slot": slot},
            timeout_seconds=997200, max_retries=0,
        )


def run_personal_daily(user_id: str, *, force: bool = True, source: str = "manual") -> dict[str, Any]:
    user = validate_owner(user_id)
    if current_owner() != user:
        raise ValueError("daily_task_owner_mismatch")
    # 在 Task Worker 进程中执行，因此 cwd 与继承到子进程的身份都已经隔离。
    from application.user_credentials_service import get_user_tushare_token
    from data_tushare import build_market_data_window, init_tushare_pro
    from scheduler.daily_worker import run_scheduled_daily_update
    from scheduler.runtime_scheduler import _safe_scheduler_error

    with account_lock(user, "execute"):
        token = get_user_tushare_token(user)
        window = build_market_data_window(init_tushare_pro(token))
        signal_date = str(window["expected_signal_date"])
        result = run_scheduled_daily_update(
            trade_date=signal_date, user_ids=[user], force=bool(force),
            source=str(source), output_dir=workspace(user) / "outputs", root=workspace(user),
        ).to_dict()
        result["owner_id"] = user
        if result.get("overall_status") != "success":
            errors = []
            for name in result.get("failed_steps") or []:
                step = (result.get("step_status") or {}).get(name) or {}
                errors.append(f"{name}: {step.get('error_message') or step.get('error_type') or '失败'}")
            reason = "; ".join(errors or result.get("warnings") or ["日更未完成"])
            raise RuntimeError(_safe_scheduler_error(reason))
        return result
