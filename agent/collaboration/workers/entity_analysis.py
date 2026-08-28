"""W09 financial-entity analysis from other Workers' execution results only."""
from __future__ import annotations

from typing import Any

from core.llm import LLMService
from core.llm.contracts import LLMJSONError
from core.llm.prompt_compaction import compact_json_dumps, schema_for_prompt

from ..completion import build_completion_report, non_success_completion_report
from ..models import GraphAgentTask, GraphWorkerResult, MissingContextItem, ResultStatus
from ..worker_contracts import array_schema, object_schema, string_schema, validate_schema
from .common import execution_safe_value, materialize_promised_data, safe_public_value
from .structured_output import generate_json_with_local_structural_repair

_MAX_PRIMARY_OUTPUT_TOKENS = 2600
_MAX_REPAIR_OUTPUT_TOKENS = 2200
_MAX_REPAIR_INPUT_CHARS = 12000
_ANALYSIS_STATUSES = {"complete", "partial", "insufficient"}


def _claim_schema() -> dict[str, Any]:
    return object_schema(
        {
            "claim_id": string_schema(min_length=1, max_length=80),
            "statement": string_schema(min_length=1, max_length=320),
        },
        required=["claim_id", "statement"],
        additional_properties=False,
    )


def _missing_requirement_schema() -> dict[str, Any]:
    return object_schema(
        {
            "semantic_key": string_schema(min_length=1, max_length=100),
            "reason": string_schema(min_length=1, max_length=240),
        },
        required=["semantic_key", "reason"],
        additional_properties=False,
    )


def _analysis_schema() -> dict[str, Any]:
    claim = _claim_schema()
    return object_schema(
        {
            "analysis_status": {"type": "string", "enum": sorted(_ANALYSIS_STATUSES)},
            "missing_data_requirements": array_schema(_missing_requirement_schema(), max_items=8),
            "facts": array_schema(claim, max_items=10),
            "analysis": array_schema(claim, max_items=8),
            "uncertainties": array_schema(claim, max_items=8),
            "conclusion": string_schema(max_length=600),
        },
        required=[
            "analysis_status",
            "missing_data_requirements",
            "facts",
            "analysis",
            "uncertainties",
            "conclusion",
        ],
        additional_properties=False,
    )


def _allowed_missing_semantics(task: GraphAgentTask) -> set[str]:
    allowed: set[str] = set()
    for row in list((task.metadata or {}).get("input_requirements") or []):
        if isinstance(row, dict) and str(row.get("semantic_key") or "").strip():
            allowed.add(str(row.get("semantic_key")).strip())
    contract = dict((task.metadata or {}).get("request_need_contract") or {})
    for need in contract.get("needs") or []:
        if not isinstance(need, dict):
            continue
        for req in need.get("requirements") or []:
            if not isinstance(req, dict) or req.get("direction") != "input" or req.get("kind") != "data":
                continue
            key = str(req.get("semantic_key") or "").strip()
            if key:
                allowed.add(key)
    return allowed


def _validate_analysis(payload: dict[str, Any], *, allowed_missing_semantics: set[str] | None = None) -> None:
    validate_schema(payload, _analysis_schema())
    status = str(payload.get("analysis_status") or "").strip()
    if status not in _ANALYSIS_STATUSES:
        raise RuntimeError(f"entity_analysis_status_invalid:{status}")
    seen_missing: set[str] = set()
    for index, item in enumerate(payload.get("missing_data_requirements") or []):
        semantic_key = str(item.get("semantic_key") or "").strip()
        if semantic_key in seen_missing:
            raise RuntimeError(f"entity_analysis_duplicate_missing_semantic:{semantic_key}")
        seen_missing.add(semantic_key)
        if allowed_missing_semantics and semantic_key not in allowed_missing_semantics:
            raise RuntimeError(f"entity_analysis_missing_semantic_outside_request:{semantic_key}")
        if not str(item.get("reason") or "").strip():
            raise RuntimeError(f"entity_analysis_missing_reason_required:{index}")
    if status == "insufficient" and not seen_missing:
        raise RuntimeError("entity_analysis_insufficient_requires_missing_data_requirements")
    for field in ("facts", "analysis", "uncertainties"):
        seen: set[str] = set()
        for index, item in enumerate(payload.get(field) or []):
            claim_id = str(item.get("claim_id") or "").strip()
            if not claim_id:
                raise RuntimeError(f"entity_analysis_{field}_claim_id_required:{index}")
            if claim_id in seen:
                raise RuntimeError(f"entity_analysis_duplicate_claim_id:{claim_id}")
            seen.add(claim_id)
            if not str(item.get("statement") or "").strip():
                raise RuntimeError(f"entity_analysis_{field}_statement_required:{index}")


