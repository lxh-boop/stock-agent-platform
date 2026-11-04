from __future__ import annotations

import re
from typing import Any


SENSITIVE_KEY_MARKERS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "header",
    "password",
    "secret",
    "token",
}

SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|secret|password)\s*[:=]\s*([^\s,;]+)"
)


def redact_sensitive(value: Any, *, max_chars: int = 1000) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if any(marker in text_key.lower() for marker in SENSITIVE_KEY_MARKERS):
                redacted[text_key] = "***"
            else:
                redacted[text_key] = redact_sensitive(item, max_chars=max_chars)
        return redacted
    if isinstance(value, list):
        items = [redact_sensitive(item, max_chars=max_chars) for item in value[:50]]
        if len(value) > 50:
            items.append({"truncated_count": len(value) - 50})
        return items
    if isinstance(value, str):
        text = SECRET_VALUE_PATTERN.sub(r"\1=***", value)
        return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"
    return value


def truncate_external_text(value: Any, *, max_chars: int = 4000) -> Any:
    if isinstance(value, dict):
        return {str(key): truncate_external_text(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [truncate_external_text(item, max_chars=max_chars) for item in value[:100]]
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"
    return value


def safe_external_payload(value: Any, *, max_chars: int = 4000) -> Any:
    return redact_sensitive(truncate_external_text(value, max_chars=max_chars), max_chars=max_chars)


def is_write_like_tool(tool_name: str, description: str = "", annotations: dict[str, Any] | None = None) -> bool:
    annotations = dict(annotations or {})
    read_only = annotations.get(
        "readOnlyHint",
        annotations.get("read_only_hint"),
    )
    destructive = annotations.get(
        "destructiveHint",
        annotations.get("destructive_hint"),
    )
    if read_only is False or destructive is True:
        return True
    if read_only is True and destructive is False:
        return False
    text = f"{tool_name} {description}".lower()
    markers = [
        "write",
        "trade",
        "execute",
        "commit",
        "update",
        "delete",
        "order",
        "buy",
        "sell",
        "rebalance",
        "modify",
    ]
    if any(marker in text for marker in markers):
        return True
    return False
