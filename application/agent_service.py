from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.collaboration import execute_control_action
from agent.communication import MessageStore
from agent.console_trace import trace_event, trace_exception
from agent.executor import run_agent_request
from agent.mcp.registry_bridge import summarize_mcp_usage
from agent.memory.memory_context_bridge import (
    build_memory_safe_summary,
    list_memory_records_safe_page,
)
from agent.react.react_context_bridge import (
    build_react_safe_summary,
    list_safe_observation_summaries,
)
from agent.runtime import load_run_snapshot
from agent.services.strategy_proposal_service import StrategyProposalService
from agent.tools.portfolio_state_tool import query_portfolio_state
from agent.tools.scheduler_tool import query_scheduler_status
from agent.tools.tool_registry import list_tools
from agent.tools.tool_schemas import PAPER_AGENT_DISCLAIMER
from application.handoff_service import build_handoff_safe_summary, format_handoff_caption
from application.reflection_service import build_reflection_safe_summary, format_reflection_caption
from core.llm.runtime_settings import LLMRuntimeSettings
from database.repositories.agent_repository import AgentRepository


@dataclass(slots=True)
class AgentApplicationService:
    """Single application boundary for the Streamlit Agent page.

    Repository objects and Agent runtime modules do not escape this class.
    """

    db_path: str | None = None
    _repo: AgentRepository = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._repo = AgentRepository(self.db_path or None)

    @property
    def _repository(self) -> AgentRepository:
        return self._repo

    def list_active_conversations(
        self,
        user_id: str,
        *,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        return list(
            self._repository.list_active_conversations(
                str(user_id), limit=int(limit), offset=int(offset)
            )
            or []
        )

    def list_recent_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        return list(
            self._repository.list_recent_messages(
                str(conversation_id),
                user_id=str(user_id),
                limit=int(limit),
                offset=int(offset),
            )
            or []
        )

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return dict(self._repository.get_conversation(str(conversation_id)) or {})

    def get_user_conversation(
        self,
        user_id: str,
        conversation_id: str,
        *,
        active_only: bool = False,
    ) -> dict[str, Any]:
        row = self.get_conversation(conversation_id)
        if str(row.get("user_id") or "") != str(user_id):
            return {}
        if active_only and str(row.get("status") or "active") != "active":
            return {}
        return row

    def upsert_conversation(self, payload: dict[str, Any]) -> Any:
        return self._repository.upsert_conversation(dict(payload))

    def rename_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        title: str,
        updated_at: str,
    ) -> bool:
        if not self.get_user_conversation(user_id, conversation_id, active_only=True):
            return False
        changed = self._repository.store.update(
            "conversations",
            {"conversation_id": str(conversation_id)},
            {"title": str(title or "New conversation")[:80], "updated_at": str(updated_at)},
        )
        return int(changed or 0) > 0

    def soft_delete_conversation(
        self,
        *,
        user_id: str,
        conversation_id: str,
        updated_at: str,
    ) -> bool:
        row = self.get_user_conversation(user_id, conversation_id, active_only=True)
        if not row:
            return False
        self.upsert_conversation(
            {
                "conversation_id": str(conversation_id),
                "user_id": str(user_id),
                "title": row.get("title") or "New conversation",
                "status": "deleted",
                "language": row.get("language") or "zh",
                "created_at": row.get("created_at") or str(updated_at),
                "updated_at": str(updated_at),
                "last_message_at": row.get("last_message_at") or "",
                "metadata": {"deleted_from": "ai_agent_page"},
            }
        )
        return True

    def upsert_message(self, payload: dict[str, Any]) -> Any:
        return self._repository.upsert_message(dict(payload))

    def list_agent_runs_by_ids(self, run_ids: list[str]) -> list[dict[str, Any]]:
        return list(self._repository.list_agent_runs_by_ids(list(run_ids)) or [])

    def run(
        self,
        query: str,
        *,
        user_id: str,
        output_dir: str,
        top_k: int,
        session_id: str,
        llm_settings: LLMRuntimeSettings | None,
    ) -> dict[str, Any]:
        from agent_graph.service import resolve_agent_engine

        if resolve_agent_engine() == "langgraph":
            from agent_graph.service import run_langgraph_agent_request

            return run_langgraph_agent_request(
                str(query),
                user_id=str(user_id),
                output_dir=str(output_dir),
                db_path=self.db_path,
                top_k=int(top_k),
                session_id=str(session_id),
                llm_settings=llm_settings,
            )
        return run_agent_request(
            str(query),
            user_id=str(user_id),
            output_dir=str(output_dir),
            db_path=self.db_path,
            top_k=int(top_k),
            session_id=str(session_id),
            llm_settings=llm_settings,
        )

    def list_registered_tools(self) -> list[Any]:
        return list(list_tools() or [])

    def query_portfolio(self, user_id: str, *, output_dir: str, db_path: str | None = None) -> dict[str, Any]:
        return query_portfolio_state(
            str(user_id),
            output_dir=str(output_dir),
            db_path=db_path if db_path is not None else self.db_path,
        )

    def query_scheduler(self, root: str = ".") -> dict[str, Any]:
        return query_scheduler_status(str(root))

    def control_action(self, **kwargs: Any) -> dict[str, Any]:
        return dict(execute_control_action(**kwargs) or {})

    def build_message_trace_summary(
        self,
        *,
        run_id: str,
        user_id: str,
        output_dir: str | Path,
    ) -> tuple[list[Any], Any]:
        store = MessageStore(output_dir=output_dir)
        messages = store.list_messages_by_run(str(run_id), user_id=str(user_id))
        trace = store.build_trace(str(run_id), user_id=str(user_id))
        return list(messages or []), trace


__all__ = [
    "AgentApplicationService",
    "LLMRuntimeSettings",
    "PAPER_AGENT_DISCLAIMER",
    "StrategyProposalService",
    "build_handoff_safe_summary",
    "build_memory_safe_summary",
    "build_react_safe_summary",
    "build_reflection_safe_summary",
    "format_handoff_caption",
    "format_reflection_caption",
    "list_memory_records_safe_page",
    "list_safe_observation_summaries",
    "load_run_snapshot",
    "summarize_mcp_usage",
    "trace_event",
    "trace_exception",
]
