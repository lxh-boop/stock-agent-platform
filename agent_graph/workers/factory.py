"""Specialist worker subgraphs built with ``langchain.agents.create_agent``.

Each worker card keeps its legacy private tool whitelist and private prompt;
the card data itself is reused from ``agent.collaboration.worker_directory``
so both engines share one source of truth.
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool

from agent.collaboration.worker_directory import CapabilityWorkerCard, CapabilityWorkerDirectory

from agent_graph.prompts import REPLAN_EXTRA_HINT, WORKER_SUPERVISOR_HINT
from agent_graph.tools.registry import build_worker_tools


def worker_card(worker_id: str) -> CapabilityWorkerCard:
    return CapabilityWorkerDirectory().get(worker_id)


def worker_system_prompt(worker_id: str, *, attempt: int = 0) -> str:
    card = worker_card(worker_id)
    parts = [WORKER_SUPERVISOR_HINT, card.private_worker_prompt]
    if attempt > 0:
        parts.append(REPLAN_EXTRA_HINT)
    return "\n".join(part for part in parts if part)


def build_worker_agent(
    model: BaseChatModel,
    tools: list[BaseTool],
    worker_id: str,
    *,
    attempt: int = 0,
):
    """Compile one worker subgraph; tool-less workers degrade to a plain LLM node."""

    return create_agent(
        model,
        tools,
        system_prompt=worker_system_prompt(worker_id, attempt=attempt),
    )


def build_all_worker_agents(
    model: BaseChatModel,
    tools_by_worker: dict[str, list[BaseTool]],
) -> dict[str, Any]:
    directory = CapabilityWorkerDirectory()
    agents: dict[str, Any] = {}
    for card in directory.list():
        tools = tools_by_worker.get(card.worker_id) or []
        agents[card.worker_id] = build_worker_agent(model, tools, card.worker_id)
    return agents


__all__ = [
    "build_worker_agent",
    "build_all_worker_agents",
    "worker_card",
    "worker_system_prompt",
]
