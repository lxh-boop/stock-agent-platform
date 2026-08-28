from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
from typing import Any
from uuid import uuid4

from config import AGENT_QUANT_DB_PATH, OUTPUT_DIR
from application.agent_service import AgentApplicationService
from application.handoff_service import build_handoff_safe_summary
from application.reflection_service import build_reflection_safe_summary
from agent.memory.memory_context_bridge import build_memory_safe_summary
from agent.react.react_context_bridge import (
    build_react_safe_summary,
    list_safe_observation_summaries,
)
from agent.runtime import load_run_snapshot
from agent.proposals import ProposalStore
from agent.services.strategy_proposal_service import StrategyProposalService
from agent.artifacts import ArtifactStore
from agent.collaboration.turn_inventory import record_turn_summary


COMPLIANCE_NOTE = "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。"
UNAVAILABLE_MESSAGE = "目前不能回答，相关功能仍在后续开发中。"

SENSITIVE_KEYS = {
    "api_key",
    "token",
    "password",
    "secret",
    "credential",
    "confirmation_token",
    "confirmation_token_hash",
    "plan_hash",
    "snapshot_id",
    "business_state_version",
    "db_path",
    "database_path",
    "output_dir",
    "file_path",
    "module_path",
    "traceback",
    "traceback_tail",
    "stack_trace",
}


def _now_text() -> str:
    return datetime.now(UTC).isoformat()


