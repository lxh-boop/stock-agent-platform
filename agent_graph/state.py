"""Shared state model for the single-agent (agent-as-tool) engine.

Graph shape: START -> agent -> (approval_gate -> agent)? -> END. The main
agent owns the whole conversation loop; specialists and write capabilities
are plain tools on it. Write-tool approvals surface as top-level
``interrupt()`` calls so they survive checkpointing.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ApprovalRequest(TypedDict, total=False):
    """Payload handed to ``interrupt()`` when a write tool needs confirmation."""

    action: str  # constant "write_approval"
    gate: str  # "tool" | "confirm_proposal"
    tool_name: str
    worker_id: str
    args: dict[str, Any]
    summary: str


class AgentState(TypedDict, total=False):
    # request identity
    query: str
    user_id: str
    session_id: str
    reply_language: str
    run_id: str

    # main agent loop transcript (subgraph node state)
    messages: Annotated[list[AnyMessage], add_messages]

    # agent output
    final_answer: str
    tool_trace: list[dict[str, Any]]
    entity_refs: list[dict[str, Any]]
    duration_ms: float

    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]

    # approval gate
    pending_approval: ApprovalRequest | None
    approval_decision: str | None


__all__ = ["AgentState", "ApprovalRequest"]
