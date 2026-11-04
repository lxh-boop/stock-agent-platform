"""Shared state model for the LangGraph agent engine.

The graph flows: decompose -> route -> plan -> (Send fan-out) workers ->
collect -> synthesize -> approval gate -> END. ``worker_results`` uses an
add-reducer because ``Send`` dispatches workers in parallel; errors/warnings
accumulate the same way.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class WorkerTask(TypedDict):
    """One unit of work the supervisor assigns to a specialist worker."""

    task_id: str
    worker_id: str  # W01..W09 card id from agent.collaboration.worker_directory
    objective: str
    request_index: int  # 1-based index into the decomposed request bundle
    attempt: int  # 0 on first dispatch, incremented on re-dispatch
    stage: str  # card execution_stage: provider | analysis | decision | ...


class WorkerResult(TypedDict, total=False):
    """Contract-shaped outcome of one worker subgraph run."""

    task_id: str
    worker_id: str
    request_index: int
    status: str  # completed | failed | waiting_approval
    answer: str
    tool_calls: list[dict[str, Any]]
    entity_refs: list[dict[str, Any]]
    errors: list[str]
    attempt: int
    duration_ms: float


class ApprovalRequest(TypedDict, total=False):
    """Payload handed to ``interrupt()`` when a write tool needs confirmation."""

    action: str  # constant "write_approval"
    gate: str  # "tool" | "write_request"
    tool_name: str
    worker_id: str
    args: dict[str, Any]
    summary: str
    task_id: str
    branch: dict[str, Any]


class AgentState(TypedDict, total=False):
    # request identity
    query: str
    user_id: str
    session_id: str
    reply_language: str
    run_id: str

    # conversation context injected before the run
    memory_summary: str

    # decompose output
    category: str  # general | business
    requests: list[dict[str, Any]]
    presentation: dict[str, Any]

    # plan output
    worker_tasks: list[WorkerTask]
    dispatched_task_ids: list[str]

    # fan-out results (add-reducer across parallel Send branches)
    worker_results: Annotated[list[WorkerResult], operator.add]
    retried_task_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]

    # synthesize output
    final_answer: str
    write_outcome: dict[str, Any]

    # approval gate
    pending_approval: ApprovalRequest | None
    approval_decision: str | None
    write_executed: bool


__all__ = ["AgentState", "WorkerTask", "WorkerResult", "ApprovalRequest"]