def _estimate_tokens(text: str) -> int:
    value = str(text or "")
    return max(1, (len(value) + 3) // 4) if value else 0


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_value(value: Any, *, max_chars: int = 800, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if lowered in SENSITIVE_KEYS or any(marker in lowered for marker in ("api_key", "password", "secret", "confirmation_token")):
                continue
            output[key] = _safe_value(item, max_chars=max_chars, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        output = [_safe_value(item, max_chars=max_chars, depth=depth + 1) for item in items[:50]]
        if len(items) > 50:
            output.append({"truncated_count": len(items) - 50})
        return output
    if isinstance(value, str):
        text = value.replace("\\", "/")
        if ":/" in text[:5] or text.startswith("/app/"):
            return "[server-path-redacted]"
        return text if len(text) <= max_chars else text[:max_chars] + "...[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        try:
            return _safe_value(value.to_dict(), max_chars=max_chars, depth=depth + 1)
        except Exception:
            return str(value)
    return str(value)


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _runtime_call_records(
    run_id: str,
    steps: list[dict[str, Any]],
    persisted_tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build one safe diagnostic stream for legacy Tool calls and Worker DAG runs."""

    records: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    steps_with_persisted_calls: set[str] = set()

    for index, row in enumerate(persisted_tool_calls):
        if not isinstance(row, dict):
            continue
        step_id = str(row.get("step_id") or "")
        call_id = str(row.get("tool_call_id") or f"{run_id}:persisted:{index}")
        if call_id in seen_call_ids:
            continue
        seen_call_ids.add(call_id)
        if step_id:
            steps_with_persisted_calls.add(step_id)
        records.append(
            {
                "tool_call_id": call_id,
                "step_id": step_id,
                "tool_name": str(row.get("tool_name") or "unknown_tool"),
                "status": str(row.get("status") or "unknown"),
                "call_kind": "tool",
                "record_source": "agent_tool_calls",
                "error_type": str(row.get("error_type") or ""),
                "error_message": str(row.get("error_message") or ""),
                "started_at": str(row.get("started_at") or ""),
                "finished_at": str(row.get("finished_at") or ""),
                "duration_seconds": _float_value(row.get("duration_seconds")),
                "retry_count": int(_float_value(row.get("retry_count"))),
                "input_summary": row.get("input_summary_json") or {},
                "output_summary": row.get("output_summary_json") or {},
            }
        )

    for step_index, row in enumerate(steps):
        if not isinstance(row, dict):
            continue
        step_id = str(row.get("step_id") or f"step_{step_index}")
        metadata = _metadata(row.get("metadata_json") or row.get("metadata"))
        raw_executions = metadata.get("tool_executions")
        if not isinstance(raw_executions, list):
            single_execution = metadata.get("tool_execution")
            raw_executions = [single_execution] if isinstance(single_execution, dict) else []

        added_execution = False
        for execution_index, execution in enumerate(raw_executions):
            if not isinstance(execution, dict):
                continue
            call_id = str(
                execution.get("tool_call_id")
                or f"{run_id}:{step_id}:tool:{execution_index}"
            )
            if call_id in seen_call_ids:
                added_execution = True
                continue
            seen_call_ids.add(call_id)
            added_execution = True
            duration_ms = _float_value(execution.get("duration_ms"), -1.0)
            duration_seconds = (
                duration_ms / 1000.0
                if duration_ms >= 0.0
                else _float_value(row.get("duration_seconds"))
            )
            records.append(
                {
                    "tool_call_id": call_id,
                    "step_id": step_id,
                    "tool_name": str(
                        execution.get("canonical_tool_name")
                        or execution.get("tool_name")
                        or row.get("intent")
                        or "unknown_tool"
                    ),
                    "status": str(execution.get("status") or row.get("status") or "unknown"),
                    "call_kind": "tool",
                    "record_source": "worker_result_metadata",
                    "error_type": str(execution.get("error_type") or ""),
                    "error_message": str(execution.get("error_message") or ""),
                    "started_at": str(execution.get("started_at") or row.get("started_at") or ""),
                    "finished_at": str(execution.get("finished_at") or row.get("finished_at") or ""),
                    "duration_seconds": duration_seconds,
                    "retry_count": int(_float_value(execution.get("retry_count"))),
                    "input_summary": {
                        "argument_keys": list(
                            (row.get("tool_args_summary_json") or {}).keys()
                        )
                        if isinstance(row.get("tool_args_summary_json"), dict)
                        else [],
                    },
                    "output_summary": {
                        "success": execution.get("success"),
                        "warning_count": execution.get("warning_count"),
                        "error_count": execution.get("error_count"),
                        "artifact_id": execution.get("artifact_id"),
                        "circuit_state": execution.get("circuit_state"),
                        "failure_kind": execution.get("failure_kind"),
                        "retryable": execution.get("retryable"),
                    },
                }
            )

        if (
            not added_execution
            and step_id not in steps_with_persisted_calls
            and str(metadata.get("runtime_layer") or "") == "worker_dag"
        ):
            records.append(
                {
                    "tool_call_id": f"{run_id}:{step_id}:worker",
                    "step_id": step_id,
                    "tool_name": str(
                        metadata.get("task_type")
                        or row.get("intent")
                        or "worker_task"
                    ),
                    "status": str(row.get("status") or "unknown"),
                    "call_kind": "worker",
                    "record_source": "agent_steps",
                    "error_type": str(row.get("error_type") or ""),
                    "error_message": "",
                    "started_at": str(row.get("started_at") or ""),
                    "finished_at": str(row.get("finished_at") or ""),
                    "duration_seconds": _float_value(row.get("duration_seconds")),
                    "retry_count": max(
                        0,
                        int(_float_value(metadata.get("attempt"), 1.0)) - 1,
                    ),
                    "input_summary": {
                        "depends_on": row.get("depends_on_json") or [],
                        "required_outputs": metadata.get("required_outputs") or [],
                    },
                    "output_summary": {
                        "summary": str(row.get("observation_summary") or ""),
                        "worker_result_status": metadata.get("worker_result_status"),
                        "confidence": metadata.get("confidence"),
                        "artifact_refs": metadata.get("artifact_refs") or [],
                        "evidence_ref_count": len(metadata.get("evidence_refs") or []),
                    },
                }
            )

    return records


def _result_run_id(result: dict[str, Any] | None) -> str:
    data = result if isinstance(result, dict) else {}
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    return str(
        data.get("run_id")
        or runtime.get("run_id")
        or nested.get("run_id")
        or ""
    )


def _normalise_answer(result: dict[str, Any] | None, *, failure_message: str = "") -> str:
    """Forward the Coordinator-selected user-facing answer without reinterpreting run success.

    Coordinator is the single authority that validates W06 FinalReport. A partial
    or failed sub-request may make ``success`` false while W06 still has a valid,
    bounded answer. Only fall back to task failure/unavailable text when the
    Coordinator supplied no answer at all.
    """
    data = result if isinstance(result, dict) else {}
    answer = str(
        data.get("answer")
        or data.get("message")
        or data.get("response")
        or ""
    ).strip()
    if not answer:
        answer = f"Agent 任务未完成：{failure_message}" if failure_message else UNAVAILABLE_MESSAGE
    if "不构成投资建议" not in answer:
        answer = f"{answer}\n\n{COMPLIANCE_NOTE}".strip()
    return answer


class WebAgentApplicationService:
    """Browser-facing Agent facade for Stage 6.4.

    The facade exposes conversation/message read models and safe run diagnostics.
    Agent execution remains in the existing Task Runtime and all protected writes
    continue through the existing control gateway.
    """

    def __init__(
        self,
        *,
        output_dir: str | Path = OUTPUT_DIR,
        db_path: str | Path | None = AGENT_QUANT_DB_PATH,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.db_path = Path(db_path) if db_path else None
        self.agent = AgentApplicationService(str(self.db_path) if self.db_path else None)

    @staticmethod
    def _user_id(value: Any) -> str:
        return str(value or "default").strip() or "default"

    def _require_session(self, user_id: str, conversation_id: str, *, active_only: bool = True) -> dict[str, Any]:
        row = self.agent.get_user_conversation(
            self._user_id(user_id),
            str(conversation_id or ""),
            active_only=active_only,
        )
        if not row:
            raise PermissionError("conversation_not_found_or_not_owned")
        return row

    @staticmethod
    def _conversation(row: dict[str, Any]) -> dict[str, Any]:
        metadata = _metadata(row.get("metadata_json") or row.get("metadata"))
        return {
            "conversation_id": str(row.get("conversation_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "title": str(row.get("title") or "New conversation"),
            "status": str(row.get("status") or "active"),
            "language": str(row.get("language") or "zh"),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "last_message_at": str(row.get("last_message_at") or ""),
            "last_run_id": str(metadata.get("last_run_id") or ""),
        }

    @staticmethod
    def _message(row: dict[str, Any]) -> dict[str, Any]:
        metadata = _metadata(row.get("metadata_json") or row.get("metadata"))
        result_summary = metadata.get("agent_result_summary") if isinstance(metadata.get("agent_result_summary"), dict) else {}
        legacy_result = metadata.get("agent_result") if isinstance(metadata.get("agent_result"), dict) else {}
        run_id = str(metadata.get("run_id") or _result_run_id(legacy_result) or "")
        task_id = str(metadata.get("task_id") or "")
        return {
            "message_id": str(row.get("message_id") or ""),
            "conversation_id": str(row.get("conversation_id") or ""),
            "user_id": str(row.get("user_id") or ""),
            "role": str(row.get("role") or "assistant"),
            "content": str(row.get("content") or ""),
            "language": str(row.get("language") or "zh"),
            "created_at": str(row.get("created_at") or ""),
            "run_id": run_id,
            "task_id": task_id,
            "result_summary": _safe_value(result_summary or legacy_result, max_chars=500),
        }

    def list_sessions(self, user_id: str, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        rows = self.agent.list_active_conversations(user_id, limit=max(1, min(int(limit), 100)), offset=max(0, int(offset)))
        records = [self._conversation(dict(row or {})) for row in rows]
        return {"records": records, "total": len(records), "offset": max(0, int(offset)), "limit": max(1, min(int(limit), 100))}

    def create_session(self, *, user_id: str, title: str = "", language: str = "zh") -> dict[str, Any]:
        user_id = self._user_id(user_id)
        now = _now_text()
        conversation_id = f"conv_{uuid4().hex[:12]}"
        row = self.agent.upsert_conversation(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "title": str(title or "New conversation")[:80],
                "status": "active",
                "language": "en" if str(language).lower().startswith("en") else "zh",
                "created_at": now,
                "updated_at": now,
                "last_message_at": "",
                "metadata": {"surface": "react_agent", "created_by": "web_agent_service"},
            }
        )
        return self._conversation(dict(row or self.agent.get_conversation(conversation_id) or {}))

    def get_session(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        return self._conversation(self._require_session(user_id, conversation_id))

    def rename_session(self, *, user_id: str, conversation_id: str, title: str) -> dict[str, Any]:
        self._require_session(user_id, conversation_id)
        value = " ".join(str(title or "").split())[:80] or "New conversation"
        if not self.agent.rename_conversation(
            user_id=self._user_id(user_id),
            conversation_id=conversation_id,
            title=value,
            updated_at=_now_text(),
        ):
            raise RuntimeError("conversation_rename_failed")
        return self.get_session(user_id, conversation_id)

    def delete_session(self, *, user_id: str, conversation_id: str) -> dict[str, Any]:
        self._require_session(user_id, conversation_id)
        if not self.agent.soft_delete_conversation(
            user_id=self._user_id(user_id),
            conversation_id=conversation_id,
            updated_at=_now_text(),
        ):
            raise RuntimeError("conversation_delete_failed")
        return {"conversation_id": conversation_id, "deleted": True}

    def list_messages(
        self,
        user_id: str,
        conversation_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        self._require_session(user_id, conversation_id)
        rows = self.agent.list_recent_messages(
            conversation_id,
            user_id=user_id,
            limit=max(1, min(int(limit), 100)),
            offset=max(0, int(offset)),
        )
        records = [self._message(dict(row or {})) for row in rows]
        return {"records": records, "total": len(records), "offset": max(0, int(offset)), "limit": max(1, min(int(limit), 100))}

    def append_message(
        self,
        *,
        user_id: str,
        conversation_id: str,
        role: str,
        content: str,
        language: str = "zh",
        message_id: str = "",
    ) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        conversation = self._require_session(user_id, conversation_id)
        role = str(role or "").lower()
        if role not in {"user", "assistant", "system"}:
            raise ValueError("invalid_message_role")
        content = str(content or "").strip()
        if not content:
            raise ValueError("message_content_required")
        now = _now_text()
        message_id = str(message_id or f"msg_{uuid4().hex[:16]}")
        row = self.agent.upsert_message(
            {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "role": role,
                "content": content,
                "language": "en" if str(language).lower().startswith("en") else "zh",
                "created_at": now,
                "token_estimate": _estimate_tokens(content),
                "metadata": {"surface": "react_agent"},
            }
        )
        title = str(conversation.get("title") or "")
        if role == "user" and (not title or title == "New conversation"):
            compact = " ".join(content.split())
            title = compact[:28] + ("..." if len(compact) > 28 else "")
        self.agent.upsert_conversation(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "title": title or "New conversation",
                "status": "active",
                "language": conversation.get("language") or language or "zh",
                "created_at": conversation.get("created_at") or now,
                "updated_at": now,
                "last_message_at": now,
                "metadata": conversation.get("metadata_json") or {},
            }
        )
        return self._message(dict(row or {}))

    def finalize_task(self, *, user_id: str, conversation_id: str, task: dict[str, Any]) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        conversation = self._require_session(user_id, conversation_id)
        if str(task.get("owner_id") or "") != user_id:
            raise PermissionError("task_owner_mismatch")
        if str(task.get("session_id") or "") != str(conversation_id):
            raise PermissionError("task_conversation_mismatch")
        if str(task.get("task_type") or "") != "agent.run":
            raise ValueError("not_an_agent_task")
        status = str(task.get("status") or "")
        if status not in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
            raise ValueError("agent_task_not_terminal")

        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        error = task.get("error") if isinstance(task.get("error"), dict) else {}
        answer = _normalise_answer(
            result,
            failure_message=str(error.get("message") or task.get("message") or "") if status != "succeeded" else "",
        )
        run_id = _result_run_id(result)
        task_id = str(task.get("task_id") or "")
        message_id = f"msg_task_{task_id.replace('task_', '')[:24]}"
        now = _now_text()
        summary = {
            "success": status == "succeeded" and result.get("success", True) is not False,
            "status": status,
            "run_id": run_id,
            "warnings": _safe_value(result.get("warnings") or [], max_chars=300),
            "intent": str(result.get("intent") or ""),
        }
        row = self.agent.upsert_message(
            {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "user_id": user_id,
                "role": "assistant",
                "content": answer,
                "language": conversation.get("language") or "zh",
                "created_at": now,
                "token_estimate": _estimate_tokens(answer),
                "metadata": {
                    "surface": "react_agent",
                    "run_id": run_id,
                    "task_id": task_id,
                    "agent_result_summary": summary,
                },
            }
        )
        metadata = conversation.get("metadata_json") if isinstance(conversation.get("metadata_json"), dict) else {}
        self.agent.upsert_conversation(
            {
                "conversation_id": conversation_id,
                "user_id": user_id,
                "title": conversation.get("title") or "New conversation",
                "status": "active",
                "language": conversation.get("language") or "zh",
                "created_at": conversation.get("created_at") or now,
                "updated_at": now,
                "last_message_at": now,
                "metadata": {**metadata, "last_run_id": run_id, "last_task_id": task_id},
            }
        )
        if str(answer or "").strip():
            # 只要本轮产生了可见回答就沉淀轮次摘要。父 Run 即使部分失败，
            # 其中已成功并落成 Artifact 的 BusinessData 仍可在后续 Need 级复用。
            try:
                self._record_turn_summary(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    run_id=run_id,
                    answer=answer,
                )
            except Exception:
                pass
        return {
            **self._message(dict(row or {})),
            "run_id": run_id,
            "task_id": task_id,
            "result_summary": summary,
        }

    def _record_turn_summary(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
        answer: str,
    ) -> None:
        # 汇总本轮沉淀所需材料并写入轮次摘要：
        # 用户提问取最近一条 user 消息；artifact 引用按 run_id 从共享存储回收
        user_message = ""
        recent = self.agent.list_recent_messages(conversation_id, user_id, limit=20)
        for message in reversed(list(recent or [])):
            if str(message.get("role") or "") == "user":
                user_message = str(message.get("content") or "")
                break
        artifact_refs: list[dict[str, Any]] = []
        if run_id:
            rows = ArtifactStore(db_path=self.db_path).list_readable(
                user_id=user_id,
                run_id=str(run_id),
                artifact_type="tool_result",
                limit=50,
            )
            for artifact_row in rows:
                # 只保留数据级保护所需的最小字段集
                artifact_refs.append(
                    {
                        "artifact_id": str(artifact_row.get("artifact_id") or ""),
                        "producer_id": str(artifact_row.get("producer_id") or ""),
                        "produced_outputs": list(artifact_row.get("produced_outputs") or []),
                        "trade_date": str(artifact_row.get("trade_date") or ""),
                        "content_hash": str(artifact_row.get("content_hash") or ""),
                        "expires_at": str(artifact_row.get("expires_at") or ""),
                    }
                )
        record_turn_summary(
            self.db_path,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=str(run_id or ""),
            user_message=user_message,
            assistant_answer=answer,
            artifact_refs=artifact_refs,
        )

    def _require_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        rows = self.agent.list_agent_runs_by_ids([str(run_id or "")])
        row = dict(rows[0] or {}) if rows else {}
        if not row:
            raise ValueError("agent_run_not_found")
        if str(row.get("user_id") or user_id) != user_id:
            raise PermissionError("agent_run_owner_mismatch")
        return row

    def run_detail(self, user_id: str, run_id: str) -> dict[str, Any]:
        run = self._require_run(user_id, run_id)
        snapshot = load_run_snapshot(self.db_path, run_id)
        steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
        persisted_tool_calls = snapshot.get("tool_calls") if isinstance(snapshot.get("tool_calls"), list) else []
        tool_calls = _runtime_call_records(run_id, steps, persisted_tool_calls)
        sources = snapshot.get("sources") if isinstance(snapshot.get("sources"), list) else []
        proposals = snapshot.get("proposals") if isinstance(snapshot.get("proposals"), list) else []
        return {
            "run": _safe_value(
                {
                    "run_id": run.get("run_id"),
                    "conversation_id": run.get("conversation_id"),
                    "goal": run.get("goal"),
                    "status": run.get("status"),
                    "created_at": run.get("created_at"),
                    "started_at": run.get("started_at"),
                    "finished_at": run.get("finished_at"),
                    "metadata": run.get("metadata_json") or {},
                },
                max_chars=500,
            ),
            "steps": [
                _safe_value(
                    {
                        "step_id": row.get("step_id"),
                        "intent": row.get("intent"),
                        "status": row.get("status"),
                        "depends_on": row.get("depends_on_json") or [],
                        "observation": row.get("observation_summary") or "",
                        "duration_seconds": row.get("duration_seconds"),
                        "metadata": row.get("metadata_json") or {},
                    },
                    max_chars=400,
                )
                for row in steps
                if isinstance(row, dict)
            ],
            "tool_calls": [
                _safe_value(row, max_chars=400)
                for row in tool_calls
                if isinstance(row, dict)
            ],
            "sources": [
                _safe_value(
                    {
                        "source_id": row.get("source_id"),
                        "source_type": row.get("source_type"),
                        "title": row.get("source_title"),
                        "snippet": row.get("snippet"),
                        "retrieved_at": row.get("retrieved_at"),
                    },
                    max_chars=300,
                )
                for row in sources
                if isinstance(row, dict)
            ],
            "proposals": [_safe_value(row, max_chars=400) for row in proposals if isinstance(row, dict)],
            "counts": {
                "steps": len(steps),
                "tool_calls": len(tool_calls),
                "persisted_tool_calls": len(persisted_tool_calls),
                "worker_calls": sum(
                    1 for row in tool_calls if row.get("call_kind") == "worker"
                ),
                "sources": len(sources),
                "proposals": len(proposals),
            },
        }

    def trace_summary(self, user_id: str, run_id: str) -> dict[str, Any]:
        self._require_run(user_id, run_id)
        messages, trace = self.agent.build_message_trace_summary(
            run_id=run_id,
            user_id=self._user_id(user_id),
            output_dir=self.output_dir,
        )
        trace_payload = trace.to_dict() if hasattr(trace, "to_dict") else trace
        return {
            "run_id": run_id,
            "message_count": len(messages),
            "trace": _safe_value(trace_payload, max_chars=500),
            "messages": [
                _safe_value(message.to_dict() if hasattr(message, "to_dict") else message, max_chars=350)
                for message in messages[:100]
            ],
        }

    def reflection_summary(self, user_id: str, run_id: str) -> dict[str, Any]:
        self._require_run(user_id, run_id)
        return _safe_value(
            build_reflection_safe_summary(
                {},
                user_id=self._user_id(user_id),
                output_dir=self.output_dir,
                run_id=run_id,
            ),
            max_chars=600,
        )

    def handoff_summary(self, user_id: str, run_id: str) -> dict[str, Any]:
        self._require_run(user_id, run_id)
        return _safe_value(
            build_handoff_safe_summary(
                {},
                user_id=self._user_id(user_id),
                output_dir=self.output_dir,
                run_id=run_id,
            ),
            max_chars=600,
        )

    def react_summary(self, user_id: str, run_id: str) -> dict[str, Any]:
        self._require_run(user_id, run_id)
        summary = build_react_safe_summary(
            user_id=self._user_id(user_id),
            output_dir=self.output_dir,
            run_id=run_id,
        )
        observations = list_safe_observation_summaries(
            user_id=self._user_id(user_id),
            output_dir=self.output_dir,
            run_id=run_id,
            limit=20,
            offset=0,
        )
        return {
            "summary": _safe_value(summary, max_chars=600),
            "observations": _safe_value(observations, max_chars=500),
        }

    def memory_summary(self, user_id: str, run_id: str) -> dict[str, Any]:
        self._require_run(user_id, run_id)
        summary = build_memory_safe_summary(
            user_id=self._user_id(user_id),
            output_dir=self.output_dir,
        )
        return _safe_value(summary, max_chars=600)

    @staticmethod
    def _confirmation_phrase(plan_id: str) -> str:
        suffix = str(plan_id or "")[-6:].upper()
        return f"CONFIRM-{suffix}" if suffix else "CONFIRM"

    def pending_actions(self, user_id: str, *, conversation_id: str = "") -> dict[str, Any]:
        user_id = self._user_id(user_id)
        if conversation_id:
            self._require_session(user_id, conversation_id)
        store = ProposalStore(output_dir=self.output_dir, db_path=self.db_path)
        candidates = (
            store.list_pending(user_id=user_id, session_id=conversation_id, limit=100)
            if conversation_id
            else store.list_pending_for_user(user_id=user_id, limit=100)
        )
        records: list[dict[str, Any]] = []
        for proposal in candidates:
            payload = dict(proposal.payload or {})
            proposal_id = proposal.proposal_id
            records.append(
                {
                    "proposal_id": proposal_id,
                    # Frozen Stage 6 route/type compatibility only. Both values
                    # identify the same canonical Proposal; no legacy plan store
                    # is queried or executed.
                    "plan_id": proposal_id,
                    "proposal_version": proposal.current_version,
                    "payload_hash": proposal.current_payload_hash,
                    "run_id": proposal.source_run_id,
                    "intent": str(payload.get("operation_type") or proposal.proposal_type),
                    "operation_type": str(payload.get("operation_type") or proposal.proposal_type),
                    "confirmation_status": "pending",
                    "execution_status": proposal.status.value,
                    "created_at": proposal.created_at,
                    "expires_at": proposal.expires_at,
                    "before_state_summary": _safe_value(payload.get("target") or {}, max_chars=350),
                    "proposed_changes": _safe_value(
                        payload.get("changes") or payload.get("execution_parameters") or {},
                        max_chars=350,
                    ),
                    "after_state_preview": _safe_value(payload.get("constraint_response") or {}, max_chars=350),
                    "warnings": _safe_value(payload.get("limitations") or [], max_chars=300),
                    "validation_results": {
                        "proposal_type": proposal.proposal_type,
                        "payload_hash_bound": bool(proposal.current_payload_hash),
                    },
                    "confirmation_phrase": self._confirmation_phrase(proposal_id),
                }
            )
        records.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return {"records": records, "total": len(records)}

    def control_pending_action(
        self,
        *,
        action: str,
        user_id: str,
        conversation_id: str,
        plan_id: str,
        confirmation_text: str = "",
        request_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        self._require_session(user_id, conversation_id)
        if not request_id or not idempotency_key:
            raise ValueError("request_id_and_idempotency_key_required")
        store = ProposalStore(output_dir=self.output_dir, db_path=self.db_path)
        proposal = store.get(str(plan_id))
        if proposal is None:
            raise ValueError("pending_action_not_found")
        if proposal.user_id != user_id:
            raise PermissionError("pending_action_owner_mismatch")
        if proposal.session_id and proposal.session_id != conversation_id:
            raise PermissionError("pending_action_session_mismatch")
        action = str(action or "").lower()
        kwargs = {
            "proposal_id": proposal.proposal_id,
            "user_id": user_id,
            "session_id": conversation_id,
            "run_id": proposal.source_run_id,
            "language": "zh",
            "output_dir": self.output_dir,
            "db_path": self.db_path,
            "idempotency_key": str(idempotency_key),
        }
        if action == "confirm":
            if str(confirmation_text or "").strip().upper() != self._confirmation_phrase(plan_id):
                raise ValueError("confirmation_text_mismatch")
        elif action != "reject":
            raise ValueError("invalid_control_action")
        result = self.agent.control_action(action=action, **kwargs)
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "result": _safe_value(result, max_chars=600),
        }

    def strategy_proposal(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        self._require_session(user_id, conversation_id)
        service = StrategyProposalService(self.db_path)
        proposal = service.get_active(
            user_id=user_id,
            account_id=f"paper_{user_id}",
            conversation_id=conversation_id,
        )
        if proposal is None:
            return {"available": False, "proposal": None, "versions": []}
        versions = service.list_versions(proposal.proposal_id, user_id=user_id)
        return {
            "available": True,
            "proposal": _safe_value(proposal.to_dict(), max_chars=500),
            "versions": [_safe_value(item.to_dict(), max_chars=600) for item in versions[-10:]],
        }


web_agent_service = WebAgentApplicationService()
