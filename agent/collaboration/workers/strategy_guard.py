"""W05 read-only Proposal Worker with canonical cross-Run persistence.

W05 reads only the Worker-projected ContextBundle view. It may persist Proposal
control state, but it never mutates the user's business-domain state.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.llm import LLMService
from agent.proposals import ProposalStore, ProposalStoreError
from agent.skill_runtime import get_skill_registry
from agent.skill_runtime.injection import append_skill_guidance

from ..models import GraphAgentTask, GraphWorkerResult, MissingContextItem, ResultStatus
from ..worker_contracts import array_schema, object_schema, string_schema, validate_schema
from .common import execution_safe_value, materialize_promised_data, safe_public_value
from .structured_output import generate_json_with_local_structural_repair


def _proposal_output_schema() -> dict[str, Any]:
    return object_schema(
        {
            "action": string_schema(enum=["proposal_ready", "need_context", "blocked"]),
            "proposal": object_schema(
                {
                    "proposal_type": string_schema(enum=[
                        "portfolio_mutation", "business_graph_mutation",
                        "strategy_change", "profile_change", "generic_proposal",
                    ]),
                    "operation_type": string_schema(min_length=1),
                    "target": {"type": "object", "additionalProperties": True},
                    "changes": {"type": "object", "additionalProperties": True},
                    "execution_parameters": {"type": "object", "additionalProperties": True},
                    "rationale": string_schema(min_length=1),
                    "constraint_response": array_schema({"type": "object", "additionalProperties": True}),
                    "graph_patch": {"type": "object", "additionalProperties": True},
                },
                required=["proposal_type", "operation_type", "target", "changes", "execution_parameters", "rationale"],
                additional_properties=True,
            ),
            "limitations": array_schema({"type": "string"}),
            "reason": {"type": "string"},
            "missing_items": array_schema(object_schema(
                {
                    "key": string_schema(min_length=1),
                    "description": string_schema(min_length=1),
                    "expected_format": {"type": "string"},
                },
                required=["key", "description", "expected_format"],
                additional_properties=False,
            )),
            "requires_approval": {"type": "boolean"},
            "execution_allowed": {"type": "boolean"},
        },
        required=[
            "action", "proposal", "limitations", "reason", "missing_items",
            "requires_approval", "execution_allowed",
        ],
        additional_properties=False,
    )


def _collect_hard_constraint_breaches(value: Any) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key in ("hard_constraint_breaches", "single_position_limit_breaches"):
                rows = item.get(key)
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and str(row.get("security_ref") or "").strip():
                            found[str(row["security_ref"])] = dict(row)
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
    visit(value)
    return sorted(found.values(), key=lambda item: float(item.get("current_asset_weight") or 0.0), reverse=True)


def run_strategy_guard(
    llm_service: LLMService,
    task: GraphAgentTask,
    *,
    current_user_request: str,
    working_memory_context: dict[str, Any] | None,
    output_dir: str | Path,
    db_path: str | Path | None,
    default_top_k: int,
    language: str,
    execution_context: dict[str, Any] | None,
) -> GraphWorkerResult:
    del default_top_k
    working_context = execution_safe_value(dict(working_memory_context or {}))
    hard_constraint_breaches = _collect_hard_constraint_breaches(working_context)

    def validate(payload: dict[str, Any]) -> None:
        validate_schema(payload, _proposal_output_schema())
        action = str(payload.get("action") or "")
        if action == "proposal_ready":
            if not bool(payload.get("requires_approval")):
                raise RuntimeError("proposal_requires_approval_must_be_true")
            if bool(payload.get("execution_allowed")):
                raise RuntimeError("proposal_execution_allowed_must_be_false")
            if not isinstance(payload.get("proposal"), dict) or not payload.get("proposal"):
                raise RuntimeError("proposal_payload_required")
            proposal = dict(payload.get("proposal") or {})
            proposal_type = str(proposal.get("proposal_type") or "")
            if proposal_type == "portfolio_mutation" and not isinstance(proposal.get("execution_parameters"), dict):
                raise RuntimeError("portfolio_execution_parameters_required")
            if proposal_type == "business_graph_mutation":
                patch = proposal.get("graph_patch")
                if not isinstance(patch, dict) or not patch:
                    raise RuntimeError("business_graph_patch_required")
            if hard_constraint_breaches:
                responses = payload["proposal"].get("constraint_response")
                if not isinstance(responses, list):
                    raise RuntimeError("proposal_constraint_response_required")
                by_security = {
                    str(item.get("security_ref") or ""): item
                    for item in responses if isinstance(item, dict) and str(item.get("security_ref") or "")
                }
                for breach in hard_constraint_breaches:
                    security_ref = str(breach.get("security_ref") or "")
                    response = by_security.get(security_ref)
                    if not response:
                        raise RuntimeError("proposal_constraint_response_missing:" + security_ref)
                    if str(response.get("action") or "") != "reduce_to_limit":
                        raise RuntimeError("proposal_constraint_action_invalid:" + security_ref)
                    target = float(response.get("target_asset_weight"))
                    limit = float(breach.get("max_allowed_asset_weight"))
                    if target > limit + 1e-12 or target < 0:
                        raise RuntimeError("proposal_constraint_target_exceeds_limit:" + security_ref)

    guard_system = append_skill_guidance(
        (
            "你是 W05 Strategy Guard，是只读的建议分析Worker。你只根据当前ContextBundle工作记忆和用户明确目标生成待审批Proposal，"
            "不关心数据由哪个Worker、Tool或Request产生。你可以自行判断现有数据是否足够；不足时返回need_context并只说明缺什么业务信息。"
            "不得调用业务写工具、不得修改账户/持仓/策略/画像/配置，也不得声称已经执行。"
            "你可以把Proposal保存到Runtime的canonical ProposalStore；这只是待审批控制状态，不是业务Mutation。"
            "proposal_ready时requires_approval必须为true，execution_allowed必须为false。"
            "proposal必须给出proposal_type、operation_type、target、changes、execution_parameters、rationale。"
            "如果用户目标是调整模拟持仓，proposal_type=portfolio_mutation，并把可确定的stock_code、requested_weight、"
            "position_adjustment_ratio、requested_quantity等放入execution_parameters；不要为了凑字段猜值。"
            "如果用户明确要求修改正式Business Graph，proposal_type=business_graph_mutation并给出完整graph_patch。"
            "若hard_constraint_breaches非空，proposal必须逐一给出constraint_response，action=reduce_to_limit，且目标权重不得超过上限。"
            "严格输出proposal_output_schema对应JSON。"
        ),
        get_skill_registry(),
        task.user_id,
        worker_id=str(task.worker_id or "W05"),
    )
    messages = [
        {
            "role": "system",
            "content": guard_system,
        },
        {
            "role": "user",
            "content": json.dumps({
                "user_request": str(current_user_request or ""),
                "task_objective": task.objective,
                "business_parameters": safe_public_value(task.args),
                "working_memory": working_context,
                "hard_constraint_breaches": safe_public_value(hard_constraint_breaches),
                "reply_language": "en" if language == "en" else "zh",
                "proposal_output_schema": _proposal_output_schema(),
            }, ensure_ascii=False, default=str),
        },
    ]
    payload = generate_json_with_local_structural_repair(
        llm_service,
        stage="graph_strategy_guard",
        operation=task.boundary_id,
        messages=messages,
        output_schema=_proposal_output_schema(),
        validator=validate,
        immutable_repair_context={
            "hard_constraint_breaches": safe_public_value(hard_constraint_breaches),
            "requires_approval": True,
            "execution_allowed": False,
        },
        repair_guidance=(
            "Repair JSON shape, approval flags, and constraint_response only. Do not add facts outside the supplied working memory. "
            "Every breached security_ref must have action=reduce_to_limit and target_asset_weight <= max_allowed_asset_weight."
        ),
        primary_max_output_tokens=3200,
        repair_max_output_tokens=1800,
        primary_disable_thinking=False,
    )

    action = str(payload.get("action") or "")
    if action == "need_context":
        missing = [
            MissingContextItem(
                key=str(item.get("key") or "proposal_context"),
                description=str(item.get("description") or "生成方案所需业务信息"),
                expected_format=str(item.get("expected_format") or "结构化业务数据"),
                reason=str(payload.get("reason") or "当前工作记忆不足。"),
                searched_sources=["ContextBundle"],
            )
            for item in payload.get("missing_items") or [] if isinstance(item, dict)
        ]
        return GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.NEED_CONTEXT,
            output_type="ReviewedProposal", data=None, error=None, focus_refs=task.focus_refs,
            summary=str(payload.get("reason") or "生成方案前需要补充业务信息。"),
            missing_items=missing, warnings=[str(item) for item in payload.get("limitations") or []],
        )
    if action == "blocked":
        return GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.BLOCKED,
            output_type="ReviewedProposal", data=None,
            error={"code": "proposal_blocked", "message": str(payload.get("reason") or "当前信息不能形成安全方案。"), "component": "strategy_guard", "retryable": False},
            focus_refs=task.focus_refs, summary=str(payload.get("reason") or "当前信息不能形成安全方案。"),
            warnings=[str(item) for item in payload.get("limitations") or []],
        )

    proposal_payload = safe_public_value(payload.get("proposal") or {})
    source_request_id = str((task.metadata or {}).get("request_id") or "").strip()
    if not source_request_id:
        source_request_id = str(task.task_id or "REQUEST").split("-", 1)[0] or "REQUEST"
    store = ProposalStore(output_dir=output_dir, db_path=db_path)

    # Revision only reuses an existing canonical proposal when the Runtime or
    # explicit request context identifies it.  W05 never invents IDs.
    runtime_context = dict(execution_context or {})
    existing_id = str(
        runtime_context.get("proposal_id")
        or task.business_parameters.get("proposal_id")
        or ""
    ).strip()
    try:
        if existing_id.startswith("proposal_"):
            artifact = store.revise(
                proposal_id=existing_id,
                user_id=task.user_id,
                payload=proposal_payload,
                revision_reason=str(current_user_request or "")[:500],
                created_by="W05",
            )
            revision = True
        else:
            artifact = store.create(
                proposal_type=str(proposal_payload.get("proposal_type") or "generic_proposal"),
                user_id=task.user_id,
                session_id=task.session_id,
                source_run_id=task.run_id,
                source_request_id=source_request_id,
                payload=proposal_payload,
                metadata={"worker_id": "W05", "task_id": task.task_id},
                created_by="W05",
            )
            revision = False
    except ProposalStoreError as exc:
        return GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.BLOCKED,
            output_type="ReviewedProposal", data=None,
            error={"code": str(exc), "message": str(exc), "component": "proposal_store", "retryable": False},
            focus_refs=task.focus_refs, summary=f"Proposal持久化失败：{exc}",
        )

    proposal_data = {
        "proposal_id": artifact.proposal_id,
        "proposal_version": artifact.current_version,
        "proposal_status": artifact.status.value,
        "payload_hash": artifact.current_payload_hash,
        "proposal_type": artifact.proposal_type,
        "proposal": proposal_payload,
        "limitations": [str(item) for item in payload.get("limitations") or []],
        "requires_approval": True,
        "execution_allowed": False,
        "business_effect_applied": False,
        "proposal_persisted": True,
    }
    business_data = materialize_promised_data(task, proposal_data)
    return GraphWorkerResult(
        task_id=task.task_id,
        agent_id=task.assigned_agent,
        status=ResultStatus.PROPOSAL_READY,
        output_type="ReviewedProposal",
        data={**proposal_data, "business_data": business_data, "produced_data_names": list(business_data)},
        error=None,
        focus_refs=task.focus_refs,
        summary=str(payload.get("reason") or "已生成并保存待审批方案，尚未修改业务状态。"),
        findings=[{
            "kind": "canonical_proposal_revision" if revision else "canonical_proposal_created",
            "proposal_id": artifact.proposal_id,
            "proposal_version": artifact.current_version,
        }],
        confidence=0.9,
        warnings=[str(item) for item in payload.get("limitations") or []],
        metadata={
            "proposal_id": artifact.proposal_id,
            "proposal_version": artifact.current_version,
            "proposal_status": artifact.status.value,
            "payload_hash": artifact.current_payload_hash,
            "requires_approval": True,
            "execution_allowed": False,
            "can_mutate": False,
            "business_effect_applied": False,
            "proposal_persisted": True,
        },
    )


__all__ = ["run_strategy_guard"]
