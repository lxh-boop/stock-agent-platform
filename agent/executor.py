from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from core.llm import LLMExecutionDependencies, LLMRuntimeSettings, LLMService, resolve_active_llm_settings
from core.llm.contracts import LLMConfigurationError, LLMJSONError, LLMProviderError
from core.llm.dependencies import register_llm_execution_dependencies

from agent.collaboration import execute_unified_agent_request
from agent.collaboration.planner import CoordinatorPlanningError
from agent.graph.errors import GraphConfigurationError, GraphUnavailableError
from agent.console_trace import (
    finalize_flow_markdown,
    flow_event,
    trace_event,
    trace_exception,
)
from agent.llm_audit import activate_llm_audit_context
from agent.runtime import (
    AgentRuntimeRecorder,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIALLY_COMPLETED,
    RUN_PLANNING,
    RUN_RUNNING,
    RUN_WAITING_FOR_APPROVAL,
)
from agent.top_k import DEFAULT_TOOL_TOP_K


def _language(query: str, explicit: str | None) -> str:
    value = str(explicit or "").strip().lower()
    if value in {"zh", "zh-cn", "chinese"}:
        return "zh"
    if value in {"en", "english"}:
        return "en"
    return "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in str(query or "")) else "en"


def _sanitize_context(value: dict[str, Any] | None, *, user_id: str, session_id: str) -> dict[str, Any]:
    context = dict(value or {})
    # Runtime identity is authoritative. Model/planner-generated account_id is not
    # accepted as a public identity in the graph runtime.
    context.pop("account_id", None)
    context["user_id"] = user_id
    context["session_id"] = session_id
    # as_of_time/date is retained only when explicitly supplied by the caller.
    if "as_of_time" not in context and "as_of_date" in context:
        context["as_of_time"] = str(context.get("as_of_date") or "")
    return context


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _failure_descriptor(exc: Exception, language: str) -> dict[str, Any]:
    chain = _exception_chain(exc)
    messages = " | ".join(str(item) for item in chain)

    if any(isinstance(item, (LLMJSONError, CoordinatorPlanningError)) for item in chain):
        return {
            "code": "main_agent_planning_failed",
            "stage": "worker_planning",
            "message": (
                "MainAgent 未能生成符合 Worker 输入、输出和依赖合同的执行计划，本次请求尚未进入 Worker 执行阶段。"
                if language == "zh"
                else "The MainAgent could not produce a Worker DAG that satisfies the Worker input, output, and dependency contracts. Worker execution did not start."
            ),
            "retryable": True,
        }

    if any(isinstance(item, (LLMConfigurationError, LLMProviderError)) for item in chain):
        return {
            "code": "llm_service_unavailable",
            "stage": "llm_execution",
            "message": (
                "当前配置的本地模型服务不可用，本次请求未完成。"
                if language == "zh"
                else "The configured local model service is unavailable, so the request could not be completed."
            ),
            "retryable": True,
        }

    if any(isinstance(item, (GraphConfigurationError, GraphUnavailableError)) for item in chain):
        return {
            "code": "financial_graph_unavailable",
            "stage": "graph_runtime_initialization",
            "message": (
                "金融图运行时不可用，请检查 Neo4j 配置、连接和主数据初始化。"
                if language == "zh"
                else "The financial-graph runtime is unavailable. Check Neo4j configuration, connectivity, and master-data initialization."
            ),
            "retryable": True,
        }

    lowered = messages.lower()
    if "neo4j" in lowered or "graphunavailable" in lowered:
        return {
            "code": "financial_graph_unavailable",
            "stage": "graph_runtime",
            "message": (
                "金融图运行时不可用，请检查 Neo4j 配置、连接和主数据初始化。"
                if language == "zh"
                else "The financial-graph runtime is unavailable. Check Neo4j configuration, connectivity, and master-data initialization."
            ),
            "retryable": True,
        }

    return {
        "code": "agent_runtime_failed",
        "stage": "agent_runtime",
        "message": (
            "Agent 运行过程中发生技术错误，本次请求未完成。"
            if language == "zh"
            else "A technical error occurred in the Agent runtime, so the request was not completed."
        ),
        "retryable": True,
    }


def _exception_diagnostics(exc: Exception) -> dict[str, Any]:
    for item in _exception_chain(exc):
        diagnostics = getattr(item, "diagnostics", None)
        if isinstance(diagnostics, dict):
            return dict(diagnostics)
    return {}


