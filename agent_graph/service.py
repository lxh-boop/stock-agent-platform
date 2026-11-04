"""Public entry for the LangGraph single-agent engine.

``run_langgraph_agent_request`` mirrors the response contract of the legacy
executor so the Task API, SSE pipeline and React frontend work unchanged.
``resume_langgraph_agent`` completes an interrupted run after the user answers
a write-approval prompt (pending-actions flow).
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from langgraph.types import Command

from core.llm.runtime_settings import LLMRuntimeSettings, resolve_active_llm_settings

from agent_graph.graph import build_agent_graph, build_initial_messages
from agent_graph.messages_utils import summarize_messages
from agent_graph.llm import build_chat_model
from agent_graph.memory import open_checkpointer
from agent_graph.tools.runtime import AgentResources
from agent_graph.trace_adapter import GraphRunTracer

_ROUTING_LAYER = "langgraph_single_agent"
_FORMAL_ENTRY = "agent_graph.service.run_langgraph_agent_request"
_PLAN_ID_PREFIX = "lg_approve_"


def plan_id_for_run(run_id: str) -> str:
    return f"{_PLAN_ID_PREFIX}{run_id}"


def run_id_from_plan_id(plan_id: str) -> str:
    prefix = _PLAN_ID_PREFIX
    return str(plan_id)[len(prefix):] if str(plan_id).startswith(prefix) else ""


def _language(query: str, reply_language: str | None) -> str:
    if reply_language:
        return str(reply_language)
    return "zh"


def _failure_response(
    *,
    query: str,
    user_id: str,
    session_id: str,
    language: str,
    run_id: str,
    message: str,
    error_type: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "run_id": run_id,
        "formal_entry_audit": {
            "formal_entry_used": True,
            "formal_entry_name": _FORMAL_ENTRY,
            "run_id": run_id,
            "conversation_id": session_id,
        },
        "runtime": {"run_id": run_id, "status": "failed"},
        "intent": "financial_graph_agent",
        "parameters": {},
        "original_query": query,
        "resolved_query": "",
        "reply_language": language,
        "orchestration": {
            "agent_timeline": [],
            "internal_tool_call_count": 0,
            "tool_trace": [],
            "warnings": [],
            "errors": [f"{error_type}: {message}"],
        },
        "routing_layer": _ROUTING_LAYER,
        "answer": message,
        "result": {
            "success": False,
            "message": message,
            "data": {},
            "errors": [f"{error_type}: {message}"],
            "tool_name": "financial_graph_agent",
            "status": "failed",
            "requires_confirmation": False,
            "plan_id": "",
        },
        "tool_calls": [],
        "context": {"user_id": user_id, "session_id": session_id, "graph_refs": []},
        "context_warnings": [],
    }


def _resolve_model(llm_settings: LLMRuntimeSettings | None, kwargs: dict[str, Any]):
    active_llm = llm_settings or resolve_active_llm_settings(
        mode=kwargs.get("llm_mode"),
        api_key=kwargs.get("llm_api_key"),
        base_url=kwargs.get("llm_base_url"),
        model=kwargs.get("llm_model"),
    )
    return active_llm, build_chat_model(active_llm)


def _invoke_with_persistence(graph, chat_model, resources, run_meta, initial_state, config):
    """Invoke once with a checkpointer when PostgreSQL is reachable."""

    with ExitStack() as stack:
        checkpointer = None
        persistence_warnings: list[str] = []
        try:
            checkpointer = stack.enter_context(open_checkpointer())
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            persistence_warnings = [f"持久化不可用，审批中断将不可恢复: {exc}"[:200]]
        effective = graph
        if checkpointer is not None:
            effective = build_agent_graph(chat_model, resources, run_meta, checkpointer=checkpointer)
        final_state = dict(effective.invoke(initial_state, config=config) or {})
        if persistence_warnings:
            final_state.setdefault("warnings", [])
            final_state["warnings"] = list(final_state["warnings"]) + persistence_warnings
        return final_state


def run_langgraph_agent_request(
    query: str,
    user_id: str = "default",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    session_id: str = "",
    reply_language: str | None = None,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_settings: LLMRuntimeSettings | None = None,
    llm_mode: str | None = None,
    decomposition_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one agent run on the LangGraph engine; executor-shaped response."""

    run_started = time.perf_counter()
    raw_query = str(query or "").strip()
    user_id = str(user_id or "default")
    session_id = str(session_id or f"session_{user_id}")
    language = _language(raw_query, reply_language)
    run_meta: dict[str, Any] = {}
    resources: AgentResources | None = None

    try:
        _active_llm, chat_model = _resolve_model(
            llm_settings,
            {
                "llm_mode": llm_mode,
                "llm_api_key": llm_api_key,
                "llm_base_url": llm_base_url,
                "llm_model": llm_model,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _failure_response(
            query=raw_query, user_id=user_id, session_id=session_id, language=language,
            run_id="", message=f"LLM 运行时解析失败: {exc}"[:300], error_type="llm_settings_failure",
        )

    tracer = GraphRunTracer(user_id=user_id, goal=raw_query, db_path=db_path, session_id=session_id)
    run_id = tracer.run_id

    try:
        resources = AgentResources.build(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
            output_dir=str(output_dir),
            db_path=str(db_path) if db_path else None,
        )
    except Exception as exc:  # noqa: BLE001 - graph store unavailable
        tracer.finalize(success=False, answer="", warnings=[], errors=[str(exc)], started_at=run_started)
        return _failure_response(
            query=raw_query, user_id=user_id, session_id=session_id, language=language,
            run_id=run_id, message=f"金融图存储不可用: {exc}"[:300], error_type="graph_unavailable",
        )

    try:
        graph = build_agent_graph(chat_model, resources, run_meta)
        initial_state = {
            "query": raw_query,
            "user_id": user_id,
            "session_id": session_id,
            "reply_language": language,
            "run_id": run_id,
            "messages": build_initial_messages({"query": raw_query, "reply_language": language}),
        }
        config = {
            "configurable": {"thread_id": session_id, "user_id": user_id, "run_id": run_id},
            "recursion_limit": 96,
        }
        tracer.planning()
        final_state = _invoke_with_persistence(
            graph, chat_model, resources, run_meta, initial_state, config
        )
        return _compose_response(
            final_state=final_state,
            query=raw_query,
            user_id=user_id,
            session_id=session_id,
            language=language,
            run_id=run_id,
            run_started=run_started,
            run_meta=run_meta,
            tracer=tracer,
        )
    except Exception as exc:  # noqa: BLE001 - mirror executor failure contract
        tracer.finalize(success=False, answer="", warnings=[], errors=[str(exc)], started_at=run_started)
        return _failure_response(
            query=raw_query, user_id=user_id, session_id=session_id, language=language,
            run_id=run_id, message=f"Agent 运行失败: {exc}"[:300], error_type="agent_run_failure",
        )
    finally:
        if resources is not None:
            resources.close()


def resume_langgraph_agent(
    *,
    session_id: str,
    user_id: str,
    run_id: str,
    decision: str,
    reason: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    llm_settings: LLMRuntimeSettings | None = None,
) -> dict[str, Any]:
    """Resume a run paused at a write-approval interrupt (pending-actions flow)."""

    run_started = time.perf_counter()
    session_id = str(session_id or f"session_{user_id}")
    language = "zh"
    run_meta: dict[str, Any] = {}
    resources: AgentResources | None = None
    tracer = GraphRunTracer(
        user_id=str(user_id or "default"), goal="(resumed run)", db_path=db_path,
        session_id=session_id, run_id=str(run_id),
    )
    try:
        _active_llm, chat_model = _resolve_model(llm_settings, {})
        resources = AgentResources.build(
            user_id=str(user_id or "default"),
            session_id=session_id,
            run_id=tracer.run_id,
            output_dir=str(output_dir),
            db_path=str(db_path) if db_path else None,
        )
        with open_checkpointer() as checkpointer:
            if checkpointer is None:
                raise RuntimeError("checkpointer_unavailable_for_resume")
            graph = build_agent_graph(chat_model, resources, run_meta, checkpointer=checkpointer)
            config = {
                "configurable": {"thread_id": session_id, "user_id": user_id, "run_id": tracer.run_id},
                "recursion_limit": 96,
            }
            final_state = dict(
                graph.invoke(Command(resume={"decision": decision, "reason": reason}), config=config) or {}
            )
        return _compose_response(
            final_state=final_state,
            query=str(final_state.get("query") or ""),
            user_id=str(user_id or "default"),
            session_id=session_id,
            language=language,
            run_id=tracer.run_id,
            run_started=run_started,
            run_meta=run_meta,
            tracer=tracer,
        )
    except Exception as exc:  # noqa: BLE001
        tracer.finalize(success=False, answer="", warnings=[], errors=[str(exc)], started_at=run_started)
        return _failure_response(
            query="", user_id=str(user_id or "default"), session_id=session_id, language=language,
            run_id=tracer.run_id, message=f"恢复运行失败: {exc}"[:300], error_type="agent_resume_failure",
        )
    finally:
        if resources is not None:
            resources.close()


def _compose_response(
    *,
    final_state: dict[str, Any],
    query: str,
    user_id: str,
    session_id: str,
    language: str,
    run_id: str,
    run_started: float,
    run_meta: dict[str, Any],
    tracer: GraphRunTracer,
) -> dict[str, Any]:
    answer, tool_trace, graph_refs = summarize_messages(list(final_state.get("messages") or []))
    warnings = list(final_state.get("warnings") or [])
    errors = list(final_state.get("errors") or [])

    interrupted = bool(final_state.get("pending_approval")) or "__interrupt__" in final_state
    if not interrupted:
        tracer.planning()
        tracer.running()
        tracer.agent_loop_step()
        tracer.record_tool_trace(tool_trace)
        tracer.finish_agent_step(bool(answer))
        success = bool(answer) and not errors
        tracer.finalize(
            success=success,
            answer=answer,
            warnings=warnings,
            errors=errors,
            started_at=run_started,
        )
        status = "completed" if success else "failed"
        requires_confirmation = False
        pending = None
        plan_id = ""
    else:
        pending = dict(final_state.get("pending_approval") or {})
        tracer.approval_requested(pending)
        tracer.waiting()
        status = "waiting_for_approval"
        answer = (
            "执行写操作前需要你的确认："
            + str(pending.get("summary") or pending.get("tool_name") or "请确认该操作")
        )
        requires_confirmation = True
        plan_id = plan_id_for_run(run_id)
        success = False

    duration_ms = round((time.perf_counter() - run_started) * 1000)
    return {
        "success": bool(success or requires_confirmation),
        "run_id": run_id,
        "formal_entry_audit": {
            "formal_entry_used": True,
            "formal_entry_name": _FORMAL_ENTRY,
            "run_id": run_id,
            "conversation_id": session_id,
        },
        "runtime": {"run_id": run_id, "status": status},
        "intent": "financial_graph_agent",
        "parameters": {},
        "original_query": query,
        "resolved_query": "",
        "reply_language": language,
        "orchestration": {
            "agent_timeline": [],
            "run_total_duration_ms": duration_ms,
            "internal_tool_call_count": len(tool_trace),
            "tool_trace": tool_trace,
            "stage_timings_ms": dict(run_meta),
            "warnings": warnings,
            "errors": errors,
        },
        "routing_layer": _ROUTING_LAYER,
        "answer": answer,
        "result": {
            "success": bool(success or requires_confirmation),
            "message": answer[:500],
            "data": {
                "tool_trace": tool_trace,
                "entity_refs": graph_refs[:8],
                "pending_approval": pending or None,
            },
            "errors": errors,
            "tool_name": "financial_graph_agent",
            "status": status,
            "requires_confirmation": requires_confirmation,
            "plan_id": plan_id,
        },
        "tool_calls": tool_trace,
        "context": {
            "user_id": user_id,
            "session_id": session_id,
            "graph_refs": graph_refs[:8],
        },
        "context_warnings": warnings,
    }


__all__ = [
    "run_langgraph_agent_request",
    "resume_langgraph_agent",
    "plan_id_for_run",
    "run_id_from_plan_id",
]
