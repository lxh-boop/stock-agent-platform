from __future__ import annotations

from agent.runtime_version import RUNTIME_VERSION

from pathlib import Path
from typing import Any

from core.llm import LLMService

from agent.console_trace import flow_event, trace_exception
from agent.runtime import AgentRuntimeRecorder

from .write_runtime import WriteRequestExecutor
from .coordinator import AgentCollaborationCoordinator
from .llm_runtime import require_run_llm_service
from .runtime_services import CollaborationRuntimeServices
from .session_state import SessionStateStore


RUNTIME_BUILD = RUNTIME_VERSION
ACCESS_MODEL_VERSION = "read-write.v1"
COMPLETION_CONTRACT_VERSION = "capability-contract-list.v2"
COMPLETION_REPORT_VERSION = "capability-contract-report.v2"
EVIDENCE_ANALYSIS_REPORT_VERSION = "evidence-analysis-report.v1"


class UnifiedGraphAgentRequest:
    """The sole public Agent entry after the Neo4j hard cut."""

    def __init__(self, query: str) -> None:
        self.intent = "financial_graph_agent"
        self.parameters: dict[str, Any] = {}
        self.execution_route = "single_main_agent_graph_entry"
        self.decomposition = {
            "query": str(query or ""),
            "route_layer": "single_main_agent_graph_entry",
            "tasks": [],
            "is_multi_intent": False,
            "need_clarification": False,
            "clarification_question": "",
            "confidence": 1.0,
            "warnings": [],
            "user_goal": {
                "raw_message": str(query or ""),
                "resolved_message": str(query or ""),
                "action": "main_agent_decides",
                "objects": [],
                "constraints": [],
                "expected_outputs": ["capability_contract_results_or_control_result"],
            },
            "task_plan": {
                "tasks": [],
                "planning_level": "capability_contract",
                "tool_visibility": "worker_progressive_private",
            },
            "supervisor_decision": {
                "decision_source": "single_main_agent_graph_entry",
                "intent": "financial_graph_agent",
                "tasks": [],
                "agent_sequence": [],
                "dependencies": {},
                "requires_write": False,
                "confidence": 1.0,
                "reason": "all_requests_enter_existing_main_coordinator_with_graph_contracts",
                "safety_flags": [
                    "main_agent_worker_progressive_visibility",
                    "main_agent_tool_visibility_none",
                    "worker_private_tools",
                    "neo4j_entity_authority",
                    "legacy_public_entity_protocol_disabled",
                ],
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "parameters": dict(self.parameters),
            "query": str(self.decomposition.get("query") or ""),
            "decomposition": dict(self.decomposition),
            "execution_route": self.execution_route,
        }


def route_unified_agent_request(query: str, **_: Any) -> UnifiedGraphAgentRequest:
    return UnifiedGraphAgentRequest(str(query or ""))


def execute_unified_agent_request(
    *,
    query: str,
    user_id: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    default_top_k: int = 50,
    session_id: str = "",
    run_id: str = "",
    language: str = "zh",
    llm_service: LLMService | None = None,
    runtime_recorder: AgentRuntimeRecorder | None = None,
    context: dict[str, Any] | None = None,
    decomposition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_recorder = runtime_recorder or AgentRuntimeRecorder(
        user_id=str(user_id or "default"),
        goal=str(query or ""),
        db_path=db_path,
        session_id=str(session_id or f"session_{user_id}"),
        run_id=str(run_id or "") or None,
    )
    effective_run_id = effective_recorder.run_id
    binding = require_run_llm_service(
        llm_service=llm_service,
        run_id=effective_run_id,
    )
    runtime_services = CollaborationRuntimeServices.from_recorder(
        effective_recorder,
        user_id=str(user_id or "default"),
        session_id=str(session_id or f"session_{user_id}"),
        strict=True,
    )
    flow_event(
        "GRAPH_RUNTIME_INITIALIZATION_STARTED",
        {
            "entry": "AgentCollaborationCoordinator",
            "run_id": effective_run_id,
            "graph_boundary": "Neo4j/GraphRef",
        },
        run_id=effective_run_id,
    )
    try:
        coordinator = AgentCollaborationCoordinator(
            output_dir=output_dir,
            db_path=db_path,
            llm_service=binding.service,
            runtime_services=runtime_services,
        )
    except Exception as exc:
        flow_event(
            "GRAPH_RUNTIME_INITIALIZATION_FAILED",
            {
                "exception_type": type(exc).__name__,
                "message": str(exc),
            },
            run_id=effective_run_id,
            level="ERROR",
        )
        trace_exception(
            "collaboration.graph_runtime.initialization_failed",
            exc,
            run_id=effective_run_id,
        )
        raise
    flow_event(
        "GRAPH_RUNTIME_INITIALIZATION_COMPLETED",
        {
            "graph_id": str(getattr(getattr(coordinator, "store", None), "graph_id", "")),
            "worker_count": len(getattr(getattr(coordinator, "directory", None), "list", lambda: [])()),
            "request_entry": "request_bundle.v3",
            "request_categories": ["business", "presentation"],
            "business_request_types": ["read", "write"],
            "worker_visibility": "all_public_descriptions_upfront_per_business_request",
            "tool_visibility": "worker_private_tool_dag_planned_before_execution",
            "runtime_build": RUNTIME_BUILD,
            "service_config_hash": str((context or {}).get("service_config_hash") or ""),
            "service_environment": str((context or {}).get("service_environment") or ""),
            "service_deployment_mode": str((context or {}).get("service_deployment_mode") or ""),
            "access_model_version": ACCESS_MODEL_VERSION,
            "completion_contract_version": COMPLETION_CONTRACT_VERSION,
            "completion_report_version": COMPLETION_REPORT_VERSION,
            "evidence_analysis_report_version": EVIDENCE_ANALYSIS_REPORT_VERSION,
        },
        run_id=effective_run_id,
    )
    try:
        result = coordinator.execute(
            query=str(query or ""),
            decomposition=dict(decomposition or {}),
            user_id=str(user_id or "default"),
            default_top_k=max(1, min(int(default_top_k or 50), 100)),
            session_id=str(session_id or f"session_{user_id}"),
            run_id=effective_run_id,
            language=str(language or "zh"),
            execution_context=dict(context or {}),
        )
    finally:
        coordinator.close()
        flow_event(
            "GRAPH_RUNTIME_CLOSED",
            {
                "graph_id": str(
                    getattr(getattr(coordinator, "store", None), "graph_id", "")
                )
            },
            run_id=effective_run_id,
        )
    runtime = result.setdefault("graph_runtime", {})
    runtime.update({
        "runtime_build": RUNTIME_BUILD,
        "access_model_version": ACCESS_MODEL_VERSION,
        "completion_contract_version": COMPLETION_CONTRACT_VERSION,
        "completion_report_version": COMPLETION_REPORT_VERSION,
        "evidence_analysis_report_version": EVIDENCE_ANALYSIS_REPORT_VERSION,
    })
    runtime["llm_binding"] = binding.public_dict()
    runtime["llm_binding"]["single_service_identity"] = True
    return result


def execute_control_action(
    *,
    action: str,
    query: str = "",
    proposal_id: str = "",
    idempotency_key: str = "",
    user_id: str = "default",
    session_id: str = "",
    run_id: str = "",
    language: str = "zh",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Thin UI compatibility entry that delegates to the canonical WRITE runtime.

    V23.0.17 has no third control runtime. The public function is retained only
    because the existing application UI calls it directly; it does
    not own proposal resolution, approval, mutation, or commit logic.
    """

    del language
    normalized = str(action or "").strip().lower()
    action_type = {
        "confirm": "confirm_execute",
        "confirm_execute": "confirm_execute",
        "reject": "reject",
        "cancel": "cancel",
    }.get(normalized)
    if action_type is None:
        raise ValueError(f"unsupported_write_action:{normalized}")

    merged_context = dict(context or {})
    resolved_proposal_id = str(proposal_id or "").strip()
    if resolved_proposal_id:
        merged_context["proposal_id"] = resolved_proposal_id

    result = WriteRequestExecutor(output_dir=output_dir, db_path=db_path).execute(
        action_type=action_type,
        query=str(query or ""),
        user_id=str(user_id or "default"),
        session_id=str(session_id or ""),
        run_id=str(run_id or ""),
        context=merged_context,
        idempotency_key=str(idempotency_key or ""),
    )
    # Existing page code reads ``answer``.  This is presentation-only and does
    # not reintroduce a second control/business execution path.
    result.setdefault("answer", str(result.get("outcome") or ""))
    return result


def should_use_financial_graph_agent(intent: str, decomposition: dict[str, Any] | None = None) -> bool:
    del decomposition
    return str(intent or "") == "financial_graph_agent"


def clear_financial_graph_agent_session(
    session_id: str,
    *,
    output_dir: str | Path = "outputs",
    hard: bool = True,
) -> dict[str, int]:
    return SessionStateStore(output_dir=output_dir).clear_session(session_id, hard=hard)
