"""Hybrid LLM + atomic-tool W04 Risk Analyst.

W04 reads the target object current ContextBundle working memory. Its private Tool DAG
produces deterministic risk facts; the LLM interprets those facts.  Successful
Worker outputs are published through ``data["business_data"]``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm import LLMService

from agent.skill_runtime import get_skill_registry
from agent.skill_runtime.injection import append_skill_guidance
from agent.tool_dag import WorkerToolDagRuntime

from ..models import GraphAgentTask, GraphWorkerResult, ResultStatus
from ..worker_contracts import array_schema, object_schema, string_schema, validate_schema
from .common import materialize_promised_data, safe_public_value
from .structured_output import generate_json_with_local_structural_repair


def _risk_output_schema() -> dict[str, Any]:
    risk_item = object_schema(
        {
            "risk_id": string_schema(min_length=1),
            "category": string_schema(min_length=1),
            "level": string_schema(enum=["low", "medium", "high", "unknown"]),
            "statement": string_schema(min_length=1),
            "source_refs": array_schema(string_schema(min_length=1), min_items=1),
            "scope": {"type": "string"},
        },
        required=["risk_id", "category", "level", "statement", "source_refs", "scope"],
        additional_properties=False,
    )
    constraint = object_schema(
        {
            "constraint_id": string_schema(min_length=1),
            "description": string_schema(min_length=1),
            "source_risk_ids": array_schema(string_schema(min_length=1)),
        },
        required=["constraint_id", "description", "source_risk_ids"],
        additional_properties=False,
    )
    return object_schema(
        {
            "risk_analysis": object_schema(
                {
                    "summary": string_schema(min_length=1),
                    "risk_items": array_schema(risk_item),
                    "limitations": array_schema({"type": "string"}),
                },
                required=["summary", "risk_items", "limitations"],
                additional_properties=False,
            ),
            "records": array_schema(risk_item),
            "risk_constraints": array_schema(constraint),
            "source_tool_result_refs": array_schema(string_schema(min_length=1), min_items=1),
        },
        required=[
            "risk_analysis",
            "records",
            "risk_constraints",
            "source_tool_result_refs",
        ],
        additional_properties=False,
    )


def _final_facts(dag_result: Any) -> tuple[dict[str, Any], list[str]]:
    for result in list(getattr(dag_result, "final_results", []) or []):
        data = dict(getattr(result, "data", {}) or {})
        if "risk_facts" in data:
            return data, [f"tool_result:{task_id}" for task_id in dag_result.final_output_task_ids]
    return {}, []


def _hard_constraint_breaches(facts: dict[str, Any]) -> list[dict[str, Any]]:
    breaches: list[dict[str, Any]] = []
    for item in facts.get("risk_facts") or []:
        if not isinstance(item, dict) or str(item.get("fact_type") or "") != "concentration":
            continue
        for breach in item.get("single_position_limit_breaches") or []:
            if isinstance(breach, dict):
                breaches.append(dict(breach))
    deduped: dict[str, dict[str, Any]] = {}
    for breach in breaches:
        key = str(breach.get("security_ref") or "") or json.dumps(breach, sort_keys=True, ensure_ascii=False)
        deduped[key] = breach
    return sorted(
        deduped.values(),
        key=lambda item: float(item.get("current_asset_weight") or 0.0),
        reverse=True,
    )


def _working_memory_data(working_memory_context: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten the current task object's ContextBundle view by simple data name."""
    context = dict(working_memory_context or {})
    data: dict[str, Any] = {}
    for entity in context.get("entities") or []:
        if isinstance(entity, dict) and isinstance(entity.get("data"), dict):
            for name, value in entity["data"].items():
                data[str(name)] = value
    for name, value in dict(context.get("global_data") or {}).items():
        data[str(name)] = value
    return data


def _upstream_analysis_context(working_memory_context: dict[str, Any] | None) -> dict[str, Any]:
    data = _working_memory_data(working_memory_context)
    return {
        name: safe_public_value(value)
        for name, value in data.items()
        if name not in {"portfolio", "positions", "account", "user_constraints", "user_profile"}
    }


def _business_parameter_context(task: GraphAgentTask) -> dict[str, Any]:
    """Return only explicit business parameters compiled into this task.

    Runtime RequirementResolver has already checked required parameter
    sufficiency.  W04 consumes the values without inventing defaults or
    interpreting their ownership.
    """

    return safe_public_value(dict(task.business_parameters or {}))

def _portfolio_context(working_memory_context: dict[str, Any] | None) -> dict[str, Any]:
    data = _working_memory_data(working_memory_context)
    state = dict(data.get("portfolio") or {}) if isinstance(data.get("portfolio"), dict) else {}
    positions = data.get("positions")
    if isinstance(positions, list):
        state["positions"] = [dict(item) for item in positions if isinstance(item, dict)]
    account = data.get("account")
    if isinstance(account, dict):
        state["account"] = dict(account)
    constraints = data.get("user_constraints")
    if isinstance(constraints, dict):
        state["user_constraints"] = dict(constraints)
    return state


