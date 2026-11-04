"""Specialist capabilities exposed as tools (agent-as-tool pattern).

There are no sub-agent graph nodes and no supervisor dispatch: the main agent
simply calls these tools. Three specialists encapsulate their own narrow tool
loop (evidence / internal data / graph relations); risk analysis is a single
deterministic capability tool. Tool whitelists come from the capability cards
(``workers/cards.py``), which remain the single source of tool-ownership
metadata.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent_graph.workers.cards import CapabilityWorkerDirectory

from agent_graph.messages_utils import json_view, summarize_messages
from agent_graph.prompts import SPECIALIST_HINT
from agent_graph.tools.adapter import wrap_tool_definition

_RECURSION_LIMIT = 16
_MAX_RESULT_CHARS = 6000


class SpecialistArgs(BaseModel):
    objective: str = Field(description="本次专项检索要完成的目标，一句明确的中文指令")
    entity_refs: list[dict[str, Any]] = Field(
        default_factory=list,
        description="可选；其他工具结果返回的权威实体引用（GraphRef 原样传入）",
    )


def _specialist_result(answer: str, refs: list[dict[str, Any]], status: str, elapsed_ms: float) -> str:
    return json_view(
        {
            "status": status,
            "answer": answer[:_MAX_RESULT_CHARS],
            "entity_refs": refs[:8],
            "elapsed_ms": elapsed_ms,
        },
        limit=_MAX_RESULT_CHARS + 500,
    )


def _build_specialist_tool(
    model: BaseChatModel,
    registry: Any,
    context: dict[str, Any],
    *,
    tool_name: str,
    card_id: str,
    tool_ids: list[str],
    description: str,
) -> BaseTool | None:
    if registry is None:
        return None
    directory = CapabilityWorkerDirectory()
    card_prompt = directory.private_prompt(card_id)
    tools: list[BaseTool] = []
    for tool_id in tool_ids:
        definition = registry.get(tool_id)
        if definition is None or not definition.enabled:
            continue
        tools.append(wrap_tool_definition(definition, dict(context), worker_id=card_id))
    if not tools:
        return None
    from langchain.agents import create_agent

    specialist_agent = create_agent(
        model,
        tools,
        system_prompt=f"{SPECIALIST_HINT}\n{card_prompt}",
    )

    def _run(objective: str, entity_refs: list[dict[str, Any]] | None = None) -> str:
        started = time.perf_counter()
        entity_refs = list(entity_refs or [])
        user_view = {"objective": objective}
        if entity_refs:
            user_view["authoritative_entity_refs"] = entity_refs
        try:
            result = specialist_agent.invoke(
                {"messages": [HumanMessage(content=json_view(user_view, 6000))]},
                {"recursion_limit": _RECURSION_LIMIT},
            )
        except Exception as exc:  # noqa: BLE001 - specialist failures flow back to the main agent
            return _specialist_result(f"专项执行失败: {str(exc)[:300]}", [], "failed", round((time.perf_counter() - started) * 1000))
        if isinstance(result, dict) and result.get("__interrupt__"):
            return _specialist_result("专项内出现需要审批的写操作，已阻止", [], "blocked", round((time.perf_counter() - started) * 1000))
        messages = list((result or {}).get("messages") or [])
        text, _calls, refs = summarize_messages(messages)
        status = "completed" if text else "failed"
        return _specialist_result(
            text or "专项未产出结论",
            refs,
            status,
            round((time.perf_counter() - started) * 1000),
        )

    return StructuredTool.from_function(
        func=_run,
        name=tool_name,
        description=description,
        args_schema=SpecialistArgs,
    )


class RiskAnalyzerArgs(BaseModel):
    objective: str = Field(default="", description="可选；说明本次风险评估的关注点")


def _build_risk_tool(registry: Any, context: dict[str, Any]) -> BaseTool | None:
    """Deterministic portfolio-risk capability: provider.analyze_risk, no LLM loop."""

    from pydantic import create_model

    # The GraphProviderAdapter rides the run context (set by AgentResources).
    provider = context.get("_provider")
    if provider is None:
        return None

    def _run(objective: str = "") -> str:
        _ = objective
        try:
            result = provider.analyze_risk(
                user_id=str(context.get("user_id") or "default"),
                output_dir=str(context.get("output_dir") or "outputs"),
                db_path=context.get("db_path"),
            )
        except Exception as exc:  # noqa: BLE001
            return json_view({"status": "failed", "answer": f"风险计算失败: {str(exc)[:300]}"})
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        return json_view({"status": "completed", "answer": result}, limit=_MAX_RESULT_CHARS)

    model = create_model("RiskAnalyzerArgs", objective=(str, Field(default="", description="可选；风险评估关注点")))
    return StructuredTool.from_function(
        func=_run,
        name="risk_analyzer",
        description=(
            "基于系统内当前模拟盘组合计算风险事实（集中度、行业暴露、账户风险）。"
            "不需要传入持仓数据；输入 objective 说明关注点即可。"
        ),
        args_schema=model,
    )


def build_specialist_tools(
    model: BaseChatModel,
    registry: Any,
    context: dict[str, Any],
) -> list[BaseTool]:
    """Assemble the specialist tool set for the main agent (agent-as-tool)."""

    specs = [
        {
            "tool_name": "evidence_collector",
            "card_id": "W01",
            "tool_ids": ["evidence.search_news", "evidence.search_rag"],
            "description": (
                "外部证据专项：检索目标实体相关的新闻、公告和 RAG 文档证据，去重合并后返回。"
                "传入 objective 与（可选）权威 entity_refs。"
            ),
        },
        {
            "tool_name": "internal_data_provider",
            "card_id": "W02",
            "tool_ids": [
                "internal.prediction.get_stock", "internal.ranking.get_latest",
                "internal.entity.resolve_ranked_security", "internal.model.get_metrics",
                "internal.backtest.get_summary", "internal.strategy.get_selected",
                "internal.portfolio.get_state", "internal.account.get_state",
                "internal.user_profile.get",
            ],
            "description": (
                "系统内部权威数据专项：模型预测/评分排名、实体解析（自然语言→权威 GraphRef）、"
                "模型质量、回测摘要、当前策略、组合持仓、账户与用户画像。传入 objective。"
            ),
        },
        {
            "tool_name": "graph_relation_retriever",
            "card_id": "W03",
            "tool_ids": ["graph.relations.read_neighborhood", "graph.relations.find_paths"],
            "description": (
                "金融图关系专项：在权威 entity_refs 范围内读取邻域关系与关联路径。"
                "必须传入之前工具结果返回的 entity_refs。"
            ),
        },
    ]
    tools: list[BaseTool] = []
    for spec in specs:
        tool = _build_specialist_tool(model, registry, context, **spec)
        if tool is not None:
            tools.append(tool)
    risk_tool = _build_risk_tool(registry, context)
    if risk_tool is not None:
        tools.append(risk_tool)
    return tools


__all__ = ["build_specialist_tools"]
