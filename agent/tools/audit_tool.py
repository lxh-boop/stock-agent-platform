from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from database.schemas import json_dumps
from database.postgres_store import PostgresStore

from agent.tools._common import now_text, to_serializable


def _audit_dir(output_dir: str | Path, user_id: str) -> Path:
    return Path(output_dir) / "agent_audit" / str(user_id)


def _append_jsonl(path: Path, record: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(to_serializable(record), ensure_ascii=False, default=str) + "\n")
    return path


def write_agent_action_log(
    user_id: str,
    intent: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    tool_output_summary: dict[str, Any] | str | None = None,
    plan_id: str = "",
    confirmation_status: str = "",
    execution_status: str = "",
    decision_source: str = "agent_control_center",
    trade_date: str = "",
    session_id: str = "",
    error_message: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    record = {
        "action_id": action_id or f"agent_action_{uuid4().hex[:12]}",
        "session_id": session_id,
        "user_id": str(user_id or "default"),
        "intent": str(intent or ""),
        "tool_name": str(tool_name or ""),
        "tool_input": to_serializable(tool_input or {}),
        "tool_output_summary": to_serializable(tool_output_summary or {}),
        "plan_id": str(plan_id or ""),
        "confirmation_status": str(confirmation_status or ""),
        "execution_status": str(execution_status or ""),
        "decision_source": str(decision_source or "agent_control_center"),
        "trade_date": str(trade_date or ""),
        "created_at": now_text(),
        "confirmed_at": "",
        "executed_at": now_text() if execution_status in {"executed", "executed_no_order"} else "",
        "error_message": str(error_message or ""),
    }
    _append_jsonl(_audit_dir(output_dir, record["user_id"]) / "agent_action_log.jsonl", record)
    try:
        db_record = dict(record)
        for key in ["tool_input", "tool_output_summary"]:
            db_record[key] = json_dumps(db_record[key])
        PostgresStore().upsert("agent_action_log", db_record)
    except Exception:
        pass
    return record


def write_agent_tool_call_log(
    user_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    tool_output_summary: dict[str, Any] | str | None = None,
    status: str = "success",
    session_id: str = "",
    error_message: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    record = {
        "call_id": f"agent_tool_call_{uuid4().hex[:12]}",
        "session_id": session_id,
        "user_id": str(user_id or "default"),
        "tool_name": str(tool_name or ""),
        "tool_input": to_serializable(tool_input or {}),
        "tool_output_summary": to_serializable(tool_output_summary or {}),
        "status": str(status or "success"),
        "created_at": now_text(),
        "error_message": str(error_message or ""),
    }
    _append_jsonl(_audit_dir(output_dir, record["user_id"]) / "agent_tool_call_log.jsonl", record)
    try:
        db_record = dict(record)
        for key in ["tool_input", "tool_output_summary"]:
            db_record[key] = json_dumps(db_record[key])
        PostgresStore().upsert("agent_tool_call_log", db_record)
    except Exception:
        pass
    return record


def write_agent_confirmation_log(
    user_id: str,
    plan_id: str,
    confirmation_status: str,
    expires_at: str = "",
    session_id: str = "",
    token_hash: str = "",
    error_message: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    record = {
        "confirmation_id": f"agent_confirmation_{uuid4().hex[:12]}",
        "session_id": session_id,
        "user_id": str(user_id or "default"),
        "plan_id": str(plan_id or ""),
        "confirmation_token_hash": str(token_hash or ""),
        "confirmation_status": str(confirmation_status or ""),
        "created_at": now_text(),
        "confirmed_at": now_text() if confirmation_status == "confirmed" else "",
        "expires_at": str(expires_at or ""),
        "error_message": str(error_message or ""),
    }
    _append_jsonl(_audit_dir(output_dir, record["user_id"]) / "agent_confirmation_log.jsonl", record)
    try:
        PostgresStore().upsert("agent_confirmation_log", record)
    except Exception:
        pass
    return record


def read_agent_action_logs(user_id: str, output_dir: str | Path = "outputs") -> list[dict[str, Any]]:
    path = _audit_dir(output_dir, str(user_id or "default")) / "agent_action_log.jsonl"
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows
