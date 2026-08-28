"""Capability-contract completion reports and deterministic flow decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import GraphAgentTask, ResultStatus
from .worker_contracts import WorkerContractViolation

COMPLETION_CONTRACT_VERSION = "capability-contract-list.v2"
COMPLETION_REPORT_VERSION = "capability-contract-report.v2"


@dataclass(frozen=True)
class CompletionFlowDecision:
    result_status: ResultStatus
    semantic_satisfied: bool
    should_freeze: bool
    reusable: bool
    replan_recommended: bool
    failure_kind: str
    freeze_reason: str


def _contracts(task_or_contract: GraphAgentTask | dict[str, Any]) -> list[dict[str, Any]]:
    rows = (task_or_contract.get("contracts") or []) if isinstance(task_or_contract, dict) else task_or_contract.contracts
    return [dict(item) for item in rows if isinstance(item, dict)]


def _output_data_names(task_or_contract: GraphAgentTask | dict[str, Any]) -> list[str]:
    result: list[str] = []
    for contract in _contracts(task_or_contract):
        for row in contract.get("promised_data") or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("data_name") or "").strip()
            if name and name not in result:
                result.append(name)
    return result


def compile_completion_contract(task: GraphAgentTask) -> dict[str, Any]:
    return {
        "schema_version": COMPLETION_CONTRACT_VERSION,
        "contracts": [dict(item) for item in task.contracts],
        "required_business_data": _output_data_names(task),
        "output_type": "CapabilityResult",
    }


def _rule_rows(task_or_contract: GraphAgentTask | dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for contract in _contracts(task_or_contract):
        contract_id = str(contract.get("contract_id") or "")
        for rule_id in contract.get("acceptance_rule_ids") or []:
            rows.append({
                "contract_id": contract_id,
                "rule_id": str(rule_id),
                "satisfied": True,
                "reason": "",
                "evidence_paths": [],
            })
    return rows


def validate_completion_report(report: dict[str, Any], contract: dict[str, Any] | GraphAgentTask, *, path: str = "$.completion") -> None:
    if not isinstance(report, dict):
        raise WorkerContractViolation("completion_report_not_object", path, type(report).__name__)
    if str(report.get("schema_version") or "") != COMPLETION_REPORT_VERSION:
        raise WorkerContractViolation("completion_report_version_mismatch", f"{path}.schema_version", str(report.get("schema_version") or ""))
    expected = set(_output_data_names(contract))
    produced = {str(item) for item in report.get("produced_data_names") or [] if str(item)}
    missing = {str(item) for item in report.get("missing_data_names") or [] if str(item)}
    if produced & missing:
        raise WorkerContractViolation("completion_report_data_overlap", path, ",".join(sorted(produced & missing)))
    unknown = (produced | missing) - expected
    if expected and unknown:
        raise WorkerContractViolation("completion_report_unknown_data_name", path, ",".join(sorted(unknown)))
    if expected and produced | missing != expected:
        absent = expected - produced - missing
        raise WorkerContractViolation("completion_report_data_partition_incomplete", path, ",".join(sorted(absent)))
    # Business-content validation is intentionally disabled. During pass-through
    # mode a Worker may have executed successfully while some expected business
    # names are absent; keep that as diagnostics instead of invalidating the
    # completion report.
    if (
        str(report.get("business_validation_mode") or "") != "pass_through"
        and bool(report.get("expected_task_completed"))
        and missing
    ):
        raise WorkerContractViolation("completed_report_cannot_have_missing_data", path, ",".join(sorted(missing)))


def build_completion_report(
    task: GraphAgentTask,
    *,
    execution_status: str,
    contract_status: str,
    business_status: str,
    completion_status: str,
    expected_task_completed: bool,
    produced_data_names: list[str],
    criterion_results: list[dict[str, Any]] | None = None,
    limitations: list[str] | None = None,
    failure_kind: str = "none",
    report_source: str = "runtime",
) -> dict[str, Any]:
    expected = _output_data_names(task)
    produced = list(dict.fromkeys(str(item) for item in produced_data_names if str(item)))
    missing = [name for name in expected if name not in set(produced)]
    report = {
        "schema_version": COMPLETION_REPORT_VERSION,
        "report_source": str(report_source or "runtime"),
        "business_validation_mode": "pass_through",
        "execution_status": str(execution_status),
        "contract_status": str(contract_status),
        "business_status": str(business_status),
        "completion_status": str(completion_status),
        "expected_task_completed": bool(expected_task_completed),
        "output_type": "CapabilityResult",
        "produced_data_names": produced,
        "missing_data_names": missing,
        "criteria": [dict(item) for item in (criterion_results or _rule_rows(task))],
        "limitations": [str(item) for item in limitations or [] if str(item).strip()],
        "failure_kind": str(failure_kind or "none"),
        "contract_reports": [],
    }
    validate_completion_report(report, task)
    return report


def non_success_completion_report(task: GraphAgentTask, *, execution_status: str, reason: str, failure_kind: str) -> dict[str, Any]:
    expected = _output_data_names(task)
    return {
        "schema_version": COMPLETION_REPORT_VERSION,
        "report_source": "runtime",
        "business_validation_mode": "pass_through",
        "execution_status": str(execution_status),
        "contract_status": "not_satisfied",
        "business_status": "unknown",
        "completion_status": "not_completed",
        "expected_task_completed": False,
        "output_type": "CapabilityResult",
        "produced_data_names": [],
        "missing_data_names": expected,
        "criteria": [{**row, "satisfied": False, "reason": str(reason)[:1000]} for row in _rule_rows(task)],
        "limitations": [str(reason)[:1000]],
        "failure_kind": str(failure_kind),
        "contract_reports": [],
    }


def runtime_completion_report(
    task: GraphAgentTask,
    *,
    result_status: ResultStatus,
    output_type: str,
    data: dict[str, Any] | None,
    error: dict[str, Any] | None,
) -> dict[str, Any]:
    del output_type
    success = result_status in {ResultStatus.COMPLETED, ResultStatus.PROPOSAL_READY}
    materialized = data.get("business_data") if isinstance(data, dict) and isinstance(data.get("business_data"), dict) else {}
    # Key existence means the operation completed, even when the value is empty.
    produced = [str(name) for name in materialized if str(name)]
    business_empty = bool(
        isinstance(data, dict) and (
            data.get("business_empty") is True
            or (produced and all(materialized.get(name) in (None, {}, [], "") for name in produced))
        )
    )
    return build_completion_report(
        task,
        execution_status=result_status.value,
        contract_status="satisfied" if success else "not_satisfied",
        business_status="empty" if business_empty else "available" if success else "unknown",
        completion_status="completed" if success else "not_completed",
        expected_task_completed=success,
        produced_data_names=produced,
        limitations=[] if success else [str((error or {}).get("message") or "Worker did not complete.")],
        failure_kind="none" if success else str((error or {}).get("code") or "worker_execution_failure"),
    )


def evaluate_need_completion(request_need_contract: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate Need completion from validated business-data outputs."""
    produced = {
        str(name)
        for observation in observations or []
        if bool(observation.get("semantic_satisfied"))
        for name in observation.get("produced_data_names") or []
        if str(name)
    }
    insufficiency_seen = any(
        str(item.get("failure_kind") or "") in {"business_insufficient", "business_empty"}
        or str((item.get("completion") or {}).get("business_status") or "") in {"insufficient", "empty"}
        for item in observations or []
    )
    rows: list[dict[str, Any]] = []
    required_statuses: list[str] = []
    for need in request_need_contract.get("needs") or []:
        required_outputs = list(dict.fromkeys(
            str(req.get("data_name") or "")
            for req in need.get("requirements") or []
            if req.get("direction") == "output" and str(req.get("necessity") or "required") == "required" and str(req.get("data_name") or "")
        ))
        if not required_outputs:
            status, missing = "untracked", []
        else:
            missing = [name for name in required_outputs if name not in produced]
            status = "completed" if not missing else "business_insufficient" if insufficiency_seen else "not_completed"
        if bool(need.get("required", True)) and status != "untracked":
            required_statuses.append(status)
        rows.append({
            "need_id": str(need.get("need_id") or ""),
            "kind": str(need.get("kind") or "business"),
            "description": str(need.get("description") or ""),
            "required": bool(need.get("required", True)),
            "status": status,
            "required_output_data_names": required_outputs,
            "missing_output_data_names": missing,
        })
    goal_status = (
        "untracked" if not required_statuses else
        "completed" if all(status == "completed" for status in required_statuses) else
        "partially_completed" if any(status == "completed" for status in required_statuses) else
        "not_completed"
    )
    return {"schema_version": "need-completion-report.v2", "goal_status": goal_status, "needs": rows}


def flow_decision(
    result_status: ResultStatus,
    completion: dict[str, Any],
    *,
    output_type: str = "",
    retryable: bool = False,
) -> CompletionFlowDecision:
    del output_type
    completed = bool(completion.get("expected_task_completed"))
    failure_kind = str(completion.get("failure_kind") or "none")
    if completed:
        status = ResultStatus.PROPOSAL_READY if result_status == ResultStatus.PROPOSAL_READY else ResultStatus.COMPLETED
        return CompletionFlowDecision(status, True, True, True, False, "none", "required_contracts_satisfied")
    if result_status == ResultStatus.NEED_CONTEXT:
        resolved_failure = failure_kind or "context_missing"
        replan_recommended = resolved_failure != "user_input_required"
        return CompletionFlowDecision(
            ResultStatus.NEED_CONTEXT, False, False, False, replan_recommended,
            resolved_failure, "user_input_required" if not replan_recommended else "context_required",
        )
    if result_status == ResultStatus.BLOCKED:
        return CompletionFlowDecision(ResultStatus.BLOCKED, False, False, False, bool(retryable), failure_kind or "blocked", "blocked")
    if result_status == ResultStatus.PARTIAL:
        return CompletionFlowDecision(ResultStatus.PARTIAL, False, False, False, True, failure_kind or "business_insufficient", "partial")
    return CompletionFlowDecision(ResultStatus.FAILED, False, False, False, bool(retryable), failure_kind or "worker_execution_failure", "failed")


