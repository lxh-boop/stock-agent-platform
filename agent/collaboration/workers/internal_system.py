"""Execute W02 capability contracts through its private Tool DAG."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent.graph.contracts import GraphNodeKind
from agent.tool_dag import WorkerToolDagRuntime

from ..completion import runtime_completion_report
from ..models import GraphAgentTask, GraphWorkerResult, ResultStatus
from agent.capabilities.data_names import LEGACY_OUTPUT_NAME_MAP
from .common import contract_acceptance_rules, contract_output_data_names, execution_safe_value, safe_public_value




def _available_context(
    task: GraphAgentTask,
    *,
    default_top_k: int,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "user_id": task.user_id,
        "top_k": int(task.business_parameters.get("top_k") or default_top_k or 10),
        "model_name": str(task.business_parameters.get("model_name") or ""),
        "trade_date": str(task.business_parameters.get("trade_date") or ""),
        "holding_period": int(task.business_parameters.get("holding_period") or 0),
        "as_of_time": str(task.as_of_time or ""),
    }
    securities = [
        ref for ref in [*task.focus_refs, *task.context_refs]
        if ref.node_kind == GraphNodeKind.OBJECT and str(ref.node_id).startswith("cn:security:")
    ]
    if len(securities) == 1:
        context["security_node_id"] = securities[0].node_id
    for index, ref in enumerate(securities, start=1):
        context[f"security_node_id_{index}"] = ref.node_id
    return context


def _publish_business_data(
    task: GraphAgentTask,
    dag_result: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str], list[str]]:
    wanted = set(contract_output_data_names(task))
    business_data: dict[str, Any] = {}
    business_data_contracts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for tool_result in list(getattr(dag_result, "final_results", []) or []):
        data = execution_safe_value(dict(getattr(tool_result, "data", {}) or {}))
        tool_data = data.get("business_data") if isinstance(data.get("business_data"), dict) else data.get("slots") if isinstance(data.get("slots"), dict) else {}
        slot_contracts = data.get("slot_contracts") if isinstance(data.get("slot_contracts"), dict) else {}
        for raw_name, value in tool_data.items():
            name = LEGACY_OUTPUT_NAME_MAP.get(str(raw_name), str(raw_name))
            if name in wanted:
                business_data[name] = execution_safe_value(value)
                if isinstance(slot_contracts.get(raw_name), dict):
                    business_data_contracts[name] = execution_safe_value(
                        slot_contracts[raw_name]
                    )
        warnings.extend(str(item) for item in getattr(tool_result, "warnings", []) or [] if str(item))
        warnings.extend(str(item) for item in getattr(tool_result, "errors", []) or [] if str(item))
    produced = [name for name in contract_output_data_names(task) if name in business_data]
    return (
        business_data,
        business_data_contracts,
        produced,
        list(dict.fromkeys(warnings)),
    )


def run_internal_system(
    tool_dag_runtime: WorkerToolDagRuntime,
    task: GraphAgentTask,
    output_dir: str | Path,
    db_path: str | Path | None,
    default_top_k: int,
    *,
    worker_prompt: str,
    allowed_tool_names: list[str],
    provider: Any = None,
) -> GraphWorkerResult:
    del provider
    required_outputs = contract_output_data_names(task)
    available_context = _available_context(task, default_top_k=default_top_k)
    dag_result = tool_dag_runtime.run(
        worker_task_id=task.task_id,
        worker_role=task.assigned_agent,
        boundary_id=task.boundary_id,
        worker_objective=task.objective,
        worker_prompt=worker_prompt,
        available_context=available_context,
        required_output_keys=required_outputs,
        completion_criteria=contract_acceptance_rules(task),
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
    business_data, business_data_contracts, produced, warnings = _publish_business_data(task, dag_result)
    execution_success = bool(dag_result.success)
    status = ResultStatus.COMPLETED if execution_success else ResultStatus.PARTIAL if produced else ResultStatus.FAILED
    payload = {
        "boundary_id": task.boundary_id,
        "business_data": business_data,
        "business_data_contracts": business_data_contracts,
        "produced_data_names": produced,
        # Diagnostics only. Missing business names do not determine execution success.
        "missing_data_names": [name for name in required_outputs if name not in produced],
        "business_empty": bool(produced and all(value in ({}, [], None, "") for value in business_data.values())),
        "business_validation_mode": "pass_through",
    }
    failed_nodes = [
        record.to_dict()
        for record in list(getattr(dag_result, "node_records", []) or [])
        if str(getattr(record, "status", "") or "") != "succeeded"
    ]
    failure_reason = ""
    if failed_nodes:
        first_failure = dict(failed_nodes[0].get("failure") or {})
        failure_reason = str(
            first_failure.get("reason")
            or first_failure.get("error_message")
            or first_failure.get("error_id")
            or "W02 private Tool execution failed."
        )
    error = None if execution_success else {
        "code": "internal_capability_tool_execution_failed",
        "message": failure_reason or "W02 私有 Tool 执行失败。",
        "component": task.assigned_agent,
        "retryable": True,
        "failed_tool_tasks": failed_nodes[:8],
    }
    result = GraphWorkerResult(
        task_id=task.task_id,
        agent_id=task.assigned_agent,
        status=status,
        output_type="CapabilityResult",
        payload_schema="capability_result.v1",
        payload=payload,
        data=payload,
        error=error,
        focus_refs=task.focus_refs,
        summary=(
            "W02 私有 Tool 已执行完成；业务内容充分性当前不在Worker执行层校验。"
            if execution_success else "W02 私有 Tool 执行失败，错误结果将交给Coordinator Recovery。"
        ),
        findings=[{"kind": "capability_business_data", "data_names": produced}],
        confidence=1.0 if execution_success else 0.5 if produced else 0.0,
        warnings=warnings,
        metadata={
            "boundary_id": task.boundary_id,
            "tool_dag_task_count": len(getattr(getattr(dag_result, "plan", None), "tasks", []) or []),
            "produced_data_names": produced,
            "business_validation_mode": "pass_through",
            "tool_execution_success": execution_success,
        },
    )
    result.completion = runtime_completion_report(
        task,
        result_status=result.status,
        output_type=result.output_type,
        data=result.data,
        error=result.error,
    )
    return result


__all__ = ["run_internal_system"]
