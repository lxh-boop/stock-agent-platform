"""Main LangGraph orchestration for the agent engine.

Graph shape::

    START -> decompose -> route -> general_answer -> END
                              |-> write_gate --(read requests left)-> plan
                              |-> plan --Send--> worker (provider stage, parallel)
    worker -> collect --Send--> worker (consumer stage / retry once)
                    |-> approval_gate (interrupt; re-dispatch pre-approved)
                    |-> synthesize -> END

Provider workers (card stage ``provider``/``mutation``) run first; consumer
workers (analysis/decision stages) receive the collected sibling results. A
failed worker is re-dispatched at most once, mirroring the legacy
``max_replans=1`` semantics. Write approvals surface as top-level
``interrupt()`` calls so they survive checkpointing.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from agent_graph.writes import execute_control_action
from agent_graph.workers.cards import CapabilityWorkerDirectory

from agent_graph.approval import decision_from_resume, write_request_payload
from agent_graph.llm import generate_structured
from agent_graph.prompts import (
    DECOMPOSE_SYSTEM_PROMPT,
    GENERAL_ANSWER_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    REPLAN_EXTRA_HINT,
    SYNTHESIZE_SYSTEM_PROMPT,
)
from agent_graph.schemas import Decomposition, WorkerPlan
from agent_graph.state import AgentState, WorkerResult, WorkerTask
from agent_graph.tools.runtime import AgentResources
from agent_graph.workers.factory import worker_card

_REJECT_PATTERN = re.compile(r"拒绝|取消|驳回|撤回", re.IGNORECASE)
_PROVIDER_STAGES = {"provider", "mutation"}
_MAX_RESULT_JSON = 8000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _json_view(value: Any, limit: int = _MAX_RESULT_JSON) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text[:limit]


def _extract_entity_refs(obj: Any, depth: int = 0, seen: set[int] | None = None) -> list[dict[str, Any]]:
    """Recursively collect GraphRef-shaped dicts (authoritative ids) from payloads."""

    if depth > 6:
        return []
    seen = seen if seen is not None else set()
    if isinstance(obj, dict):
        marker = id(obj)
        if marker in seen:
            return []
        seen.add(marker)
        if "node_id" in obj and ("entity_type" in obj or "graph_id" in obj or "kind" in obj):
            return [obj]
        refs: list[dict[str, Any]] = []
        for value in obj.values():
            refs.extend(_extract_entity_refs(value, depth + 1, seen))
        return refs
    if isinstance(obj, list):
        refs: list[dict[str, Any]] = []
        for item in obj:
            refs.extend(_extract_entity_refs(item, depth + 1, seen))
        return refs
    return []


def _worker_branch(
    task: dict[str, Any],
    state: dict[str, Any],
    *,
    sibling_results: list[dict[str, Any]] | None = None,
    pre_approved_tools: dict[str, str] | None = None,
) -> dict[str, Any]:
    requests = list(state.get("requests") or [])
    request_index = int(task.get("request_index") or 1)
    request = requests[request_index - 1] if 0 < request_index <= len(requests) else {}
    return {
        "task": dict(task),
        "request": request,
        "presentation": dict(state.get("presentation") or {}),
        "memory_summary": str(state.get("memory_summary") or ""),
        "reply_language": str(state.get("reply_language") or "zh"),
        "sibling_results": list(sibling_results or []),
        "pre_approved_tools": dict(pre_approved_tools or {}),
    }


def _latest_results(results: list[WorkerResult]) -> dict[str, WorkerResult]:
    latest: dict[str, WorkerResult] = {}
    for result in results:
        task_id = str(result.get("task_id"))
        existing = latest.get(task_id)
        if existing is None or int(result.get("attempt") or 0) >= int(existing.get("attempt") or 0):
            latest[task_id] = result
    return latest


def _worker_descriptions_view() -> list[dict[str, str]]:
    return [
        {
            "worker_id": card.worker_id,
            "role": card.role,
            "short_description": card.short_description,
            "execution_stage": card.execution_stage,
        }
        for card in CapabilityWorkerDirectory().list()
    ]


# --------------------------------------------------------------------------
# supervisor nodes
# --------------------------------------------------------------------------


def _make_decompose_node(model, run_meta: dict[str, Any]):
    def decompose_node(state: dict[str, Any]) -> dict[str, Any]:
        query = str(state.get("query") or "")
        started = time.perf_counter()
        user_view = {
            "user_message": query,
            "conversation_context": {"session_summary": str(state.get("memory_summary") or "")[:3000]},
            "current_reply_language": str(state.get("reply_language") or "zh"),
        }
        try:
            decomposition = generate_structured(
                model,
                Decomposition,
                [
                    ("system", DECOMPOSE_SYSTEM_PROMPT),
                    ("user", _json_view(user_view, 8000)),
                ],
            )
            requests = [item.model_dump() for item in decomposition.requests]
            category = "general" if all(r["category"] == "general" for r in requests) else "business"
        except Exception as exc:  # noqa: BLE001 - degrade to one business request
            return {
                "requests": [_fallback_request(query)],
                "category": "business",
                "presentation": {},
                "warnings": [f"请求拆解失败，已按单一业务请求降级: {exc}"[:300]],
            }
        run_meta["decompose_ms"] = round((time.perf_counter() - started) * 1000)
        presentation: dict[str, Any] = {}
        for item in requests:
            if item.get("category") == "presentation":
                presentation.update(item.get("presentation") or {})
        return {"requests": requests, "category": category, "presentation": presentation}

    return decompose_node


def _fallback_request(query: str) -> dict[str, Any]:
    return {
        "source_index": 1,
        "category": "business",
        "objective": query,
        "request_type": "read",
        "proposal_required": False,
        "semantic_target": {"entity_type": "unknown", "display_text": "", "source": "none", "status": "missing"},
        "semantic_entities": [],
        "presentation": {},
        "depends_on": [],
    }


def _route_after_decompose(state: dict[str, Any]) -> str:
    if str(state.get("category") or "") == "general":
        return "general_answer"
    for request in state.get("requests") or []:
        if str(request.get("request_type") or "") == "write":
            return "write_gate"
    return "plan"


def _make_general_answer_node(model):
    def general_answer_node(state: dict[str, Any]) -> dict[str, Any]:
        query = str(state.get("query") or "")
        language = str(state.get("reply_language") or "zh")
        try:
            response = model.invoke(
                [
                    ("system", GENERAL_ANSWER_SYSTEM_PROMPT),
                    ("user", f"回复语言：{language}\n用户输入：{query}"),
                ]
            )
            content = getattr(response, "content", "")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            text = str(content or "").strip()
        except Exception as exc:  # noqa: BLE001
            return {"final_answer": f"处理该请求时出现问题：{exc}", "errors": [str(exc)[:300]]}
        return {"final_answer": text}

    return general_answer_node


def _make_plan_node(model, run_meta: dict[str, Any]):
    def plan_node(state: dict[str, Any]) -> Command:
        all_requests = list(state.get("requests") or [])
        readable = [
            (index, r)
            for index, r in enumerate(all_requests, start=1)
            if r.get("category") == "business" and str(r.get("request_type") or "read") == "read"
        ]
        if not readable:
            return Command(goto="synthesize")
        started = time.perf_counter()
        user_view = {
            "requests": [
                {"request_index": index, "objective": str(r.get("objective") or "")}
                for index, r in readable
            ],
            "session_summary": str(state.get("memory_summary") or "")[:1400],
            "worker_descriptions": _worker_descriptions_view(),
        }
        try:
            plan = generate_structured(
                model,
                WorkerPlan,
                [
                    ("system", PLAN_SYSTEM_PROMPT),
                    ("user", _json_view(user_view, 8000)),
                ],
            )
            tasks = _compile_tasks(plan, readable)
        except Exception as exc:  # noqa: BLE001 - deterministic recovery: one provider task
            fallback = WorkerTask(
                task_id="t1",
                worker_id="W02",
                objective=str(readable[0][1].get("objective") or ""),
                request_index=readable[0][0],
                attempt=0,
                stage="provider",
            )
            return Command(
                update={
                    "worker_tasks": [fallback],
                    "dispatched_task_ids": [fallback["task_id"]],
                    "warnings": [f"任务规划失败，已按单一内部数据任务降级: {exc}"[:300]],
                },
                goto=[Send("worker", _worker_branch(fallback, state))],
            )
        run_meta["plan_ms"] = round((time.perf_counter() - started) * 1000)
        if not tasks:
            tasks = [
                WorkerTask(
                    task_id="t1",
                    worker_id="W02",
                    objective=str(readable[0][1].get("objective") or ""),
                    request_index=readable[0][0],
                    attempt=0,
                    stage="provider",
                )
            ]
        first_batch = [task for task in tasks if str(task.get("stage")) in _PROVIDER_STAGES]
        if not first_batch:
            first_batch = tasks[:1]
        return Command(
            update={
                "worker_tasks": tasks,
                "dispatched_task_ids": [task["task_id"] for task in first_batch],
            },
            goto=[Send("worker", _worker_branch(task, state)) for task in first_batch],
        )

    return plan_node


def _compile_tasks(plan: WorkerPlan, readable: list[tuple[int, dict[str, Any]]]) -> list[WorkerTask]:
    directory = CapabilityWorkerDirectory()
    valid_ids = {card.worker_id for card in directory.list()}
    valid_indexes = {index for index, _ in readable}
    tasks: list[WorkerTask] = []
    seen_pairs: set[tuple[str, int]] = set()
    for index, assignment in enumerate(plan.tasks, start=1):
        worker_id = str(assignment.worker_id).strip().upper()
        request_index = int(assignment.request_index or readable[0][0])
        if worker_id not in valid_ids or request_index not in valid_indexes:
            continue
        pair = (worker_id, request_index)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        stage = worker_card(worker_id).execution_stage
        if stage == "report":  # composing is the synthesize node's job
            continue
        tasks.append(
            WorkerTask(
                task_id=f"t{index}",
                worker_id=worker_id,
                objective=str(assignment.objective or "").strip(),
                request_index=request_index,
                attempt=0,
                stage=stage,
            )
        )
    return tasks


# --------------------------------------------------------------------------
# worker execution
# --------------------------------------------------------------------------


def _compose_worker_prompt(branch: dict[str, Any]) -> str:
    task = branch["task"]
    request = branch.get("request") or {}
    lines = [
        f"任务目标：{task.get('objective')}",
        f"所属用户请求：{request.get('objective')}",
    ]
    semantic_entities = request.get("semantic_entities") or []
    if semantic_entities:
        lines.append("语义实体（自然语言，禁止当作权威ID）：" + _json_view(semantic_entities))
    semantic_target = request.get("semantic_target") or {}
    if semantic_target.get("display_text"):
        lines.append(f"语义目标对象：{semantic_target['display_text']}")
    sibling_results = branch.get("sibling_results") or []
    if sibling_results:
        compact = [
            {
                "worker_id": r.get("worker_id"),
                "status": r.get("status"),
                "answer": (r.get("answer") or "")[:800],
                "entity_refs": list(r.get("entity_refs") or [])[:4],
            }
            for r in sibling_results
        ]
        lines.append("其他Worker已完成结论：" + _json_view(compact))
    if branch.get("memory_summary"):
        lines.append("会话记忆摘要：" + str(branch["memory_summary"])[:800])
    if branch.get("pre_approved_tools"):
        lines.append("已获用户批准的写操作：" + "、".join(branch["pre_approved_tools"].keys()))
    if int(task.get("attempt") or 0) > 0:
        lines.append(REPLAN_EXTRA_HINT)
    lines.append(f"回复语言：{branch.get('reply_language') or 'zh'}")
    return "\n".join(lines)


def _summarize_messages(messages: list[Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    final_text = ""
    tool_calls: list[dict[str, Any]] = []
    refs: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, AIMessage):
            content = message.content
            if isinstance(content, str) and content.strip():
                final_text = content.strip()
            elif isinstance(content, list):
                joined = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                ).strip()
                if joined:
                    final_text = joined
            for call in getattr(message, "tool_calls", None) or []:
                tool_calls.append({"tool": call.get("name"), "args": call.get("args")})
        else:
            payload = getattr(message, "content", None)
            if isinstance(payload, str) and payload:
                try:
                    parsed = json.loads(payload)
                except Exception:  # noqa: BLE001
                    parsed = payload
                refs.extend(_extract_entity_refs(parsed)[:4])
    return final_text, tool_calls, refs


def _make_worker_node(agents: dict[str, Any], resources: AgentResources):
    def worker_node(branch: dict[str, Any], config: RunnableConfig | None = None) -> dict[str, Any]:
        task: dict[str, Any] = branch["task"]
        worker_id = str(task.get("worker_id"))
        attempt = int(task.get("attempt") or 0)
        task_id = str(task.get("task_id"))
        started = time.perf_counter()
        base_result: dict[str, Any] = {
            "task_id": task_id,
            "worker_id": worker_id,
            "request_index": int(task.get("request_index") or 1),
            "answer": "",
            "tool_calls": [],
            "entity_refs": [],
            "errors": [],
            "attempt": attempt,
        }

        # Per-branch approval grants are injected through the shared run context
        # that tool closures captured at build time.
        if branch.get("pre_approved_tools"):
            resources.context["pre_approved_tools"] = dict(branch["pre_approved_tools"])
        else:
            resources.context.pop("pre_approved_tools", None)

        agent = agents.get(worker_id)
        if agent is None:
            return {
                "worker_results": [{**base_result, "status": "failed", "errors": [f"未知Worker: {worker_id}"]}]
            }
        prompt = _compose_worker_prompt(branch)
        try:
            result = agent.invoke({"messages": [HumanMessage(content=prompt)]}, {"recursion_limit": 24})
        except Exception as exc:  # noqa: BLE001 - worker failures become structured results
            return {
                "worker_results": [
                    {**base_result, "status": "failed", "errors": [f"worker执行异常: {str(exc)[:300]}"]}
                ],
                "errors": [f"worker {worker_id} failed: {str(exc)[:200]}"],
            }
        if isinstance(result, dict) and result.get("__interrupt__"):
            payload = None
            for item in result["__interrupt__"] or ():
                value = getattr(item, "value", None)
                if isinstance(value, dict):
                    payload = value
                    break
            if payload is not None:
                waiting = {
                    **base_result,
                    "status": "waiting_approval",
                    "answer": f"写操作等待用户审批: {payload.get('tool_name')}",
                }
                return {
                    "worker_results": [waiting],
                    "pending_approval": {**payload, "gate": "tool", "task_id": task_id, "branch": branch},
                }
            return {"worker_results": [{**base_result, "status": "failed", "errors": ["unknown interrupt"]}]}
        messages = list((result or {}).get("messages") or [])
        final_text, tool_calls, refs = _summarize_messages(messages)
        status = "completed" if final_text else "failed"
        return {
            "worker_results": [
                {
                    **base_result,
                    "status": status,
                    "answer": final_text,
                    "tool_calls": tool_calls,
                    "entity_refs": refs[:8],
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "errors": [] if status == "completed" else ["worker未产出结论"],
                }
            ]
        }

    return worker_node


# --------------------------------------------------------------------------
# collect / batch routing / approval / synthesis
# --------------------------------------------------------------------------


def _make_collect_node():
    """Batching router: retries, consumer dispatch, approval, or synthesis.

    Returns a ``Command`` directly (the pattern nodes like ``plan`` use);
    conditional-edge routing with Command values proved unreliable here.
    """

    def collect_node(state: dict[str, Any]) -> Command:
        tasks = list(state.get("worker_tasks") or [])
        dispatched = set(state.get("dispatched_task_ids") or [])
        retried = list(state.get("retried_task_ids") or [])
        latest = _latest_results(list(state.get("worker_results") or []))
        completed_ids = {tid for tid, r in latest.items() if r.get("status") == "completed"}

        # Tool-level approval pending: surface the top-level interrupt gate.
        if state.get("pending_approval"):
            return Command(goto="approval_gate")

        # Phase 1: retry failed tasks once (legacy max_replans=1); consumers
        # wait so they always see the settled provider results.
        retry_sends: list[Send] = []
        new_retried = list(retried)
        for task in tasks:
            tid = str(task.get("task_id"))
            if tid in completed_ids or tid in new_retried or tid not in dispatched:
                continue
            result = latest.get(tid)
            if result is not None and result.get("status") == "failed":
                new_retried.append(tid)
                retry_task = {**task, "attempt": int(task.get("attempt") or 0) + 1}
                retry_sends.append(Send("worker", _worker_branch(retry_task, state)))
        if retry_sends:
            return Command(update={"retried_task_ids": new_retried}, goto=retry_sends)

        # Phase 2: dispatch consumer-stage tasks with sibling results attached.
        consumer_sends: list[Send] = []
        new_dispatched = list(state.get("dispatched_task_ids") or [])
        sibling_results = [latest[tid] for tid in sorted(completed_ids)]
        for task in tasks:
            tid = str(task.get("task_id"))
            if tid in dispatched or tid in completed_ids:
                continue
            new_dispatched.append(tid)
            consumer_sends.append(
                Send("worker", _worker_branch(task, state, sibling_results=sibling_results))
            )
        if consumer_sends:
            return Command(update={"dispatched_task_ids": new_dispatched}, goto=consumer_sends)

        if any(r.get("status") == "waiting_approval" for r in latest.values()):
            return Command(goto="approval_gate")
        return Command(goto="synthesize")

    return collect_node


def _make_approval_gate_node(resources: AgentResources):
    def approval_gate_node(state: dict[str, Any]) -> Command:
        pending = dict(state.get("pending_approval") or {})
        resume_value = interrupt(pending)
        decision = decision_from_resume(resume_value)
        branch = dict(pending.get("branch") or {})
        task_id = str(pending.get("task_id") or "")
        if decision == "approved":
            tool_name = str(pending.get("tool_name") or "")
            pre = dict(branch.get("pre_approved_tools") or {})
            if tool_name:
                pre[tool_name] = "approved"
            task = dict(branch.get("task") or {})
            task = {**task, "task_id": task_id, "attempt": int(task.get("attempt") or 0) + 1}
            branch = {**branch, "pre_approved_tools": pre, "task": task}
            return Command(
                update={"approval_decision": "approved", "pending_approval": None},
                goto=[Send("worker", branch)],
            )
        return Command(
            update={
                "approval_decision": decision,
                "pending_approval": None,
                "warnings": [f"写操作被拒绝: {pending.get('tool_name')}"[:200]],
            },
            goto="synthesize",
        )

    return approval_gate_node


def _make_write_gate_node(resources: AgentResources):
    def write_gate_node(state: dict[str, Any]) -> Command:
        requests = list(state.get("requests") or [])
        write_requests = [r for r in requests if str(r.get("request_type") or "") == "write"]
        read_requests = [
            (index, r)
            for index, r in enumerate(requests, start=1)
            if r.get("category") == "business" and str(r.get("request_type") or "read") == "read"
        ]
        user_id = str(state.get("user_id") or "default")
        session_id = str(state.get("session_id") or "")
        payload = write_request_payload(write_requests, user_id=user_id, session_id=session_id)
        resume_value = interrupt(payload)
        decision = decision_from_resume(resume_value)
        if decision != "approved":
            outcome = {"approved": False, "message": "已按你的要求取消该操作。如需执行，请重新发起并明确确认。"}
            return Command(
                update={"write_outcome": outcome, "approval_decision": decision},
                goto="plan" if read_requests else "synthesize",
            )
        objectives = [str(r.get("objective") or "") for r in write_requests]
        action = "reject" if any(_REJECT_PATTERN.search(text or "") for text in objectives) else "confirm"
        try:
            result = execute_control_action(
                action=action,
                query="；".join(objectives),
                user_id=user_id,
                session_id=session_id,
                run_id=str(state.get("run_id") or ""),
                language=str(state.get("reply_language") or "zh"),
                output_dir=resources.context.get("output_dir") or "outputs",
                db_path=resources.context.get("db_path"),
                context=dict(resources.context),
            )
            message = str((result or {}).get("message") or (result or {}).get("answer") or "操作已完成")
            outcome = {"approved": True, "message": message, "action": action}
            errors: list[str] = []
        except Exception as exc:  # noqa: BLE001 - write failures surface to the user
            message = f"写操作执行失败：{str(exc)[:300]}"
            outcome = {"approved": True, "message": message, "action": action, "failed": True}
            errors = [f"write execute failed: {str(exc)[:200]}"]
        update: dict[str, Any] = {
            "write_outcome": outcome,
            "approval_decision": "approved",
            "write_executed": outcome.get("approved") and not outcome.get("failed"),
        }
        if errors:
            update["errors"] = errors
        return Command(update=update, goto="plan" if read_requests else "synthesize")

    return write_gate_node


def _make_synthesize_node(model, run_meta: dict[str, Any]):
    def synthesize_node(state: dict[str, Any]) -> dict[str, Any]:
        # Write-only requests have no worker material; the gate outcome is the answer.
        if not (state.get("worker_tasks") or state.get("worker_results")):
            outcome = state.get("write_outcome") or {}
            return {"final_answer": str(outcome.get("message") or "操作已处理。")}
        started = time.perf_counter()
        results = list(_latest_results(list(state.get("worker_results") or [])).values())
        task_view = []
        for task in state.get("worker_tasks") or []:
            latest = next((r for r in results if r.get("task_id") == task["task_id"]), None)
            task_view.append(
                {
                    "worker_id": task.get("worker_id"),
                    "objective": task.get("objective"),
                    "status": (latest or {}).get("status", "not_run"),
                    "conclusion": (latest or {}).get("answer", ""),
                }
            )
        user_view = {
            "user_requests": state.get("requests"),
            "worker_results": task_view,
            "write_outcome": state.get("write_outcome"),
            "presentation_policy": state.get("presentation") or {},
            "reply_language": state.get("reply_language") or "zh",
            "approval_decision": state.get("approval_decision"),
        }
        try:
            response = model.invoke(
                [
                    ("system", SYNTHESIZE_SYSTEM_PROMPT),
                    ("user", _json_view(user_view, 16000)),
                ]
            )
            content = getattr(response, "content", "")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            text = str(content or "").strip()
        except Exception as exc:  # noqa: BLE001 - deterministic fallback keeps the run useful
            conclusions = "\n".join(
                f"- [{r.get('worker_id')}] {(r.get('answer') or '；'.join(r.get('errors') or ['(无结论)']))[:300]}"
                for r in results
            )
            text = f"各Worker结论汇总：\n{conclusions}\n（综合报告生成失败：{str(exc)[:200]}）"
        run_meta["synthesize_ms"] = round((time.perf_counter() - started) * 1000)
        return {"final_answer": text}

    return synthesize_node


# --------------------------------------------------------------------------
# graph assembly
# --------------------------------------------------------------------------


def build_agent_graph(
    model,
    resources: AgentResources,
    agents: dict[str, Any],
    run_meta: dict[str, Any] | None = None,
    *,
    checkpointer: Any = None,
):
    """Compile the orchestration graph. One model + one resources bundle per run."""

    meta = run_meta if run_meta is not None else {}
    builder = StateGraph(AgentState)
    builder.add_node("decompose", _make_decompose_node(model, meta))
    builder.add_node("general_answer", _make_general_answer_node(model))
    builder.add_node("plan", _make_plan_node(model, meta))
    builder.add_node("worker", _make_worker_node(agents, resources))
    builder.add_node("collect", _make_collect_node())
    builder.add_node("approval_gate", _make_approval_gate_node(resources))
    builder.add_node("write_gate", _make_write_gate_node(resources))
    builder.add_node("synthesize", _make_synthesize_node(model, meta))

    builder.add_edge(START, "decompose")
    builder.add_conditional_edges("decompose", _route_after_decompose)
    builder.add_edge("general_answer", END)
    builder.add_edge("worker", "collect")
    builder.add_edge("synthesize", END)
    # approval_gate / write_gate / plan / collect route exclusively via Command.
    return builder.compile(checkpointer=checkpointer)


__all__ = ["build_agent_graph"]
