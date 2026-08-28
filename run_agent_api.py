from __future__ import annotations

import os
from pathlib import Path

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent


def _reload_directories() -> list[str]:
    """Return only source-code directories that should trigger API reloads.

    Runtime databases, logs, generated Markdown, model files and data folders are
    intentionally outside this list. Watching the whole ``/app`` bind mount can
    make WatchFiles subscribe to ``/app/runtime`` and fail with an OS-level I/O
    watcher error on Docker Desktop for Windows.
    """

    candidates = (
        "agent",
        "server",
        "core",
        "application",
        "database",
        "pipelines",
        "scoring",
        "portfolio",
        "rag",
        "skills",
    )
    return [
        str(path)
        for name in candidates
        if (path := PROJECT_ROOT / name).is_dir()
    ]


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def main() -> None:
    # 本地开发可显式开启 reload；Docker 默认关闭。
    # 这样真正的 FastAPI Server 子进程如果 import 失败，容器会直接失败，
    # 不会再出现“Docker 仍显示 Running，但实际 API 已经死掉”的假健康状态。
    reload_enabled = _env_truthy("AGENT_API_RELOAD", default=False)
    reload_dirs = _reload_directories() if reload_enabled else []
    if reload_enabled and not reload_dirs:
        raise RuntimeError(
            f"No API source directories were found under {PROJECT_ROOT}."
        )

    uvicorn.run(
        "server.api.main:app",
        host=os.environ.get("AGENT_API_HOST", "127.0.0.1"),
        port=int(os.environ.get("AGENT_API_PORT", "8010")),
        reload=reload_enabled,
        reload_dirs=reload_dirs or None,
        reload_includes=["*.py"] if reload_enabled else None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
