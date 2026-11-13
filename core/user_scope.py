"""服务端认证身份驱动的数据域；请求间不修改进程全局环境或工作目录。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
from pathlib import Path
import re
from typing import Iterator

_OWNER: ContextVar[str | None] = ContextVar("stock_data_owner", default=None)
_SOURCE = Path(__file__).resolve().parents[1]
_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")


def validate_owner(value: str) -> str:
    user = str(value or "").strip()
    if not _USER.fullmatch(user):
        raise ValueError("authenticated_user_required")
    return user


def current_owner() -> str:
    explicit = _OWNER.get()
    value = explicit if explicit is not None else os.environ.get("STOCK_DATA_OWNER_ID", "")
    return validate_owner(value) if value else ""


@contextmanager
def user_scope(user_id: str) -> Iterator[None]:
    token = _OWNER.set(validate_owner(user_id))
    try:
        yield
    finally:
        _OWNER.reset(token)


@contextmanager
def system_scope() -> Iterator[None]:
    token = _OWNER.set("")
    try:
        yield
    finally:
        _OWNER.reset(token)


def user_schema(user_id: str) -> str:
    # 不拼接客户端传来的 SQL 标识符；大小写不同的用户名也不能发生碰撞。
    digest = hashlib.sha256(validate_owner(user_id).encode()).hexdigest()[:32]
    return "stock_ud_" + digest


def workspace(user_id: str | None = None) -> Path:
    user = validate_owner(user_id or current_owner())
    root = Path(os.environ.get("STOCK_USER_WORKSPACE_ROOT", str(_SOURCE / "runtime" / "users")))
    return root.expanduser().resolve() / user


def owned_path(value: str | Path) -> Path:
    """将旧 data/outputs/runtime/logs 路径映射到当前账户，其他资源保持只读共享。"""
    path = Path(value)
    if not current_owner():
        return path
    root = workspace()
    # 调用方已经给出本人的绝对路径时不要重复嵌套。
    if path.is_absolute():
        resolved = path.resolve()
        if resolved.is_relative_to(root):
            return resolved
        if resolved.is_relative_to(root.parent):
            raise ValueError("cross_user_path_forbidden")
        try:
            relative = resolved.relative_to(_SOURCE)
        except ValueError:
            return path
    else:
        relative = Path(str(path).replace("\\", "/"))
    if str(relative) == ".":
        return root
    if relative.parts and relative.parts[0] in {"data", "outputs", "runtime", "logs"}:
        result = (root / relative).resolve()
        if not result.is_relative_to(root):
            raise ValueError("cross_user_path_forbidden")
        return result
    # 模型权重不复制，但每次日更产生的 metrics 不能写回公共模型目录。
    if relative.parts and relative.parts[0] == "models" and relative.name == "metrics.json":
        return root / "outputs" / "model_metrics" / Path(*relative.parts[1:])
    return path


def worker_environment(user_id: str, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["STOCK_DATA_OWNER_ID"] = validate_owner(user_id)
    env["PYTHONPATH"] = str(_SOURCE) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # 子进程必须按本人账号取 Token，不能隐式借用部署环境中的 Key。
    for name in ("TUSHARE_TOKEN", "STOCK_TUSHARE_TOKEN_FILE", "LLM_API_KEY", "OPENAI_API_KEY", "STOCK_LLM_API_KEY_FILE", "LLM_BASE_URL", "LLM_MODEL"):
        env.pop(name, None)
    return env
