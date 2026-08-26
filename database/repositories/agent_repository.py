from __future__ import annotations

from pathlib import Path
from typing import Any

from database.schemas import json_dumps, json_loads
from database.sqlite_store import SQLiteStore


AGENT_DECISION_JSON_FIELDS = [
    "user_constraint",
    "triggered_rules",
    "evidence_news_ids",
    "evidence_chunk_ids",
    "evidence_snapshot",
]

AGENT_RUNTIME_JSON_FIELDS: dict[str, list[str]] = {
    "conversations": ["metadata_json"],
    "messages": ["metadata_json"],
    "agent_runs": ["metadata_json"],
    "agent_steps": ["depends_on_json", "tool_args_summary_json", "metadata_json"],
    "agent_tool_calls": ["input_summary_json", "output_summary_json", "metadata_json"],
    "agent_sources": ["metadata_json"],
    "agent_sandbox_runs": ["result_summary_json", "generated_files_json", "metadata_json"],
    "action_proposals": [
        "before_state_summary_json",
        "proposed_changes_json",
        "after_state_preview_json",
        "warnings_json",
        "validation_results_json",
        "metadata_json",
    ],
    "action_approvals": ["metadata_json"],
    "action_commits": ["result_summary_json", "metadata_json"],
    "conversation_summaries": ["metadata_json"],
    "memory_items": [
        "topics_json",
        "stock_codes_json",
        "company_names_json",
        "industries_json",
        "metadata_json",
    ],
    "memory_links": ["metadata_json"],
    "user_feedback": ["metadata_json"],
    "artifacts": ["metadata_json"],
    "agent_turn_summaries": ["artifact_refs_json", "metadata_json"],
}

JSON_ALIASES: dict[str, str] = {
    "metadata": "metadata_json",
    "depends_on": "depends_on_json",
    "tool_args_summary": "tool_args_summary_json",
    "input_summary": "input_summary_json",
    "output_summary": "output_summary_json",
    "result_summary": "result_summary_json",
    "generated_files": "generated_files_json",
    "before_state_summary": "before_state_summary_json",
    "proposed_changes": "proposed_changes_json",
    "after_state_preview": "after_state_preview_json",
    "warnings": "warnings_json",
    "validation_results": "validation_results_json",
    "topics": "topics_json",
    "stock_codes": "stock_codes_json",
    "company_names": "company_names_json",
    "industries": "industries_json",
    "artifact_refs": "artifact_refs_json",
}