def _empty_failure(
    *,
    exc: Exception,
    query: str,
    user_id: str,
    session_id: str,
    run_id: str,
    language: str,
) -> dict[str, Any]:
    failure = _failure_descriptor(exc, language)
    diagnostics = _exception_diagnostics(exc)
    error_text = f"{type(exc).__name__}:{exc}"
    planner_snapshot = {
        "status": "failed",
        "failure_code": failure["code"],
        "failure_stage": failure["stage"],
        "candidate_plan_diagnostics": diagnostics,
        "worker_selection_owner": "main_agent",
        "dag_mutation_after_planning": "forbidden",
    }
    graph_runtime = {
        "contract_version": "financial_graph_runtime.v1",
        "planner": planner_snapshot,
        "worker_dag": {
            "contract_version": "worker_dag_snapshot.v1",
            "task_count": 0,
            "tasks": [],
            "execution_batches": [],
            "execution_order": [],
        },
    }
    return {
        "success": False,
        "run_id": run_id,
        "runtime": {"run_id": run_id, "status": RUN_FAILED},
        "intent": "financial_graph_agent",
        "parameters": {},
        "original_query": query,
        "resolved_query": "",
        "reply_language": language,
        "decomposition": {
            "query": query,
            "route_layer": "single_main_agent_graph_entry",
            "tasks": [],
            "user_goal": {"raw_message": query, "resolved_message": query},
            "diagnostics": {
                "single_llm_service": True,
                "failure": failure,
            },
        },
        "orchestration": {
            "success": False,
            "answer": failure["message"],
            "execution_status": "failed",
            "worker_results": {},
            "execution_batches": [],
            "agent_timeline": [],
            "internal_tool_call_count": 0,
            "missing_context": [],
            "warnings": [],
            "errors": [error_text],
            "graph_runtime": graph_runtime,
            "failure": failure,
        },
        "routing_layer": "single_main_agent_graph_entry",
        "answer": failure["message"],
        "result": {
            "success": False,
            "message": failure["message"],
            "data": {
                "graph_runtime": graph_runtime,
                "failure": failure,
            },
            "errors": [error_text],
            "tool_name": "financial_graph_agent",
            "status": "failed",
            "requires_confirmation": False,
            "plan_id": "",
        },
        "tool_calls": [],
        "context": {"user_id": user_id, "session_id": session_id},
        "context_warnings": [],
    }


