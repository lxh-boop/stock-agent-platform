"""Capability tools for the main agent: domain reads, proposals, gated writes.

Domain read tools (evidence / internal data / graph relations / risk) are
exposed directly to the agent — no specialist sub-loops. Preview tools create
pending confirmation plans (no business effect, no gate);
``confirm_pending_proposal`` and the graph-write tools are approval-gated —
calling them raises a LangGraph ``interrupt()`` that pauses the run until the
user answers the pending-actions prompt.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from agent_graph.messages_utils import json_view
from agent_graph.tools.adapter import wrap_tool_definition

_SENSITIVE_KEYS = {"confirmation_token", "token", "plan_hash", "confirmation_token_hash"}


def _trim_tool_result(result: Any, limit: int = 4000) -> str:
    if hasattr(result, "to_dict"):
        result = result.to_dict()
    if not isinstance(result, dict):
        result = {"success": False, "message": str(result)}
    payload = {key: value for key, value in result.items() if key not in _SENSITIVE_KEYS}
    data = dict(payload.get("data") or {})
    for secret in list(_SENSITIVE_KEYS):
        data.pop(secret, None)
    payload["data"] = data
    return json_view(payload, limit=limit)


def _preview_add_stock(context: dict[str, Any]):
    from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper

    class Args(BaseModel):
        stock_code: str = Field(description="6 位证券代码，如 600519")
        requested_weight: float | None = Field(default=None, description="目标权重（0-1 之间的小数）；不填则由系统推荐")

    def _run(stock_code: str, requested_weight: float | None = None) -> str:
        result = preview_add_stock_to_paper(
            user_id=str(context.get("user_id") or "default"),
            stock_code=stock_code,
            requested_weight=requested_weight,
            output_dir=str(context.get("output_dir") or "outputs"),
            db_path=context.get("db_path"),
            session_id=str(context.get("session_id") or ""),
        )
        return _trim_tool_result(result)

    return StructuredTool.from_function(
        func=_run,
        name="preview_add_stock",
        description=(
            "生成『新增一只股票到模拟盘组合』的待审批方案（不执行）。"
            "返回 plan_id 与方案摘要；用户需在待确认操作面板批准后才会执行。"
        ),
        args_schema=Args,
    )


def _preview_adjust_position(context: dict[str, Any]):
    from agent_graph.domain_tools.rebalance_plan_tool import preview_adjust_position_to_weight

    class Args(BaseModel):
        stock_code: str = Field(description="6 位证券代码")
        requested_weight: float | None = Field(default=None, description="目标权重（0-1）")
        position_adjustment_ratio: float | None = Field(default=None, description="持仓调整比例（0-1）")
        requested_quantity: int | None = Field(default=None, description="目标股数（100 的整数倍）")

    def _run(
        stock_code: str,
        requested_weight: float | None = None,
        position_adjustment_ratio: float | None = None,
        requested_quantity: int | None = None,
    ) -> str:
        result = preview_adjust_position_to_weight(
            user_id=str(context.get("user_id") or "default"),
            stock_code=stock_code,
            requested_weight=requested_weight,
            position_adjustment_ratio=position_adjustment_ratio,
            requested_quantity=requested_quantity,
            output_dir=str(context.get("output_dir") or "outputs"),
            db_path=context.get("db_path"),
            session_id=str(context.get("session_id") or ""),
        )
        return _trim_tool_result(result)

    return StructuredTool.from_function(
        func=_run,
        name="preview_adjust_position",
        description="生成『把某持仓调整到目标权重/数量』的待审批方案（不执行）。",
        args_schema=Args,
    )


def _preview_manual_operation(context: dict[str, Any]):
    from agent_graph.domain_tools.manual_position_operation_tool import preview_manual_position_operation

    class Args(BaseModel):
        stock_code: str | None = Field(default=None, description="6 位证券代码；组合级操作可留空")
        requested_weight: float | None = Field(default=None, description="目标权重（0-1）")
        position_adjustment_ratio: float | None = Field(default=None, description="持仓调整比例（0-1）")
        requested_quantity: int | None = Field(default=None, description="目标股数（100 的整数倍）")
        cash_weight: float | None = Field(default=None, description="目标现金权重（0-1）")
        target_position_count: int | None = Field(default=None, description="目标持仓只数")
        query: str = Field(default="", description="用一句中文描述想要的组合调整意图")

    def _run(**kwargs: Any) -> str:
        result = preview_manual_position_operation(
            user_id=str(context.get("user_id") or "default"),
            output_dir=str(context.get("output_dir") or "outputs"),
            db_path=context.get("db_path"),
            session_id=str(context.get("session_id") or ""),
            **kwargs,
        )
        return _trim_tool_result(result)

    return StructuredTool.from_function(
        func=_run,
        name="preview_manual_operation",
        description=(
            "生成任意组合调整的待审批方案（不执行）：单票增减仓、组合再平衡、"
            "现金管理、目标持仓只数等。描述不清时用 query 传中文意图。"
        ),
        args_schema=Args,
    )


def _confirm_pending_proposal(context: dict[str, Any]):
    from agent_graph.writes import execute_control_action

    class Args(BaseModel):
        query: str = Field(default="", description="要确认执行的内容描述（可附 proposal_id）")
        proposal_id: str = Field(default="", description="可选；待执行方案的 proposal_id")
        intent: str = Field(default="confirm", description="confirm=确认执行；reject=拒绝")

    def _run(query: str = "", proposal_id: str = "", intent: str = "confirm") -> str:
        # Approval is enforced by HumanInTheLoopMiddleware before this body runs.
        result = execute_control_action(
            action="confirm" if str(intent).lower() != "reject" else "reject",
            query=query,
            proposal_id=proposal_id,
            user_id=str(context.get("user_id") or "default"),
            session_id=str(context.get("session_id") or ""),
            run_id=str(context.get("run_id") or ""),
            output_dir=str(context.get("output_dir") or "outputs"),
            db_path=context.get("db_path"),
            context=dict(context),
        )
        message = str(result.get("answer") or result.get("outcome") or result.get("message") or "操作已完成")
        return json_view(
            {
                "status": "completed" if result.get("success") else "failed",
                "answer": message[:2000],
                "business_effect_applied": bool(result.get("business_effect_applied")),
                "proposal_status": result.get("proposal_status"),
            },
            limit=3000,
        )

    return StructuredTool.from_function(
        func=_run,
        name="confirm_pending_proposal",
        description=(
            "确认执行（或拒绝）一个待审批方案。调用后会暂停等待用户在待确认操作面板批准；"
            "仅当用户明确要求执行/拒绝已有方案时调用。"
        ),
        args_schema=Args,
    )


def _graph_write_tools(registry: Any, context: dict[str, Any]) -> list[StructuredTool]:
    tools: list[StructuredTool] = []
    for tool_id in ("database.write_portfolio_graph_context", "database.write_evidence_graph_context"):
        definition = registry.get(tool_id) if registry is not None else None
        if definition is None or not definition.enabled:
            continue
        tools.append(wrap_tool_definition(definition, dict(context), worker_id="W08"))
    return tools




# --------------------------------------------------------------------------
# domain read tools: exposed directly, one wrap per legacy contract
# --------------------------------------------------------------------------

_READ_TOOL_IDS = [
    "evidence.search_news",
    "evidence.search_rag",
    "internal.prediction.get_stock",
    "internal.ranking.get_latest",
    "internal.entity.resolve_ranked_security",
    "internal.model.get_metrics",
    "internal.strategy.get_selected",
    "internal.portfolio.get_state",
    "internal.account.get_state",
    "internal.user_profile.get",
    "graph.relations.read_neighborhood",
    "graph.relations.find_paths",
]

_TOOL_PURPOSE = {
    "evidence.search_news": "检索目标实体的新闻与公告证据",
    "evidence.search_rag": "RAG 文档检索（需权威 entity_refs）",
    "internal.prediction.get_stock": "读取某只股票的模型预测/评分",
    "internal.ranking.get_latest": "读取最新全市场模型排名",
    "internal.entity.resolve_ranked_security": "把排名记录解析成权威 GraphRef 身份",
    "internal.model.get_metrics": "读取模型质量指标",
    "internal.strategy.get_selected": "读取当前选定策略",
    "internal.portfolio.get_state": "读取当前模拟盘组合与持仓",
    "internal.account.get_state": "读取账户状态",
    "internal.user_profile.get": "读取用户画像与约束",
    "graph.relations.read_neighborhood": "在权威 entity_refs 范围读取邻域关系",
    "graph.relations.find_paths": "读取权威来源与目标之间的关联路径",
}


def _risk_analyzer_tool(context: dict[str, Any]) -> StructuredTool | None:
    """Deterministic portfolio-risk capability: provider.analyze_risk, no LLM."""

    from pydantic import create_model

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
            return json_view({"success": False, "message": f"风险计算失败: {str(exc)[:300]}"})
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        return json_view(result, limit=6000)

    args_model = create_model(
        "RiskAnalyzerArgs",
        objective=(str, Field(default="", description="可选；本次风险评估的关注点")),
    )
    return StructuredTool.from_function(
        func=_run,
        name="risk_analyzer",
        description="基于系统内当前模拟盘组合直接计算风险事实（集中度、行业暴露、账户风险）；无需传入持仓数据。",
        args_schema=args_model,
    )


def build_read_tools(registry: Any, context: dict[str, Any]) -> list[StructuredTool]:
    """Domain read tools straight on the main agent (agent-as-tool, no sub-loops)."""

    tools: list[StructuredTool] = []
    if registry is not None:
        for tool_id in _READ_TOOL_IDS:
            definition = registry.get(tool_id)
            if definition is None or not definition.enabled:
                continue
            wrapped = wrap_tool_definition(definition, dict(context), worker_id="main")
            purpose = _TOOL_PURPOSE.get(tool_id)
            if purpose and purpose not in wrapped.description:
                wrapped.description = f"{wrapped.description.splitlines()[0]}（{purpose}）"
            tools.append(wrapped)
    risk_tool = _risk_analyzer_tool(context)
    if risk_tool is not None:
        tools.append(risk_tool)
    from agent_graph.tools.backtest_tools import build_backtest_tools
    tools.extend(build_backtest_tools(context))
    return tools


def build_main_capability_tools(registry: Any, context: dict[str, Any]) -> list[StructuredTool]:
    """Assemble the main agent's direct capability tools (proposals + gated writes)."""

    tools: list[StructuredTool] = [
        _preview_add_stock(context),
        _preview_adjust_position(context),
        _preview_manual_operation(context),
        _confirm_pending_proposal(context),
    ]
    tools.extend(_graph_write_tools(registry, context))
    return tools


__all__ = ["build_main_capability_tools", "build_read_tools"]
