from __future__ import annotations

from pathlib import Path

import uvicorn

from core.config.service_settings import get_service_settings


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


def main() -> None:
    settings = get_service_settings()
    # 本地开发可显式开启 reload；Docker 默认关闭。
    # 这样真正的 FastAPI Server 子进程如果 import 失败，容器会直接失败，
    # 不会再出现“Docker 仍显示 Running，但实际 API 已经死掉”的假健康状态。
    reload_enabled = settings.api_reload
    reload_dirs = _reload_directories() if reload_enabled else []
    if reload_enabled and not reload_dirs:
        raise RuntimeError(
            f"No API source directories were found under {PROJECT_ROOT}."
        )

    uvicorn.run(
        "server.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=reload_enabled,
        reload_dirs=reload_dirs or None,
        reload_includes=["*.py"] if reload_enabled else None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