class AgentRepository:
    def __init__(self, db_path: str | Path | None = None):
        self.store = SQLiteStore(db_path)

    def _encode_runtime_record(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if table == "agent_steps":
            run_id = str(payload.get("run_id") or "").strip()
            step_id = str(payload.get("step_id") or "").strip()
            if run_id and step_id and not payload.get("step_record_id"):
                payload["step_record_id"] = f"{run_id}:{step_id}"
        for alias, target in JSON_ALIASES.items():
            if alias in payload and target not in payload:
                payload[target] = payload.pop(alias)
        for key in AGENT_RUNTIME_JSON_FIELDS.get(table, []):
            if isinstance(payload.get(key), (list, dict)):
                payload[key] = json_dumps(payload[key])
        return payload

    def _decode_runtime_record(self, table: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for key in AGENT_RUNTIME_JSON_FIELDS.get(table, []):
            payload[key] = json_loads(payload.get(key), default=self._json_default_for(key))
        return payload

    @staticmethod
    def _json_default_for(key: str) -> Any:
        if key in {
            "depends_on_json",
            "proposed_changes_json",
            "warnings_json",
            "generated_files_json",
            "topics_json",
            "stock_codes_json",
            "company_names_json",
            "industries_json",
            "artifact_refs_json",
        }:
            return []
        return {}

    def _upsert_runtime(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        return self.store.upsert(table, self._encode_runtime_record(table, record))

    def _list_runtime(
        self,
        table: str,
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.store.list(
            table,
            filters=filters,
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )
        return [self._decode_runtime_record(table, row) or {} for row in rows]

    def _list_runtime_by_values(
        self,
        table: str,
        column: str,
        values: list[Any],
        filters: dict[str, Any] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.store.list_by_values(
            table,
            column,
            values,
            filters=filters,
            order_by=order_by,
            descending=descending,
            limit=limit,
        )
        return [self._decode_runtime_record(table, row) or {} for row in rows]

    def insert_agent_rule(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if isinstance(payload.get("condition"), (list, dict)):
            payload["condition"] = json_dumps(payload["condition"])
        return self.store.upsert("agent_rule", payload)

    def get_agent_rule(self, rule_id: str) -> dict[str, Any] | None:
        row = self.store.get("agent_rule", {"rule_id": rule_id})
        if row:
            row["condition"] = json_loads(row.get("condition"), default={})
        return row

    def list_agent_rules(self, active_only: bool = False) -> list[dict[str, Any]]:
        filters = {"is_active": 1} if active_only else None
        rows = self.store.list("agent_rule", filters=filters, order_by="priority")
        for row in rows:
            row["condition"] = json_loads(row.get("condition"), default={})
        return rows

    def insert_decision_log(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if "combined_adjustment" not in payload and "final_score" in payload:
            payload["combined_adjustment"] = payload.get("final_score")
        payload.pop("final_action", None)
        payload.pop("final_score", None)
        for key in AGENT_DECISION_JSON_FIELDS:
            if isinstance(payload.get(key), (list, dict)):
                payload[key] = json_dumps(payload[key])
        return self.store.upsert("agent_decision_log", payload)

    def get_decision_log(self, decision_id: str) -> dict[str, Any] | None:
        row = self.store.get("agent_decision_log", {"decision_id": decision_id})
        return self._decode_decision_row(row) if row else None

    def list_decision_logs(
        self,
        user_id: str | None = None,
        stock_code: str | None = None,
    ) -> list[dict[str, Any]]:
        filters = {}
        if user_id:
            filters["user_id"] = user_id
        if stock_code:
            filters["stock_code"] = stock_code
        rows = self.store.list(
            "agent_decision_log",
            filters=filters or None,
            order_by="created_at",
        )
        return [self._decode_decision_row(row) for row in rows]

    def update_decision_log(self, decision_id: str, changes: dict[str, Any]) -> int:
        payload = dict(changes)
        if "combined_adjustment" not in payload and "final_score" in payload:
            payload["combined_adjustment"] = payload.get("final_score")
        payload.pop("final_action", None)
        payload.pop("final_score", None)
        for key in AGENT_DECISION_JSON_FIELDS:
            if isinstance(payload.get(key), (list, dict)):
                payload[key] = json_dumps(payload[key])
        return self.store.update("agent_decision_log", {"decision_id": decision_id}, payload)

    def upsert_conversation(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("conversations", record)

    def list_conversations(
        self,
        user_id: str,
        limit: int = 50,
        *,
        offset: int | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        return self._list_runtime(
            "conversations",
            filters={"user_id": user_id},
            order_by="updated_at",
            descending=descending,
            limit=limit,
            offset=offset,
        )

    def list_active_conversations(
        self,
        user_id: str,
        limit: int = 20,
        *,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._list_runtime(
            "conversations",
            filters={"user_id": user_id, "status": "active"},
            order_by="updated_at",
            descending=True,
            limit=limit,
            offset=offset,
        )

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        return self._decode_runtime_record(
            "conversations",
            self.store.get("conversations", {"conversation_id": conversation_id}),
        )

    def upsert_message(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("messages", record)

    def list_messages(
        self,
        conversation_id: str,
        limit: int = 100,
        *,
        offset: int | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        return self._list_runtime(
            "messages",
            filters={"conversation_id": conversation_id},
            order_by="created_at",
            descending=descending,
            limit=limit,
            offset=offset,
        )

    def list_recent_messages(
        self,
        conversation_id: str,
        user_id: str | None = None,
        limit: int = 50,
        *,
        offset: int | None = None,
    ) -> list[dict[str, Any]]:
        filters = {"conversation_id": conversation_id}
        if user_id:
            filters["user_id"] = user_id
        rows = self._list_runtime(
            "messages",
            filters=filters,
            order_by="created_at",
            descending=True,
            limit=limit,
            offset=offset,
        )
        return list(reversed(rows))

    def upsert_agent_run(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("agent_runs", record)

    def list_agent_runs_by_ids(self, run_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(run_id) for run_id in run_ids if str(run_id or "").strip()]
        return self._list_runtime_by_values(
            "agent_runs",
            "run_id",
            ids,
            order_by="created_at",
        )

    def upsert_agent_step(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("agent_steps", record)

    def upsert_agent_tool_call(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("agent_tool_calls", record)

    def upsert_agent_source(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("agent_sources", record)

    def list_agent_sources(self, user_id: str, run_id: str | None = None) -> list[dict[str, Any]]:
        filters = {"user_id": user_id}
        if run_id:
            filters["run_id"] = run_id
        return self._list_runtime("agent_sources", filters=filters, order_by="retrieved_at")

    def upsert_agent_sandbox_run(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("agent_sandbox_runs", record)

    def upsert_action_proposal(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("action_proposals", record)

    def get_action_proposal(self, plan_id: str) -> dict[str, Any] | None:
        return self._decode_runtime_record(
            "action_proposals",
            self.store.get("action_proposals", {"plan_id": plan_id}),
        )

    def upsert_action_approval(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("action_approvals", record)

    def upsert_action_commit(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("action_commits", record)

    def upsert_conversation_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("conversation_summaries", record)

    def list_conversation_summaries(
        self,
        user_id: str,
        conversation_id: str | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        filters = {"user_id": user_id, "status": status}
        if conversation_id:
            filters["conversation_id"] = conversation_id
        return self._list_runtime("conversation_summaries", filters=filters, order_by="updated_at", limit=limit)

    def upsert_memory_item(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("memory_items", record)

    def get_memory_item(self, memory_id: str) -> dict[str, Any] | None:
        return self._decode_runtime_record(
            "memory_items",
            self.store.get("memory_items", {"memory_id": memory_id}),
        )

    def update_memory_item(self, memory_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("memory_items", {"memory_id": memory_id}, self._encode_runtime_record("memory_items", changes))

    def list_memory_items(
        self,
        user_id: str,
        memory_type: str | None = None,
        status: str = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        filters = {"user_id": user_id, "status": status}
        if memory_type:
            filters["memory_type"] = memory_type
        return self._list_runtime("memory_items", filters=filters, order_by="updated_at", limit=limit)

    def upsert_memory_link(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("memory_links", record)

    def list_memory_links(self, memory_id: str) -> list[dict[str, Any]]:
        return self._list_runtime("memory_links", filters={"memory_id": memory_id}, order_by="created_at")

    def upsert_user_feedback(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("user_feedback", record)

    def list_user_feedback(
        self,
        user_id: str,
        feedback_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {"user_id": user_id}
        if feedback_type:
            filters["feedback_type"] = feedback_type
        return self._list_runtime("user_feedback", filters=filters, order_by="created_at", limit=limit)

    def upsert_artifact(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._upsert_runtime("artifacts", record)

    def upsert_turn_summary(self, record: dict[str, Any]) -> dict[str, Any]:
        # 写入/更新轮次摘要条目（ConversationInventory 数据源）
        return self._upsert_runtime("agent_turn_summaries", record)

    def get_turn_summary(self, turn_id: str) -> dict[str, Any] | None:
        # 按轮次编号读取单条摘要
        return self._decode_runtime_record(
            "agent_turn_summaries",
            self.store.get("agent_turn_summaries", {"turn_id": str(turn_id or "")}),
        )

    def list_turn_summaries(
        self,
        user_id: str,
        limit: int = 50,
        *,
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        # 按用户列出轮次摘要（默认按生成时间倒序，新的在前）
        return self._list_runtime(
            "agent_turn_summaries",
            filters={"user_id": str(user_id or "")},
            order_by="created_at",
            descending=descending,
            limit=limit,
        )

    @staticmethod
    def _decode_decision_row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        defaults = {
            "user_constraint": {},
            "triggered_rules": [],
            "evidence_news_ids": [],
            "evidence_chunk_ids": [],
            "evidence_snapshot": [],
        }
        for key, default in defaults.items():
            out[key] = json_loads(out.get(key), default=default)
        return out