def run_entity_analysis(
    llm_service: LLMService,
    task: GraphAgentTask,
    *,
    worker_results_context: dict[str, Any] | None = None,
    language: str = "zh",
) -> GraphWorkerResult:
    """Analyze the authoritative target using only already-produced Worker results.

    Business sufficiency is owned by W09/LLM. Runtime does not pre-judge whether
    other Workers' content is enough. W09 returns complete / partial /
    insufficient; only insufficient requests Coordinator Recovery.
    """
    context = execution_safe_value(dict(worker_results_context or {}))
    authoritative_refs = [ref.to_dict() for ref in task.focus_refs]
    result_items = dict(context.get("items") or {}) if isinstance(context.get("items"), dict) else {}
    available_names: list[str] = []
    for item in result_items.values():
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        business_data = data.get("business_data") if isinstance(data.get("business_data"), dict) else {}
        for name in list(data.get("produced_data_names") or []) + list(business_data.keys()):
            name = str(name or "").strip()
            if name and name not in available_names:
                available_names.append(name)
    allowed_missing_semantics = _allowed_missing_semantics(task)
    output_schema = _analysis_schema()

    system = (
        "你是W09金融实体分析Worker。你的业务事实来源只能是other_worker_results，也就是本轮其他Worker已经执行得到的GraphWorkerResult。"
        "你可以读取其中的status、summary、completion以及data/business_data等业务结果，但不得调用任何Worker、Tool、数据库、RAG、Neo4j业务查询或模型业务接口，"
        "也不得用模型记忆补充other_worker_results中不存在的事实。authoritative_entity_refs只用于确定当前分析对象身份，不是额外业务数据来源。"
        "你必须自己判断这些Worker结果是否足以完成analysis_goal；Runtime不会替你判断业务充分性。"
        "analysis_status只能是complete、partial、insufficient。complete表示现有结果足够形成完整可靠分析；"
        "partial表示存在缺失维度但仍足以形成有边界的可靠有限分析，此时必须继续产出analysis并把缺失维度写入uncertainties，不触发Recovery；"
        "insufficient仅用于连可靠的有限结论都无法形成的情况，此时missing_data_requirements必须列出缺少的业务语义semantic_key和原因，semantic_key只能来自allowed_missing_semantics。"
        "你只说明缺什么，不指定Worker或Tool。严格区分facts、analysis、uncertainties。只输出JSON。"
        if language != "en" else
        "You are W09, a financial entity-analysis Worker. Your only business-fact source is other_worker_results: GraphWorkerResult objects already produced by other Workers in this run. "
        "Do not call Workers, Tools, databases, RAG, graph business queries, business model APIs, or add model-memory facts. authoritative_entity_refs identify the target only. "
        "Judge sufficiency yourself with analysis_status=complete|partial|insufficient. partial must still produce a bounded analysis and disclose gaps under uncertainties; it does not request Recovery. "
        "Use insufficient only when no reliable bounded conclusion is possible, and then return missing_data_requirements using only semantic keys from allowed_missing_semantics. Do not name a Worker or Tool. Return JSON only."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": compact_json_dumps({
            "analysis_goal": str(task.args.get("analysis_goal") or task.objective),
            "comparison_mode": len(task.focus_refs) > 1,
            "authoritative_entity_refs": authoritative_refs,
            "other_worker_results": context,
            "allowed_missing_semantics": sorted(allowed_missing_semantics),
            "entity_analysis_output_schema": schema_for_prompt(output_schema),
            "reply_language": language,
        })},
    ]
    try:
        analysis = generate_json_with_local_structural_repair(
            llm_service,
            stage="graph_entity_analysis",
            operation=task.boundary_id,
            messages=messages,
            output_schema=output_schema,
            validator=lambda payload: _validate_analysis(
                payload, allowed_missing_semantics=allowed_missing_semantics
            ),
            immutable_repair_context={
                "authoritative_entity_refs": authoritative_refs,
                "available_names": available_names,
                "allowed_missing_semantics": sorted(allowed_missing_semantics),
            },
            repair_guidance="只修复JSON结构和允许的semantic_key；不得新增other_worker_results中不存在的事实。",
            primary_max_output_tokens=_MAX_PRIMARY_OUTPUT_TOKENS,
            repair_max_output_tokens=_MAX_REPAIR_OUTPUT_TOKENS,
            max_invalid_output_chars=_MAX_REPAIR_INPUT_CHARS,
            primary_disable_thinking=True,
        )
    except LLMJSONError as exc:
        completion = build_completion_report(
            task,
            execution_status="failed",
            contract_status="not_satisfied",
            business_status="unknown",
            completion_status="not_completed",
            expected_task_completed=False,
            produced_data_names=[],
            limitations=[str(exc)[:1000]],
            failure_kind="worker_structured_output_failure",
        )
        return GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.FAILED,
            output_type="EntityAnalysisResult", data=None,
            error={"code": "worker_structured_output_failed", "message": str(exc), "component": task.assigned_agent, "retryable": True},
            focus_refs=task.focus_refs,
            summary="W09结构化输出修复失败。" if language != "en" else "W09 structured-output repair failed.",
            warnings=[f"LLMJSONError:{exc}"], completion=completion,
            metadata={"database_write": False, "worker_results_only": True, "replan_recommended": True},
        )

    analysis_status = str(analysis.get("analysis_status") or "").strip()
    missing_requirements = [
        {
            "semantic_key": str(item.get("semantic_key") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
        }
        for item in analysis.get("missing_data_requirements") or []
        if isinstance(item, dict) and str(item.get("semantic_key") or "").strip()
    ]

    if analysis_status == "insufficient":
        reason = str(analysis.get("conclusion") or "现有其他Worker执行结果不足以支持可靠分析。")
        completion = non_success_completion_report(
            task, execution_status="need_context", reason=reason, failure_kind="entity_context_insufficient"
        )
        return GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.NEED_CONTEXT,
            output_type="EntityAnalysisResult", data=None,
            error={
                "error_id": "entity_context_insufficient",
                "operation": task.objective or task.boundary_id,
                "reason": reason,
                "retryable": True,
                "missing_data_requirements": missing_requirements,
            },
            focus_refs=task.focus_refs,
            summary="现有其他Worker执行结果不足以支持可靠分析，已返回结构化缺失业务语义。",
            missing_items=[
                MissingContextItem(
                    key=item["semantic_key"], description=item["reason"],
                    expected_format="business data from another Worker result",
                    reason="W09 judged other Worker results insufficient",
                    searched_sources=["other_worker_results"], blocking=True,
                )
                for item in missing_requirements
            ],
            completion=completion,
            metadata={
                "worker_results_only": True,
                "worker_result_available_names": available_names,
                "analysis_status": "insufficient",
                "missing_data_requirements": missing_requirements,
                "replan_recommended": True,
                "failure_kind": "entity_context_insufficient",
                "database_write": False,
            },
        )

    analysis_record = {
        "entity_refs": authoritative_refs,
        "analysis_status": analysis_status,
        "facts": safe_public_value(analysis.get("facts") or []),
        "analysis": safe_public_value(analysis.get("analysis") or []),
        "uncertainties": safe_public_value(analysis.get("uncertainties") or []),
        "conclusion": str(analysis.get("conclusion") or ""),
        "context_assessment": {
            "status": analysis_status,
            "available_names": available_names,
        },
    }
    uncertainty_record = {
        "entity_refs": authoritative_refs,
        "analysis_status": analysis_status,
        "uncertainties": analysis_record["uncertainties"],
        "available_names": available_names,
    }
    business_data = materialize_promised_data(
        task,
        analysis_record,
        per_name={"analysis": analysis_record, "analysis_uncertainty": uncertainty_record},
    )
    completion = build_completion_report(
        task,
        execution_status="succeeded",
        contract_status="validation_skipped",
        business_status="not_evaluated",
        completion_status="completed",
        expected_task_completed=True,
        produced_data_names=list(business_data),
        limitations=["bounded_partial_analysis"] if analysis_status == "partial" else [],
        failure_kind="none",
    )
    payload = {**analysis_record, "business_data": business_data, "produced_data_names": list(business_data)}
    return GraphWorkerResult(
        task_id=task.task_id,
        agent_id=task.assigned_agent,
        status=ResultStatus.COMPLETED,
        output_type="EntityAnalysisResult",
        payload_schema="entity_analysis_result.v3",
        payload=payload,
        data=payload,
        error=None,
        focus_refs=task.focus_refs,
        summary=(
            "已仅基于本轮其他Worker执行结果完成有边界的部分分析。"
            if analysis_status == "partial"
            else "已仅基于本轮其他Worker执行结果完成金融实体分析。"
        ),
        findings=[{
            "kind": "entity_analysis_output_diagnostics",
            "analysis_status": analysis_status,
            "fact_count": len(payload["facts"]),
            "analysis_count": len(payload["analysis"]),
            "uncertainty_count": len(payload["uncertainties"]),
            "worker_result_available_names": available_names,
        }],
        confidence=0.72 if analysis_status == "partial" else 0.85,
        completion=completion,
        metadata={
            "database_write": False,
            "worker_results_only": True,
            "working_memory_mode": False,
            "worker_result_available_names": available_names,
            "analysis_status": analysis_status,
            "replan_recommended": False,
        },
    )


__all__ = ["run_entity_analysis"]
