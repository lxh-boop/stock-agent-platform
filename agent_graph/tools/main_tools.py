"""Direct capability tools for the main agent: proposals, confirmation, graph writes.

Preview tools create pending confirmation plans (no business effect, no gate);
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
from langgraph.types import interrupt

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
        decision = interrupt(
            {
                "action": "write_approval",
                "gate": "confirm_proposal",
                "tool_name": "confirm_pending_proposal",
                "summary": query or proposal_id or "确认执行待审批方案",
                "args": {"query": query, "proposal_id": proposal_id, "intent": intent},
            }
        )
        approved = isinstance(decision, dict) and str(decision.get("decision") or "").lower() == "approved"
        if not approved:
            return json_view(
                {
                    "status": "rejected",
                    "answer": "用户未批准该操作，已取消执行。",
                    "entity_refs": [],
                }
            )
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


__all__ = ["build_main_capability_tools"]
