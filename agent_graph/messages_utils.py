"""Shared message-summarizing helpers for agent loops."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage


def json_view(value: Any, limit: int = 8000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:limit]


def extract_entity_refs(obj: Any, depth: int = 0, seen: set[int] | None = None) -> list[dict[str, Any]]:
    """Recursively collect GraphRef-shaped dicts (authoritative ids) from payloads."""

    if depth > 6:
        return []
    seen = seen if seen is not None else set()
    if isinstance(obj, dict):
        marker = id(obj)
        if marker in seen:
            return []
        seen.add(marker)
        if "node_id" in obj and ("entity_type" in obj or "graph_id" in obj or "kind" in obj):
            return [obj]
        refs: list[dict[str, Any]] = []
        for value in obj.values():
            refs.extend(extract_entity_refs(value, depth + 1, seen))
        return refs
    if isinstance(obj, list):
        refs: list[dict[str, Any]] = []
        for item in obj:
            refs.extend(extract_entity_refs(item, depth + 1, seen))
        return refs
    return []


def summarize_messages(messages: list[Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (final_text, tool_calls, entity_refs) from one agent-loop message list."""

    final_text = ""
    tool_calls: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str) and content.strip():
                final_text = content.strip()
            elif isinstance(content, list):
                joined = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                ).strip()
                if joined:
                    final_text = joined
            for call in getattr(message, "tool_calls", None) or []:
                tool_calls.append({"tool": call.get("name"), "args": call.get("args")})
        else:
            payload = getattr(message, "content", None)
            if isinstance(payload, str) and payload:
                try:
                    parsed = json.loads(payload)
                except Exception:  # noqa: BLE001
                    parsed = payload
                refs.extend(extract_entity_refs(parsed)[:4])
    return final_text, tool_calls, refs


__all__ = ["json_view", "extract_entity_refs", "summarize_messages"]