def canonicalize_completion_report(
    task: GraphAgentTask,
    *,
    result_status: ResultStatus,
    completion: dict[str, Any],
    contract_reports: list[Any],
    produced_data_names: list[str],
    result_data: dict[str, Any] | None,
) -> tuple[ResultStatus, dict[str, Any], bool]:
    report = dict(completion or {})
    report["business_validation_mode"] = "pass_through"
    required_pairs = [
        (contract, contract_report)
        for contract, contract_report in zip(task.contracts, contract_reports)
        if str(contract.get("criticality") or "required") == "required"
    ]
    satisfied = all(
        str(getattr(contract_report, "status", "")) in {"completed", "business_empty"}
        for _, contract_report in required_pairs
    )
    business_empty = bool(
        isinstance(result_data, dict)
        and (
            result_data.get("business_empty") is True
            or (
                isinstance(result_data.get("business_data"), dict)
                and bool(result_data.get("business_data"))
                and all(value in (None, {}, [], "") for value in result_data["business_data"].values())
            )
        )
    )
    report["contract_reports"] = [item.to_dict() for item in contract_reports]
    report["produced_data_names"] = list(dict.fromkeys(produced_data_names))
    report["missing_data_names"] = [name for name in task.expected_data_names if name not in set(produced_data_names)]
    report["expected_task_completed"] = bool(satisfied)
    report["completion_status"] = "completed" if satisfied else "not_completed"

    if satisfied:
        status = ResultStatus.PROPOSAL_READY if result_status == ResultStatus.PROPOSAL_READY else ResultStatus.COMPLETED
        report.update({
            "execution_status": "succeeded",
            "contract_status": "validation_skipped",
            "business_status": "not_evaluated",
            "failure_kind": "none",
            "limitations": [],
        })
        return status, report, True

    report["contract_status"] = "not_satisfied"
    report["execution_status"] = (
        "need_context" if result_status == ResultStatus.NEED_CONTEXT else
        "blocked" if result_status == ResultStatus.BLOCKED else
        "partial" if result_status == ResultStatus.PARTIAL else "failed"
    )
    if not str(report.get("failure_kind") or "").strip() or report.get("failure_kind") == "none":
        report["failure_kind"] = "worker_execution_failure"
    return result_status, report, False


__all__ = [
    "COMPLETION_CONTRACT_VERSION", "COMPLETION_REPORT_VERSION", "CompletionFlowDecision",
    "build_completion_report", "canonicalize_completion_report", "compile_completion_contract",
    "evaluate_need_completion", "flow_decision", "non_success_completion_report",
    "runtime_completion_report", "validate_completion_report",
]