def run_agent_request(
    query: str,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = DEFAULT_TOOL_TOP_K,
    session_id: str = "",
    reply_language: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_settings: LLMRuntimeSettings | None = None,
    llm_mode: str | None = None,
    decomposition_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Formal single entry for the Neo4j/GraphRef hard-cut runtime.

    Public inputs contain no stock_code, stock_codes, ts_code, security_scope,
    old intent, or old AgentTask contract. Provider identifiers are resolved only
    behind the Worker-private graph adapter.
    """

    run_started = time.perf_counter()
    raw_query = str(query or "").strip()
    user_id = str(user_id or "default")
    session_id = str(session_id or f"session_{user_id}")
    language = _language(raw_query, reply_language)
    active_llm = llm_settings or resolve_active_llm_settings(
        mode=llm_mode,
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=llm_model,
    )
    llm_service = LLMService(settings=active_llm)
    runtime = AgentRuntimeRecorder(
        user_id=user_id,
        goal=raw_query,
        db_path=db_path,
        session_id=session_id,
    )
    register_llm_execution_dependencies(
        runtime.run_id,
        LLMExecutionDependencies(llm_service=llm_service),
    )
    runtime.merge_metadata({
        "formal_entry_audit": {
            "formal_entry_used": True,
            "formal_entry_name": "agent.executor.run_agent_request",
            "run_id": runtime.run_id,
            "conversation_id": session_id,
        },
        "financial_graph_runtime": {
            "public_contract": "GraphRef",
            "task_contract": "graph_agent_task.v1",
            "result_contract": "graph_worker_result.v1",
        },
        "llm_runtime_snapshot": {**active_llm.public_dict, "config_hash": active_llm.config_hash},
    })
    activate_llm_audit_context(
        run_id=runtime.run_id,
        conversation_id=session_id,
        output_dir=output_dir,
        formal_entry_used=True,
        formal_entry_name="agent.executor.run_agent_request",
    )
    context = _sanitize_context(decomposition_context, user_id=user_id, session_id=session_id)
    context["run_id"] = runtime.run_id
    context["llm_runtime_settings"] = active_llm
    context["llm_profile_id"] = llm_service.profile_id
    context["llm_config_hash"] = llm_service.config_hash
    trace_event(
        "executor.graph_request.received",
        {"query": raw_query, "user_id": user_id, "session_id": session_id, "top_k": top_k, "language": language},
        run_id=runtime.run_id,
    )
    flow_event(
        "GRAPH_REQUEST",
        {
            "conversation_id": session_id,
            "user_id": user_id,
            "raw_message": raw_query,
            "language": language,
            "next_step": "resolve GraphRefs, plan Worker tasks, and execute through private graph adapters",
        },
        run_id=runtime.run_id,
    )
    runtime.transition_run(RUN_PLANNING, "financial_graph_single_entry")
    try:
        runtime.transition_run(RUN_RUNNING, "graph_worker_execution")
        execution = execute_unified_agent_request(
            query=raw_query,
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
            default_top_k=max(1, min(int(top_k or DEFAULT_TOOL_TOP_K), 100)),
            session_id=session_id,
            run_id=runtime.run_id,
            language=language,
            llm_service=llm_service,
            runtime_recorder=runtime,
            context=context,
        )
    except Exception as exc:
        trace_exception("executor.graph_request.failed", exc, run_id=runtime.run_id)
        try:
            runtime.transition_run(RUN_FAILED, f"{type(exc).__name__}:{exc}")
        except Exception:
            pass
        failure = _empty_failure(
            exc=exc,
            query=raw_query,
            user_id=user_id,
            session_id=session_id,
            run_id=runtime.run_id,
            language=language,
        )
        failure.setdefault("orchestration", {})["run_total_duration_ms"] = round(
            (time.perf_counter() - run_started) * 1000.0, 3
        )
        flow_event(
            "RUN_FAILED",
            {
                "failure": failure.get("orchestration", {}).get("failure", {}),
                "errors": failure.get("orchestration", {}).get("errors", []),
                "planner": failure.get("orchestration", {}).get("graph_runtime", {}).get("planner", {}),
            },
            run_id=runtime.run_id,
            level="ERROR",
        )
        finalize_flow_markdown(
            run_id=runtime.run_id,
            question=raw_query,
            execution=failure.get("orchestration"),
            runtime_status=RUN_FAILED,
            success=False,
            final_answer=str(failure.get("answer") or ""),
            user_id=user_id,
            session_id=session_id,
            language=language,
            llm_runtime={
                **active_llm.public_dict,
                "config_hash": active_llm.config_hash,
            },
        )
        return failure

    execution_status = str(execution.get("execution_status") or "failed")
    proposal_id = ""
    for payload in (execution.get("worker_results") or {}).values():
        if not isinstance(payload, dict):
            continue
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        proposal_id = proposal_id or str(metadata.get("proposal_id") or "")
    requires_confirmation = bool(proposal_id) and str(execution.get("control_action") or "").lower() not in {"confirm", "reject"}
    if execution_status == "waiting_context":
        final_runtime_status = RUN_PARTIALLY_COMPLETED
    elif requires_confirmation:
        final_runtime_status = RUN_WAITING_FOR_APPROVAL
    elif bool(execution.get("success")):
        final_runtime_status = RUN_COMPLETED
    elif execution_status == "partially_completed":
        final_runtime_status = RUN_PARTIALLY_COMPLETED
    else:
        final_runtime_status = RUN_FAILED
    try:
        runtime.transition_run(final_runtime_status, execution_status)
    except Exception:
        pass

    worker_results = dict(execution.get("worker_results") or {})
    agent_tasks = [
        {
            "task_id": str(task_id),
            "intent": str(payload.get("metadata", {}).get("task_type") or "graph_worker_task") if isinstance(payload, dict) else "graph_worker_task",
            "assigned_agent": str(payload.get("agent_id") or "") if isinstance(payload, dict) else "",
            "parameters": {},
            "depends_on": [],
            "reason": str(payload.get("summary") or "")[:300] if isinstance(payload, dict) else "",
            "confidence": float(payload.get("confidence") or 0.0) if isinstance(payload, dict) else 0.0,
            "capability_status": str(payload.get("status") or "unknown") if isinstance(payload, dict) else "unknown",
        }
        for task_id, payload in worker_results.items()
    ]
    decomposition = {
        "query": raw_query,
        "route_layer": "single_main_agent_graph_entry",
        "tasks": agent_tasks,
        "is_multi_intent": len(agent_tasks) > 1,
        "need_clarification": bool(execution.get("need_clarification")),
        "clarification_question": str(execution.get("clarification_question") or ""),
        "confidence": 1.0,
        "warnings": list(execution.get("warnings") or []),
        "user_goal": {
            "raw_message": raw_query,
            "resolved_message": raw_query,
            "action": "coordinate_graph_workers",
            "objects": [],
            "constraints": [],
            "expected_outputs": ["worker_results", "business_outputs"],
            "requires_write": requires_confirmation,
        },
        "task_plan": {
            "tasks": agent_tasks,
            "planning_level": "worker_agent",
            "tool_visibility": "main_agent_none_worker_progressive_private",
        },
        "diagnostics": {
            "llm_used": True,
            "decision_source": "existing_main_coordinator",
            "single_llm_service": True,
            "llm_profile_id": llm_service.profile_id,
            "llm_config_hash": llm_service.config_hash,
        },
    }
    orchestration = {
        "success": bool(execution.get("success")),
        "run_total_duration_ms": round((time.perf_counter() - run_started) * 1000.0, 3),
        "answer": str(execution.get("answer") or ""),
        "worker_results": worker_results,
        "business_outputs": dict(execution.get("business_outputs") or {}),
        "tool_calls": [],
        "internal_tool_call_count": int(execution.get("internal_tool_call_count") or 0),
        "execution_order": list(execution.get("execution_order") or []),
        "execution_batches": list(execution.get("execution_batches") or []),
        "warnings": list(execution.get("warnings") or []),
        "errors": list(execution.get("errors") or []),
        "execution_status": execution_status,
        "observations": list(execution.get("observations") or []),
        "replan_audit": list(execution.get("replan_audit") or []),
        "replan_count": int(execution.get("replan_count") or 0),
        "invalid_replan_block_count": int(execution.get("invalid_replan_block_count") or 0),
        "replan_limits": dict(execution.get("replan_limits") or {}),
        "agent_timeline": list(execution.get("agent_timeline") or []),
        "handoff": dict(execution.get("handoff") or {}),
        "graph_runtime": dict(execution.get("graph_runtime") or {}),
    }
    result_data = {
        "graph_runtime": dict(execution.get("graph_runtime") or {}),
        "worker_results": worker_results,
        "business_outputs": dict(execution.get("business_outputs") or {}),
        "missing_context": list(execution.get("missing_context") or []),
        "need_clarification": bool(execution.get("need_clarification")),
        "clarification_question": str(execution.get("clarification_question") or ""),
        "safe_to_write": True,
    }
    if proposal_id:
        result_data["proposal_id"] = proposal_id
    result = {
        "success": bool(execution.get("success")),
        "message": str(execution.get("answer") or ""),
        "data": result_data,
        "warnings": list(execution.get("warnings") or []),
        "errors": list(execution.get("errors") or []),
        "tool_name": "financial_graph_agent",
        "status": execution_status,
        "requires_confirmation": requires_confirmation,
        "proposal_id": proposal_id,
    }
    trace_event(
        "executor.graph_request.completed",
        {
            "execution_status": execution_status,
            "worker_task_count": len(agent_tasks),
            "internal_tool_call_count": orchestration["internal_tool_call_count"],
        },
        run_id=runtime.run_id,
    )
    response = {
        "success": bool(execution.get("success")),
        "run_id": runtime.run_id,
        "formal_entry_audit": {
            "formal_entry_used": True,
            "formal_entry_name": "agent.executor.run_agent_request",
            "run_id": runtime.run_id,
            "conversation_id": session_id,
        },
        "runtime": {"run_id": runtime.run_id, "status": final_runtime_status},
        "intent": "financial_graph_agent",
        "parameters": {},
        "original_query": raw_query,
        "resolved_query": "",
        "reply_language": language,
        "decomposition": decomposition,
        "orchestration": orchestration,
        "routing_layer": "single_main_agent_graph_entry",
        "answer": str(execution.get("answer") or ""),
        "result": result,
        "tool_calls": [],
        "context": {
            "user_id": user_id,
            "session_id": session_id,
            "graph_refs": (execution.get("graph_runtime") or {}).get("focus_refs") or [],
        },
        "context_warnings": [],
    }
    finalize_flow_markdown(
        run_id=runtime.run_id,
        question=raw_query,
        execution=orchestration,
        runtime_status=final_runtime_status,
        success=bool(execution.get("success")),
        final_answer=str(execution.get("answer") or ""),
        user_id=user_id,
        session_id=session_id,
        language=language,
        llm_runtime={
            **active_llm.public_dict,
            "config_hash": active_llm.config_hash,
        },
    )
    return response


__all__ = ["run_agent_request"]
