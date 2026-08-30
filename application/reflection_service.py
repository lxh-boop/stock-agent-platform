from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.reflection import CriticSanitizer, ReflectionStore


FORBIDDEN_TEXT = (
    "confirmation_token",
    "api_key",
    "tushare_token",
    "raw_positions",
    "raw_evidence",
    "raw_tool_payload",
    "Traceback",
    "stack_trace",
)


def build_reflection_safe_summary(
    result: dict[str, Any] | None = None,
    *,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    run_id: str = "",
) -> dict[str, Any]:
    result = result if isinstance(result, dict) else {}
    target_run_id = str(run_id or result.get("run_id") or (result.get("runtime") or {}).get("run_id") or "")
    reflection = result.get("reflection") if isinstance(result.get("reflection"), dict) else {}
    if not reflection and target_run_id:
        try:
            rows = ReflectionStore(output_dir=output_dir).list_results_by_run(target_run_id, user_id=user_id)
            reflection = rows[-1].to_dict() if rows else {}
        except Exception as exc:
            return {
                "reflection_available": False,
                "run_id": target_run_id,
                "load_error": type(exc).__name__,
                "safety": _safety_flags(),
            }
    if not reflection:
        return {
            "reflection_available": False,
            "run_id": target_run_id,
            "critic_count": 0,
            "issues": [],
            "safety": _safety_flags(),
        }
    safe = CriticSanitizer().sanitize_for_ui(reflection)
    issues = _safe_issues(safe.get("issues") if isinstance(safe.get("issues"), list) else [])
    summary = {
        "reflection_available": True,
        "run_id": target_run_id,
        "critic_id": str(safe.get("critic_id") or "")[:80],
        "critic_action": str(safe.get("action") or "")[:80],
        "critic_severity": str(safe.get("severity") or "")[:80],
        "critic_score": safe.get("score"),
        "issue_count": int(safe.get("issue_count") or len(issues) or 0),
        "safe_summary": str(safe.get("summary") or safe.get("target_summary") or "")[:600],
        "next_action_hint": _next_action_hint(safe),
        "issues": issues,
        "refs": {
            "evidence_refs": list(safe.get("evidence_refs") or [])[:10],
            "observation_refs": list(safe.get("observation_refs") or [])[:10],
            "replan_refs": list(safe.get("replan_refs") or [])[:10],
            "message_refs": list(safe.get("message_refs") or [])[:10],
            "memory_refs": list(safe.get("memory_refs") or [])[:10],
            "approval_refs": list(safe.get("approval_refs") or [])[:10],
        },
        "safety": _safety_flags(),
    }
    return _strip_forbidden(summary)


def format_reflection_caption(summary: dict[str, Any] | None) -> str:
    if not isinstance(summary, dict) or not summary.get("reflection_available"):
        return ""
    score = summary.get("critic_score")
    score_text = "-"
    try:
        score_text = f"{float(score):.2f}"
    except Exception:
        pass
    return (
        "Reflection Critic: "
        f"action={summary.get('critic_action') or '-'} | "
        f"severity={summary.get('critic_severity') or '-'} | "
        f"score={score_text} | "
        f"issues={int(summary.get('issue_count') or 0)}"
    )


def build_reflection_health_summary(*, user_id: str = "default", output_dir: str | Path = "outputs", limit: int = 50) -> dict[str, Any]:
    try:
        root = Path(output_dir) / "reflection_logs" / str(user_id or "default")
        files = sorted(root.glob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True) if root.exists() else []
        records: list[dict[str, Any]] = []
        sanitizer = CriticSanitizer()
        for path in files[: max(1, int(limit or 50))]:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = raw.get("critic_result") if raw.get("kind") == "critic_result" else None
                if isinstance(payload, dict):
                    safe = sanitizer.sanitize_for_context(payload)
                    safe["run_id"] = str(safe.get("run_id") or path.stem)
                    records.append(safe)
        latest = records[-1] if records else {}
        actions = [str(item.get("action") or "") for item in records]
        verdicts = [str(item.get("verdict") or "") for item in records]
        blocking = [
            item
            for item in records
            if str(item.get("action") or "") == "BLOCK_AND_REPORT" or str(item.get("severity") or "") == "BLOCKING"
        ]
        return _strip_forbidden(
            {
                "status": "ok",
                "latest_run_id": str(latest.get("run_id") or (files[0].stem if files else "")),
                "run_file_count": len(files),
                "latest_critic_count": len(records),
                "critic_pass_count": sum(1 for item in actions if item == "PASS"),
                "critic_fail_count": sum(1 for item in verdicts if item in {"FAIL", "BLOCKED"}),
                "blocking_issue_count": len(blocking),
                "latest_critic_action": str(latest.get("action") or ""),
                "latest_critic_severity": str(latest.get("severity") or ""),
                "latest_critic_score": latest.get("score"),
                "reflection_log_summary": f"reflection_logs/{str(user_id or 'default')}/files={len(files)}",
                "safety": _safety_flags(),
            }
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "latest_run_id": "",
            "run_file_count": 0,
            "latest_critic_count": 0,
            "critic_pass_count": 0,
            "critic_fail_count": 0,
            "blocking_issue_count": 0,
            "latest_critic_action": "",
            "error": type(exc).__name__,
            "safety": _safety_flags(),
        }


def _safe_issues(issues: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sanitizer = CriticSanitizer()
    for issue in issues[:10]:
        item = sanitizer.sanitize_for_ui(issue)
        if not isinstance(item, dict):
            continue
        rows.append(
            _strip_forbidden(
                {
                    "issue_type": str(item.get("category") or "")[:80],
                    "severity": str(item.get("severity") or "")[:80],
                    "summary": str(item.get("summary") or "")[:360],
                    "recommended_action": str(item.get("recommended_action") or item.get("action") or "")[:160],
                    "refs": {
                        "evidence_refs": list(item.get("evidence_refs") or [])[:5],
                        "observation_refs": list(item.get("observation_refs") or [])[:5],
                        "message_refs": list(item.get("message_refs") or [])[:5],
                        "approval_refs": list(item.get("approval_refs") or [])[:5],
                    },
                }
            )
        )
    return rows


def _next_action_hint(safe: dict[str, Any]) -> str:
    for key in ("revision_instruction", "replan_hint", "handoff_hint"):
        value = str(safe.get(key) or "").strip()
        if value:
            return value[:360]
    action = str(safe.get("action") or "")
    if action == "PASS":
        return "No action required."
    if action == "REQUIRE_APPROVAL":
        return "Wait for WriteGateway approval and revalidate before commit."
    if action == "REPLAN_READONLY":
        return "Use read-only replan evidence before changing the final wording."
    if action == "ASK_USER":
        return "Ask the user for the missing required information."
    if action == "BLOCK_AND_REPORT":
        return "Block unsafe output and report a safe summary."
    return ""


def _safety_flags() -> dict[str, bool]:
    return {
        "secrets_redacted": True,
        "raw_paths_hidden": True,
        "raw_payload_hidden": True,
        "private_reasoning_hidden": True,
    }


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _contains_forbidden(key_text):
                continue
            cleaned[key_text] = _strip_forbidden(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_forbidden(item) for item in value]
    if isinstance(value, str):
        text = value
        for marker in FORBIDDEN_TEXT:
            text = text.replace(marker, "[redacted]")
        return text
    return value


def _contains_forbidden(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker.lower() in lowered for marker in FORBIDDEN_TEXT)
