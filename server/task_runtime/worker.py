from __future__ import annotations

import argparse
import base64
import json
import os
import threading
import time
import traceback

from server.task_runtime.handlers import execute_task
from server.task_runtime.store import TERMINAL_STATUSES, TaskStore, utc_now


def _parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()

    # Expose the task identifier to lower-level observability such as prompt dumps.
    os.environ["STOCK_AGENT_TASK_ID"] = str(args.task_id)

    store = TaskStore()
    task = store.get(args.task_id)
    secret_raw = os.environ.pop("STOCK_TASK_SECRET_B64", "")
    secrets: dict[str, str] = {}
    if secret_raw:
        try:
            secrets = json.loads(base64.b64decode(secret_raw.encode("ascii")).decode("utf-8"))
        except Exception:
            secrets = {}

    stop_watch = threading.Event()

    def parent_watch() -> None:
        while not stop_watch.wait(2):
            if args.parent_pid and not _parent_alive(args.parent_pid):
                os._exit(75)

    threading.Thread(target=parent_watch, name="task-parent-watch", daemon=True).start()

    def cancelled() -> bool:
        try:
            return bool(store.get(args.task_id).get("cancel_requested"))
        except Exception:
            return False

    def emit(event_type: str, data: dict) -> None:
        payload = dict(data or {})
        update = {}
        if "progress" in payload:
            update["progress"] = max(0.0, min(float(payload["progress"]), 0.99))
        if payload.get("message"):
            update["message"] = str(payload["message"])
        if update:
            store.update(args.task_id, **update)
        store.add_event(args.task_id, event_type, payload)

    attempt = int(task.get("attempt") or 0)
    max_retries = int(task.get("max_retries") or 0)
    try:
        store.update(
            args.task_id,
            status="running",
            started_at=task.get("started_at") or utc_now(),
            message="任务正在运行",
            worker_pid=os.getpid(),
        )
        store.add_event(args.task_id, "started", {"message": "任务 Worker 已启动", "worker_pid": os.getpid()})
        heartbeat_stop = threading.Event()
        heartbeat_started = time.monotonic()

        def heartbeat() -> None:
            while not heartbeat_stop.wait(3):
                try:
                    current = store.get(args.task_id)
                    if current.get("status") not in {"queued", "running", "cancelling"}:
                        return
                    elapsed = time.monotonic() - heartbeat_started
                    timeout_seconds = max(1, int(current.get("timeout_seconds") or 99600))
                    current_progress = float(current.get("progress") or 0)
                    inferred = min(0.9, max(current_progress, elapsed / timeout_seconds * 0.85))
                    store.update(args.task_id, progress=inferred)
                    store.add_event(args.task_id, "heartbeat", {
                        "progress": inferred,
                        "elapsed_seconds": round(elapsed, 1),
                        "message": str(current.get("message") or "任务正在运行"),
                    })
                except Exception:
                    return

        threading.Thread(target=heartbeat, name="task-heartbeat", daemon=True).start()
        while True:
            store.update(args.task_id, attempt=attempt)
            try:
                result = execute_task(
                    str(task["task_type"]),
                    dict(task.get("request") or {}),
                    emit=emit,
                    is_cancelled=cancelled,
                    secrets=secrets,
                    attempt=attempt,
                )
                if cancelled():
                    store.update(args.task_id, status="cancelled", finished_at=utc_now(), progress=1, message="任务已取消")
                    store.add_event(args.task_id, "cancelled", {"message": "任务已取消"})
                    return 2
                store.update(args.task_id, status="succeeded", finished_at=utc_now(), progress=1, message="任务已完成", result=result, worker_pid=None)
                store.add_event(args.task_id, "succeeded", {"message": "任务已完成", "progress": 1})
                return 0
            except InterruptedError as exc:
                store.update(args.task_id, status="cancelled", finished_at=utc_now(), progress=1, message=str(exc), worker_pid=None)
                store.add_event(args.task_id, "cancelled", {"message": str(exc)})
                return 2
            except Exception as exc:
                if attempt < max_retries and not cancelled():
                    attempt += 1
                    store.update(args.task_id, attempt=attempt, message=f"任务失败，准备第 {attempt} 次重试")
                    store.add_event(args.task_id, "retry", {"attempt": attempt, "message": str(exc)})
                    time.sleep(min(2 ** attempt, 10))
                    continue
                error = {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "traceback_tail": traceback.format_exc()[-6000:],
                }
                store.update(args.task_id, status="failed", finished_at=utc_now(), progress=1, message=str(exc), error=error, worker_pid=None)
                store.add_event(args.task_id, "failed", error)
                return 1
    finally:
        stop_watch.set()
        try:
            heartbeat_stop.set()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
