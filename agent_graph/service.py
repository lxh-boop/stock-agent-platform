"""Public entry for the LangGraph engine.

``run_langgraph_agent_request`` mirrors the response contract of
``agent.executor.run_agent_request`` so the Task API, SSE pipeline and React
frontend work unchanged. ``resume_langgraph_agent`` completes an interrupted
run after the user answers a write-approval prompt (pending-actions flow).
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from langgraph.types import Command

from core.llm.runtime_settings import LLMRuntimeSettings, resolve_active_llm_settings

from agent_graph.graph import _latest_results, build_agent_graph
from agent_graph.llm import build_chat_model
from agent_graph.memory import open_persistence
from agent_graph.state import WorkerResult
from agent_graph.tools.runtime import AgentResources
from agent_graph.trace_adapter import GraphRunTracer
from agent_graph.workers.factory import build_all_worker_agents
from agent_graph.tools.registry import build_worker_tools

_ROUTING_LAYER = "langgraph_supervisor"
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


def _tool_call_count(results: list[WorkerResult]) -> int:
    return sum(len(list(r.get("tool_calls") or [])) for r in results)


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
        "decomposition": {},
        "orchestration": {
            "agent_timeline": [],
            "internal_tool_call_count": 0,
            "missing_context": [],
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


def _memory_summary(session_id: str, output_dir: str) -> str:
    if not session_id:
        return ""
    try:
        from agent_graph.session.state_store import SessionStateStore

        store = SessionStateStore(output_dir)
        return str(store.build_summary(session_id) or "")
    except Exception:  # noqa: BLE001 - session memory is best-effort
        return ""


def _run_id_tracer(
    *,
    query: str,
    user_id: str,
    session_id: str,
    db_path: str | Path | None,
    run_id: str | None = None,
) -> GraphRunTracer:
    return GraphRunTracer(
        user_id=user_id,
        goal=query,
        db_path=db_path,
        session_id=session_id,
        run_id=run_id,
    )


def _build_runtime(query: str, llm_settings: LLMRuntimeSettings | None, kwargs: dict[str, Any]):
    """Resolve LLM binding once; both fresh runs and resumes share this path."""

    active_llm = llm_settings or resolve_active_llm_settings(
        mode=kwargs.get("llm_mode"),
        api_key=kwargs.get("llm_api_key"),
        base_url=kwargs.get("llm_base_url"),
        model=kwargs.get("llm_model"),
    )
    chat_model = build_chat_model(active_llm)
    return active_llm, chat_model


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
    tracer: GraphRunTracer | None = None
    resources: AgentResources | None = None

    try:
        active_llm, chat_model = _build_runtime(
            raw_query,
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
            query=raw_query,
            user_id=user_id,
            session_id=session_id,
            language=language,
            run_id="",
            message=f"LLM 运行时解析失败: {exc}"[:300],
            error_type="llm_settings_failure",
        )

    tracer = _run_id_tracer(
        query=raw_query,
        user_id=user_id,
        session_id=session_id,
        db_path=db_path,
    )
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
            query=raw_query,
            user_id=user_id,
            session_id=session_id,
            language=language,
            run_id=run_id,
            message=f"金融图存储不可用: {exc}"[:300],
            error_type="graph_unavailable",
        )

    try:
        tools_by_worker = {
            card.worker_id: build_worker_tools(resources.tool_registry, resources.context, card.worker_id)
            for card in _card_list()
        }
        agents = build_all_worker_agents(chat_model, tools_by_worker)

        initial_state = {
            "query": raw_query,
            "user_id": user_id,
            "session_id": session_id,
            "reply_language": language,
            "run_id": run_id,
            "memory_summary": _memory_summary(session_id, str(output_dir)),
        }
        config = {
            "configurable": {
                "thread_id": session_id,
                "user_id": user_id,
                "run_id": run_id,
            },
            "recursion_limit": 80,
        }

        tracer.planning()
        composed: dict[str, Any] = {}
        with ExitStack() as stack:
            checkpointer = None
            try:
                checkpointer, _store = stack.enter_context(open_persistence())
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                warnings_extra = [f"持久化不可用，审批中断将不可恢复: {exc}"[:200]]
            else:
                warnings_extra = []
            graph = build_agent_graph(
                chat_model, resources, agents, run_meta, checkpointer=checkpointer
            )
            config = {
                "configurable": {
                    "thread_id": session_id,
                    "user_id": user_id,
                    "run_id": run_id,
                },
                "recursion_limit": 80,
            }
            final_state = dict(graph.invoke(initial_state, config=config) or {})
            if warnings_extra:
                final_state.setdefault("warnings", [])
                final_state["warnings"] = list(final_state["warnings"]) + warnings_extra
            composed = _compose_response(
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
        return composed
    except Exception as exc:  # noqa: BLE001 - mirror executor failure contract
        tracer.finalize(success=False, answer="", warnings=[], errors=[str(exc)], started_at=run_started)
        return _failure_response(
            query=raw_query,
            user_id=user_id,
            session_id=session_id,
            language=language,
            run_id=run_id,
            message=f"Agent 运行失败: {exc}"[:300],
            error_type="agent_run_failure",
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
    tracer = _run_id_tracer(
        query="(resumed run)",
        user_id=user_id,
        session_id=session_id,
        db_path=db_path,
        run_id=str(run_id),
    )
    try:
        active_llm, chat_model = _build_runtime("", llm_settings, {})
        resources = AgentResources.build(
            user_id=str(user_id or "default"),
            session_id=session_id,
            run_id=tracer.run_id,
            output_dir=str(output_dir),
            db_path=str(db_path) if db_path else None,
        )
        tools_by_worker = {
            card.worker_id: build_worker_tools(resources.tool_registry, resources.context, card.worker_id)
            for card in _card_list()
        }
        agents = build_all_worker_agents(chat_model, tools_by_worker)
        with open_persistence() as (checkpointer, _store):
            if checkpointer is None:
                raise RuntimeError("checkpointer_unavailable_for_resume")
            graph = build_agent_graph(chat_model, resources, agents, run_meta, checkpointer=checkpointer)
            config = {
                "configurable": {"thread_id": session_id, "user_id": user_id, "run_id": tracer.run_id},
                "recursion_limit": 80,
            }
            final_state = dict(
                graph.invoke(
                    Command(resume={"decision": decision, "reason": reason}),
                    config=config,
                )
                or {}
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
            query="",
            user_id=str(user_id or "default"),
            session_id=session_id,
            language=language,
            run_id=tracer.run_id,
            message=f"恢复运行失败: {exc}"[:300],
            error_type="agent_resume_failure",
        )
    finally:
        if resources is not None:
            resources.close()


def _card_list():
    from agent_graph.workers.cards import CapabilityWorkerDirectory

    return CapabilityWorkerDirectory().list()


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
    requests = list(final_state.get("requests") or [])
    worker_results = list(final_state.get("worker_results") or [])
    warnings = list(final_state.get("warnings") or [])
    errors = list(final_state.get("errors") or [])

    # Record the run into agent_runs so /runs/{run_id} pages keep working.
    interrupted = bool(final_state.get("pending_approval")) or "__interrupt__" in final_state
    if not interrupted:
        tracer.stage("decompose", {"request_count": len(requests)})
        if final_state.get("worker_tasks"):
            tracer.stage("plan", {"task_count": len(final_state["worker_tasks"])})
        latest = _latest_results(worker_results)
        for task in (final_state.get("worker_tasks") or []):
            tracer.worker_started(task)
        for result in worker_results:
            tracer.worker_finished(result)
        if final_state.get("final_answer"):
            tracer.stage("synthesize")
        success = bool(final_state.get("final_answer")) and not any(
            r.get("status") == "failed" for r in latest.values()
        )
        status = "completed" if success else "failed"
        tracer.finalize(
            success=success,
            answer=str(final_state.get("final_answer") or ""),
            warnings=warnings,
            errors=errors,
            started_at=run_started,
        )
    else:
        status = "waiting_for_approval"
        tracer.approval_requested(dict(final_state.get("pending_approval") or {}))
        tracer.waiting()

    pending = final_state.get("pending_approval")
    if interrupted:
        answer = (
            "执行写操作前需要你的确认："
            + str((pending or {}).get("summary") or (pending or {}).get("tool_name") or "请确认该操作")
        )
        requires_confirmation = True
        plan_id = plan_id_for_run(run_id)
    else:
        answer = str(final_state.get("final_answer") or "")
        requires_confirmation = False
        plan_id = ""

    duration_ms = round((time.perf_counter() - run_started) * 1000)
    success = not errors and bool(answer)
    return {
        "success": bool(success),
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
        "decomposition": {"requests": requests},
        "orchestration": {
            "agent_timeline": [],
            "run_total_duration_ms": duration_ms,
            "internal_tool_call_count": _tool_call_count(worker_results),
            "worker_results": worker_results,
            "missing_context": [],
            "stage_timings_ms": dict(run_meta),
            "warnings": warnings,
            "errors": errors,
        },
        "routing_layer": _ROUTING_LAYER,
        "answer": answer,
        "result": {
            "success": bool(success),
            "message": answer[:500],
            "data": {
                "worker_results": worker_results,
                "pending_approval": pending or None,
                "write_outcome": final_state.get("write_outcome"),
            },
            "errors": errors,
            "tool_name": "financial_graph_agent",
            "status": "waiting_for_approval" if interrupted else ("completed" if success else "failed"),
            "requires_confirmation": requires_confirmation,
            "plan_id": plan_id,
        },
        "tool_calls": [],
        "context": {
            "user_id": user_id,
            "session_id": session_id,
            "graph_refs": [
                ref
                for result in worker_results
                for ref in list(result.get("entity_refs") or [])[:2]
            ][:8],
        },
        "context_warnings": warnings,
    }


__all__ = [
    "run_langgraph_agent_request",
    "resume_langgraph_agent",
    "plan_id_for_run",
    "run_id_from_plan_id",
]
