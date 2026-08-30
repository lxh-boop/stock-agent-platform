from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from core.config.service_settings import get_service_settings

from server.task_runtime.store import ACTIVE_STATUSES, TERMINAL_STATUSES, TaskStore, utc_now

ALLOWED_TASK_TYPES = {
    "diagnostic.sleep",
    "diagnostic.flaky",
    "agent.run",
    "dashboard.rolling_update",
    "dashboard.backtest",
    "paper-trading.update",
    "paper-trading.backfill",
    "paper-profile.ai-news-adjustment",
    "paper-profile.scheduler-manual",
}


class TaskManager:
    def __init__(self, db_path: str | Path | None = None) -> None:
        del db_path
        self.store = TaskStore()
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        # IMPORTANT: construction must be side-effect free for persisted task state.
        # Tests, helper scripts, or a second in-process manager may construct
        # TaskManager while the real API owns live tasks. Startup recovery is
        # therefore explicit and is invoked only by the real API lifespan.
        self._startup_recovery_done = False

    def recover_on_api_startup(self) -> list[str]:
        """Mark stale active tasks interrupted exactly once for this API manager.

        This method is intentionally explicit. Merely importing task modules or
        constructing a helper/test TaskManager must never mutate production task
        state. The real FastAPI lifespan owns startup recovery.
        """
        with self._lock:
            if self._startup_recovery_done:
                return []
            interrupted = self.store.recover_interrupted()
            self._startup_recovery_done = True
            return interrupted

    def submit(
        self,
        *,
        task_type: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        owner_id: str = "",
        session_id: str = "",
        metadata: dict[str, Any] | None = None,
        timeout_seconds: int = 99600,
        max_retries: int = 0,
        secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        task_type = str(task_type)
        if task_type not in ALLOWED_TASK_TYPES:
            raise KeyError(f"Task type is not allowed: {task_type}")
        max_concurrent = get_service_settings().max_concurrent_tasks
        with self._lock:
            active_count = len(self.store.list(active_only=True, limit=200))
            if active_count >= max_concurrent:
                raise RuntimeError(
                    f"Task concurrency limit reached: {active_count}/{max_concurrent}"
                )
        task_id = f"task_{uuid.uuid4().hex}"
        self.store.create(
            task_id=task_id,
            task_type=task_type,
            request={"args": list(args or []), "kwargs": dict(kwargs or {})},
            owner_id=str(owner_id or ""),
            session_id=str(session_id or ""),
            metadata=dict(metadata or {}),
            timeout_seconds=max(1, int(timeout_seconds)),
            max_retries=max(0, int(max_retries)),
        )
        env = os.environ.copy()
        if secrets:
            env["STOCK_TASK_SECRET_B64"] = base64.b64encode(
                json.dumps(secrets, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
        command = [
            sys.executable,
            "-m",
            "server.task_runtime.worker",
            "--task-id",
            task_id,
            "--parent-pid",
            str(os.getpid()),
        ]
        creationflags = 0
        start_new_session = False
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            start_new_session = True
        process = subprocess.Popen(
            command,
            cwd=str(Path.cwd()),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        self.store.update(task_id, worker_pid=int(process.pid))
        with self._lock:
            self._processes[task_id] = process
        threading.Thread(
            target=self._monitor,
            args=(task_id, process),
            name=f"task-monitor-{task_id[-8:]}",
            daemon=True,
        ).start()
        return self.store.get(task_id)

    def _terminate_tree(self, process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _llm_descriptor(task: dict[str, Any]) -> dict[str, Any]:
        request = task.get("request") if isinstance(task, dict) else {}
        kwargs = request.get("kwargs") if isinstance(request, dict) else {}
        descriptor = kwargs.get("llm_settings_descriptor") if isinstance(kwargs, dict) else {}
        return dict(descriptor) if isinstance(descriptor, dict) else {}

    @staticmethod
    def _native_ollama_base_url(base_url: str) -> str:
        parsed = urlsplit(str(base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        path = str(parsed.path or "").rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))

    def _other_task_uses_local_model(
        self,
        *,
        task_id: str,
        descriptor: dict[str, Any],
    ) -> bool:
        model = str(descriptor.get("model") or "").strip().lower()
        base_url = self._native_ollama_base_url(str(descriptor.get("base_url") or "")).lower()
        if not model or not base_url:
            return False
        for item in self.store.list(active_only=True, limit=200):
            if str(item.get("task_id") or "") == str(task_id):
                continue
            other = self._llm_descriptor(item)
            if str(other.get("mode") or "").strip().lower() != "local":
                continue
            if str(other.get("model") or "").strip().lower() != model:
                continue
            if self._native_ollama_base_url(str(other.get("base_url") or "")).lower() == base_url:
                return True
        return False

    def _clear_local_model_cache(
        self,
        *,
        task_id: str,
        descriptor: dict[str, Any],
    ) -> None:
        if str(descriptor.get("mode") or "").strip().lower() != "local":
            return
        model = str(descriptor.get("model") or "").strip()
        base_url = self._native_ollama_base_url(str(descriptor.get("base_url") or ""))
        if not model or not base_url:
            self.store.add_event(
                task_id,
                "ollama_cache_clear_failed",
                {"message": "Local model descriptor is incomplete"},
            )
            return
        if self._other_task_uses_local_model(task_id=task_id, descriptor=descriptor):
            self.store.add_event(
                task_id,
                "ollama_cache_clear_skipped",
                {
                    "message": "The same local model is still used by another active task",
                    "model": model,
                },
            )
            return

        self.store.add_event(
            task_id,
            "ollama_cache_clear_requested",
            {"message": "Requesting Ollama runner unload", "model": model},
        )
        payload = json.dumps(
            {
                "model": model,
                "prompt": "",
                "stream": False,
                "keep_alive": 0,
            }
        ).encode("utf-8")
        request = Request(
            f"{base_url}/api/generate",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=12) as response:  # nosec B310: descriptor is server-built
                response.read()
            self.store.add_event(
                task_id,
                "ollama_cache_cleared",
                {
                    "message": "Ollama runner unload request completed",
                    "model": model,
                },
            )
        except (HTTPError, URLError, OSError) as exc:
            self.store.add_event(
                task_id,
                "ollama_cache_clear_failed",
                {
                    "message": f"Ollama runner unload failed: {type(exc).__name__}",
                    "model": model,
                },
            )

    def _monitor(self, task_id: str, process: subprocess.Popen[Any]) -> None:
        started = time.monotonic()
        try:
            while process.poll() is None:
                task = self.store.get(task_id)
                if task.get("cancel_requested"):
                    self._terminate_tree(process)
                    current = self.store.get(task_id)
                    if current["status"] not in TERMINAL_STATUSES:
                        self.store.update(task_id, status="cancelled", finished_at=utc_now(), progress=1, message="任务已取消", worker_pid=None)
                        self.store.add_event(task_id, "cancelled", {"message": "任务进程已终止"})
                    return
                if time.monotonic() - started > int(task.get("timeout_seconds") or 99600):
                    self._terminate_tree(process)
                    current = self.store.get(task_id)
                    if current["status"] not in TERMINAL_STATUSES:
                        self.store.update(task_id, status="timed_out", finished_at=utc_now(), progress=1, message="任务执行超时", worker_pid=None)
                        self.store.add_event(task_id, "timed_out", {"message": "任务超过服务端超时限制，进程已终止"})
                    return
                time.sleep(0.5)
            current = self.store.get(task_id)
            if current["status"] not in TERMINAL_STATUSES:
                code = int(process.returncode or 0)
                self.store.update(
                    task_id,
                    status="failed" if code else "interrupted",
                    finished_at=utc_now(),
                    progress=1,
                    message=f"任务 Worker 异常退出，返回码 {code}",
                    error={"code": "WORKER_EXIT", "message": f"Worker exited with code {code}"},
                    worker_pid=None,
                )
                self.store.add_event(task_id, "worker_exit", {"returncode": code})
        finally:
            with self._lock:
                self._processes.pop(task_id, None)

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.store.request_cancel(task_id)
        descriptor = self._llm_descriptor(task)
        with self._lock:
            process = self._processes.get(str(task_id))

        if process is not None and process.poll() is None:
            self.store.add_event(
                task_id,
                "worker_termination_requested",
                {"message": "Terminating the task process tree", "worker_pid": process.pid},
            )
            self._terminate_tree(process)

        current = self.store.get(task_id)
        if current.get("status") not in TERMINAL_STATUSES:
            self.store.update(
                task_id,
                status="cancelled",
                finished_at=utc_now(),
                progress=1,
                message="任务已取消",
                worker_pid=None,
            )
            self.store.add_event(
                task_id,
                "cancelled",
                {"message": "任务进程已终止"},
            )

        # Closing the worker process aborts the in-flight HTTP request. The
        # keep_alive=0 request then unloads Ollama's runner and KV cache without
        # deleting the model files. API-mode tasks never call this branch.
        self._clear_local_model_cache(task_id=task_id, descriptor=descriptor)
        return self.store.get(task_id)