def run_risk(
    llm_service: LLMService,
    tool_dag_runtime: WorkerToolDagRuntime,
    task: GraphAgentTask,
    output_dir: str | Path,
    db_path: str | Path | None,
    *,
    working_memory_context: dict[str, Any] | None = None,
    worker_prompt: str,
    allowed_tool_names: list[str],
    language: str = "zh",
) -> GraphWorkerResult:
    portfolio_payload = _portfolio_context(working_memory_context)
    if not portfolio_payload:
        return GraphWorkerResult(
            task_id=task.task_id,
            agent_id=task.assigned_agent,
            status=ResultStatus.NEED_CONTEXT,
            output_type="PortfolioRiskResult",
            data=None,
            error=None,
            focus_refs=task.focus_refs,
            summary="缺少结构化组合状态，无法进行组合风险分析。",
        )

    # Runtime checks only explicit user-owned parameters. W04 receives the current
    # ContextBundle business view and decides whether its data quality is sufficient.
    upstream_analysis_context = _upstream_analysis_context(working_memory_context)
    business_parameter_context = _business_parameter_context(task)

    available_context = {
        "portfolio_state": safe_public_value(portfolio_payload),
        "risk_question": str(task.args.get("risk_question") or task.objective or ""),
        "user_id": task.user_id,
        "upstream_analysis_context": safe_public_value(upstream_analysis_context),
        "business_parameters": safe_public_value(business_parameter_context),
    }
    dag_result = tool_dag_runtime.run(
        worker_task_id=task.task_id,
        worker_role=task.assigned_agent,
        boundary_id=task.boundary_id,
        worker_objective=task.objective,
        worker_prompt=worker_prompt,
        available_context=available_context,
        required_output_keys=["risk_facts", "source_refs", "limitations"],
        completion_criteria=[
            "根据风险问题选择必要的原子风险事实工具，不使用固定工具链。",
            "最终节点汇总所选工具的结构化风险事实、来源和限制。",
            "所有工具只读，不形成 Proposal，不修改业务状态。",
        ],
        allowed_tool_names=list(allowed_tool_names),
        execution_context={
            "user_id": task.user_id,
            "conversation_id": task.session_id,
            "session_id": task.session_id,
            "run_id": task.run_id,
            "task_id": task.task_id,
            "agent_role": task.assigned_agent,
            "output_dir": output_dir,
            "db_path": db_path,
        },
        read_only=True,
        max_replans=1,
    )
    facts, source_tool_refs = _final_facts(dag_result)
    if not dag_result.success or not facts:
        failed = [record.to_dict() for record in dag_result.node_records if not record.success]
        return GraphWorkerResult(
            task_id=task.task_id,
            agent_id=task.assigned_agent,
            status=ResultStatus.FAILED,
            output_type="PortfolioRiskResult",
            data=None,
            error={
                "code": "risk_tool_dag_failed",
                "message": "W04 私有风险 Tool DAG 未形成结构化风险事实。",
                "component": task.assigned_agent,
                "retryable": any(bool(item.get("retryable")) for item in failed),
                "failure_details": failed[:10],
            },
            focus_refs=task.focus_refs,
            summary="组合风险事实计算失败。",
            warnings=[str(item) for item in facts.get("limitations") or []],
        )

    allowed_source_refs = set(source_tool_refs) | {
        str(item) for item in facts.get("source_refs") or [] if str(item)
    }
    hard_constraint_breaches = _hard_constraint_breaches(facts)

    output_schema = _risk_output_schema()

    def validate(payload: dict[str, Any]) -> None:
        validate_schema(payload, output_schema)
        risk_ids = {
            str(item.get("risk_id") or "")
            for item in payload.get("records") or []
            if isinstance(item, dict)
        }
        for item in payload.get("records") or []:
            unknown = sorted(set(str(ref) for ref in item.get("source_refs") or []) - allowed_source_refs)
            if unknown:
                raise RuntimeError("risk_unknown_source_refs:" + ",".join(unknown))
        for item in payload.get("risk_constraints") or []:
            unknown = sorted(set(str(ref) for ref in item.get("source_risk_ids") or []) - risk_ids)
            if unknown:
                raise RuntimeError("risk_constraint_unknown_risk_ids:" + ",".join(unknown))
        if hard_constraint_breaches:
            constraint_records = [
                item for item in payload.get("records") or []
                if isinstance(item, dict) and str(item.get("category") or "").lower() == "constraint"
            ]
            if not constraint_records:
                raise RuntimeError("hard_constraint_breach_risk_item_required")
            statements = "\n".join(str(item.get("statement") or "") for item in constraint_records)
            for breach in hard_constraint_breaches:
                security_ref = str(breach.get("security_ref") or "")
                if security_ref and security_ref not in statements:
                    raise RuntimeError("hard_constraint_breach_security_missing:" + security_ref)
            constraint_risk_ids = {str(item.get("risk_id") or "") for item in constraint_records}
            if not any(
                constraint_risk_ids.intersection(str(ref) for ref in item.get("source_risk_ids") or [])
                for item in payload.get("risk_constraints") or [] if isinstance(item, dict)
            ):
                raise RuntimeError("hard_constraint_breach_constraint_required")

    risk_system = append_skill_guidance(
        (
            "你是 W04 Risk Analyst。你只基于 atomic_risk_facts 形成组合层风险分析和风险约束。"
            "不得重新查询业务状态，不得补造风险事实，不得生成具体买卖动作或 Proposal。"
            "每条 risk item 必须引用 allowed_source_refs；每条 risk constraint 必须引用已输出的 risk_id。"
            "所有权重表述必须明确分母：invested_weight/legacy weight 的分母是股票持仓市值，asset_weight 的分母是总资产。"
            "max_single_position 等用户仓位上限只能与 asset_weight（总资产口径）比较，禁止拿 invested_weight 判断用户约束。"
            "若 deterministic_hard_constraint_breaches 非空，必须输出 category=constraint 的风险记录，逐一写明其中的 security_ref，"
            "并至少生成一条引用这些 constraint risk_id 的风险约束；不得声称当前仓位符合该硬上限。"
            "upstream_analysis_context来自当前ContextBundle工作记忆；W04只根据其内容判断风险，不关心数据由哪个Worker或Tool产生。"
            "business_parameters只包含本任务已明确存在的用户业务参数，不得自行补默认值。"
            "不得在没有确定性工具事实时伪造精确的情景后权重、相关性或收益指标。"
            "完成度与合同验收由Runtime负责。严格输出 risk_output_schema 对应 JSON。"
        ),
        get_skill_registry(),
        task.user_id,
        worker_id=str(task.worker_id or "W04"),
    )
    messages = [
        {
            "role": "system",
            "content": risk_system,
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "risk_question": str(task.args.get("risk_question") or task.objective or ""),
                    "working_memory_data_names": list((working_memory_context or {}).get("available_names") or []),
                    "atomic_risk_facts": safe_public_value(facts),
                    "upstream_analysis_context": safe_public_value(upstream_analysis_context),
                    "business_parameters": safe_public_value(business_parameter_context),
                    "deterministic_hard_constraint_breaches": safe_public_value(hard_constraint_breaches),
                    "allowed_source_refs": sorted(allowed_source_refs),
                    "reply_language": "en" if language == "en" else "zh",
                    "risk_output_schema": output_schema,
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    payload = generate_json_with_local_structural_repair(
        llm_service,
        stage="graph_risk_analyst",
        operation=task.boundary_id,
        messages=messages,
        output_schema=output_schema,
        validator=validate,
        immutable_repair_context={
            "allowed_source_refs": sorted(allowed_source_refs),
            "deterministic_hard_constraint_breaches": safe_public_value(hard_constraint_breaches),
        },
        repair_guidance=(
            "Repair JSON shape and risk/source references only. If deterministic_hard_constraint_breaches is non-empty, "
            "preserve every security_ref in a category=constraint risk record and create a linked risk constraint. "
            "Never invent risk facts or proposals."
        ),
        primary_max_output_tokens=3200,
        repair_max_output_tokens=1800,
        primary_disable_thinking=False,
    )

    risk_bundle = {
                "risk_analysis": safe_public_value(payload.get("risk_analysis") or {}),
        "records": safe_public_value(payload.get("records") or []),
        "risk_constraints": safe_public_value(payload.get("risk_constraints") or []),
        "source_tool_result_refs": [str(item) for item in payload.get("source_tool_result_refs") or []],
        "hard_constraint_breaches": safe_public_value(hard_constraint_breaches),
        "access_mode": "read",
        "persistent_write_performed": False,
    }
    business_data = materialize_promised_data(task, risk_bundle)
    data = {**risk_bundle, "business_data": business_data, "produced_data_names": list(business_data)}
    return GraphWorkerResult(
        task_id=task.task_id,
        agent_id=task.assigned_agent,
        status=ResultStatus.COMPLETED,
        output_type="PortfolioRiskResult",
        data=data,
        error=None,
        focus_refs=task.focus_refs,
        summary=str((risk_bundle.get("risk_analysis") or {}).get("summary") or "已完成组合风险分析。"),
        findings=[{
            "kind": "portfolio_risk",
            "risk_item_count": len(risk_bundle.get("records") or []),
            "constraint_count": len(risk_bundle.get("risk_constraints") or []),
        }],
        confidence=0.9,
        warnings=[str(item) for item in (risk_bundle.get("risk_analysis") or {}).get("limitations") or []],
        metadata={
            "tool_dag_used": True,
            "tool_task_count": len(dag_result.plan.tasks),
            "tool_dag_replan_count": int(dag_result.replan_count),
            "access_mode": "read",
            "persistent_write_performed": False,
        },
    )


__all__ = ["run_risk"]
