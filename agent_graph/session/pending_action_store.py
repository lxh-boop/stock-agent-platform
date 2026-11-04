from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_graph.session.conversation_state import session_dir


def _path(user_id: str, output_dir: str | Path = "outputs") -> Path:
    return session_dir(user_id, output_dir) / "pending_actions.json"


def load_pending_actions(user_id: str, output_dir: str | Path = "outputs") -> dict[str, dict[str, Any]]:
    path = _path(user_id, output_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_pending_actions(user_id: str, actions: dict[str, dict[str, Any]], output_dir: str | Path = "outputs") -> Path:
    path = _path(user_id, output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(actions, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def save_pending_plan(user_id: str, plan: dict[str, Any], output_dir: str | Path = "outputs") -> dict[str, Any]:
    actions = load_pending_actions(user_id, output_dir)
    actions[str(plan["plan_id"])] = dict(plan)
    save_pending_actions(user_id, actions, output_dir)
    return plan


def get_pending_plan(user_id: str, plan_id: str, output_dir: str | Path = "outputs") -> dict[str, Any] | None:
    return load_pending_actions(user_id, output_dir).get(str(plan_id))


def update_pending_plan(user_id: str, plan_id: str, changes: dict[str, Any], output_dir: str | Path = "outputs") -> dict[str, Any]:
    actions = load_pending_actions(user_id, output_dir)
    plan = dict(actions.get(str(plan_id)) or {})
    if not plan:
        raise ValueError(f"pending plan not found: {plan_id}")
    plan.update(changes)
    actions[str(plan_id)] = plan
    save_pending_actions(user_id, actions, output_dir)
    return plan
