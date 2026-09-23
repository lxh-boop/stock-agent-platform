from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from portfolio.schemas import now_text

# user_id 会成为 outputs/agent_sessions/<user_id> 目录名，禁止路径分隔符。
_SAFE_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class ConversationState:
    session_id: str = field(default_factory=lambda: f"agent_session_{uuid4().hex[:10]}")
    user_id: str = "default"
    selected_stock_code: str = ""
    current_intent: str = ""
    pending_plan_id: str = ""
    pending_confirmation_token: str = ""
    last_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now_text)
    updated_at: str = field(default_factory=now_text)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def session_dir(user_id: str, output_dir: str | Path = "outputs") -> Path:
    text = str(user_id or "")
    if not _SAFE_USER_ID.fullmatch(text):
        raise ValueError(f"unsafe_user_id:{text[:64]}")
    return Path(output_dir) / "agent_sessions" / text
