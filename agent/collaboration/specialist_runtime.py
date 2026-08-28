"""Worker execution facade. Providers may use ContextBundle; W09 consumes sibling Worker results."""
from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from core.llm import LLMService
from agent.capabilities import BusinessParameterResolver, CapabilityContract, CapabilityContractValidator
from agent.communication import MessageType, publish_agent_message
from agent.console_trace import get_llm_execution_timing, get_tool_execution_timing
from agent.context.context_types import ContextBundle
from agent.graph.impact_service import GraphImpactService
from agent.graph.provider_adapter import GraphProviderAdapter
from agent.llm_audit import activate_llm_audit_context
from agent.runtime_state import RuntimeResourceBudget
from agent.tool_dag import ToolDagExecutor, ToolDagValidator, WorkerToolDagPlanner, WorkerToolDagRuntime
from agent.tool_runtime import ToolExecutor
from agent.tool_runtime import ToolOutputContract
from agent.worker_tools import WorkerToolDirectory, build_worker_tool_registry

from .completion import canonicalize_completion_report, flow_decision, non_success_completion_report, runtime_completion_report
from .error_contracts import escalation_from_worker_result
from .models import GraphAgentTask, GraphWorkerResult, MissingContextItem, ResultStatus, TaskStatus
from .worker_directory import (CapabilityWorkerDirectory, DATABASE_WRITER, ENTITY_ANALYST, EVIDENCE_COLLECTOR,
    GRAPH_RELATION_RETRIEVER, PORTFOLIO_ANALYST, REPORT_WRITER, RISK_ANALYST, STRATEGY_GUARD, SYSTEM_DIAGNOSTIC)
from .workers import (run_diagnostic, run_evidence, run_entity_analysis, run_graph_context, run_graph_impact,
    run_internal_system, run_report_writer, run_risk, run_strategy_guard)
from .workers.common import dependency_results as _dependency_results
from .workers.common import refs_from_dependencies as _refs_from_dependencies
from .workers.common import safe_public_value as _safe


class SpecialistRuntime:
    """执行一个已经完成分配的 Worker。

    查询/生成 Worker 仍可把可复用 BusinessData 发布到 RunContextStore。
    W09 是例外的纯分析 Consumer：它只读取 Coordinator 已存在的其他 GraphWorkerResult，
    不从 RunContextStore、Tool、数据库或 RAG 获取业务事实。
    """

    def __init__(self, *, llm_service: LLMService, provider: GraphProviderAdapter,
                 impact_service: GraphImpactService, directory: CapabilityWorkerDirectory | None = None,
                 resource_budget: RuntimeResourceBudget | None = None) -> None:
        self.llm_service = llm_service
        self.provider = provider
        self.impact_service = impact_service
        self.directory = directory or CapabilityWorkerDirectory()
        self.resource_budget = resource_budget
        self.context_bundle: ContextBundle | None = None
        self.worker_tool_registry = build_worker_tool_registry(provider=provider, impact_service=impact_service)
        self.worker_tool_directory = WorkerToolDirectory(self.worker_tool_registry)
        self.worker_tool_executor = ToolExecutor(registry=self.worker_tool_registry)
        validator = ToolDagValidator(self.worker_tool_registry, self.worker_tool_directory)
        planner = WorkerToolDagPlanner(llm_service=self.llm_service, directory=self.worker_tool_directory, validator=validator)
        self.worker_tool_dag_runtime = WorkerToolDagRuntime(
            planner=planner,
            executor=ToolDagExecutor(self.worker_tool_executor, max_parallel=4, resource_budget=self.resource_budget),
        )
        self.contract_validator = CapabilityContractValidator()
        self.parameter_resolver = BusinessParameterResolver()

    def bind_context_bundle(self, bundle: ContextBundle) -> None:
        self.context_bundle = bundle

    @staticmethod
    def _ref_dict(ref: Any) -> dict[str, Any]:
        if isinstance(ref, dict):
            return dict(ref)
        if hasattr(ref, "to_dict"):
            return dict(ref.to_dict())
        return {}

    @staticmethod
    def _produced_data(result: GraphWorkerResult) -> tuple[dict[str, Any], list[str]]:
        payload = dict(result.data or {}) if isinstance(result.data, dict) else {}
        values = dict(payload.get("business_data") or {}) if isinstance(payload.get("business_data"), dict) else {}
        names = [str(name) for name in payload.get("produced_data_names") or values.keys() if str(name)]
        names = list(dict.fromkeys(name for name in names if name in values))
        return values, names

    def _working_context(self, task: GraphAgentTask) -> dict[str, Any]:
        if self.context_bundle is None:
            context = {
                "schema_version": "run_context_business_data.v1",
                "run_id": task.run_id,
                "entities": [],
                "global_data": {},
                "global_contracts": {},
                "available_names": [],
            }
        else:
            context = self.context_bundle.business_data_context(entity_refs=[self._ref_dict(ref) for ref in task.focus_refs])
        context["runtime_input_quality"] = {
            "degraded_execution": bool(task.metadata.get("degraded_execution")),
            "missing_preferred_data_names": list(task.metadata.get("missing_preferred_data_names") or []),
            "missing_optional_data_names": list(task.metadata.get("missing_optional_data_names") or []),
            "input_gate": dict(task.metadata.get("input_gate") or {}),
        }
        return context

    def _provider_reuse(self, task: GraphAgentTask) -> GraphWorkerResult | None:
        if self.context_bundle is None:
            return None
        card = self.directory.get(task.worker_id or task.assigned_agent)
        if card.working_memory_mode != "provider" or not task.expected_data_names:
            return None
        refs = list(task.focus_refs or [])
        if refs:
            missing = self.context_bundle.missing_business_data_entities(entity_refs=refs, names=task.expected_data_names)
            if missing:
                return None
            view = self._working_context(task)
            values: dict[str, Any] = {}
            for entity in view.get("entities") or []:
                if not isinstance(entity, dict):
                    continue
                for name, value in dict(entity.get("data") or {}).items():
                    if name in task.expected_data_names:
                        values.setdefault(name, value)
        else:
            view = self.context_bundle.business_data_context()
            global_data = dict(view.get("global_data") or {})
            if not all(name in global_data for name in task.expected_data_names):
                return None
            values = {name: global_data.get(name) for name in task.expected_data_names}
        if not values:
            return None
        data = {"business_data": values, "produced_data_names": list(values),
                "business_empty": all(value in (None, {}, [], "") for value in values.values()),
                "working_memory_reused": True}
        result = GraphWorkerResult(task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.COMPLETED,
            output_type="CapabilityResult", data=data, payload=data, payload_schema="capability_result.v2", error=None,
            focus_refs=task.focus_refs, summary="已复用本轮RunContextStore中的同实体已查询数据，未重复调用查询Tool。",
            findings=[{"kind": "run_context_store_reuse", "data_names": list(values)}], confidence=1.0,
            metadata={"working_memory_reused": True, "database_write": False})
        result.completion = runtime_completion_report(task, result_status=result.status, output_type=result.output_type,
                                                      data=result.data, error=result.error)
        return result

    def _provider_execution_task(self, task: GraphAgentTask) -> GraphAgentTask:
        if self.context_bundle is None or not task.focus_refs or not task.expected_data_names:
            return task
        card = self.directory.get(task.worker_id or task.assigned_agent)
        if card.working_memory_mode != "provider":
            return task
        missing = self.context_bundle.missing_business_data_entities(entity_refs=task.focus_refs, names=task.expected_data_names)
        if not missing or len(missing) == len(task.focus_refs):
            return task
        clone = copy.deepcopy(task)
        clone.focus_refs = list(missing)
        missing_ids = {getattr(ref, "node_id", "") for ref in missing}
        clone.context_refs = [ref for ref in clone.context_refs if getattr(ref, "node_id", "") in missing_ids]
        clone.metadata["working_memory_partial_reuse"] = True
        return clone

    @staticmethod
    def _candidate_refs_from_value(value: Any) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        def visit(node: Any) -> None:
            if isinstance(node, dict):
                for key in ("selected_entity_ref", "entity_ref", "graph_ref"):
                    ref = node.get(key)
                    if isinstance(ref, dict) and str(ref.get("node_id") or ""):
                        refs.append(dict(ref))
                for key in ("entity_refs", "focus_refs"):
                    seq = node.get(key)
                    if isinstance(seq, list):
                        for ref in seq:
                            if isinstance(ref, dict) and str(ref.get("node_id") or ""):
                                refs.append(dict(ref))
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)
        visit(value)
        unique: dict[str, dict[str, Any]] = {}
        for ref in refs:
            unique[str(ref.get("node_id"))] = ref
        return list(unique.values())

    def _publish_business_data(self, task: GraphAgentTask, result: GraphWorkerResult) -> list[dict[str, Any]]:
        if self.context_bundle is None or result.status not in {ResultStatus.COMPLETED, ResultStatus.PARTIAL, ResultStatus.PROPOSAL_READY}:
            return []
        values, names = self._produced_data(result)
        if not names:
            return []
        refs = [self._ref_dict(ref) for ref in (result.focus_refs or task.focus_refs) if self._ref_dict(ref)]
        if not refs:
            for value in values.values():
                refs.extend(self._candidate_refs_from_value(value))
        unique_refs: dict[str, dict[str, Any]] = {str(ref.get("node_id")): ref for ref in refs if str(ref.get("node_id") or "")}
        target_refs = list(unique_refs.values()) or [None]
        payload = dict(result.data or {}) if isinstance(result.data, dict) else {}
        declared_contracts = (
            dict(payload.get("business_data_contracts") or {})
            if isinstance(payload.get("business_data_contracts"), dict)
            else {}
        )
        records: list[dict[str, Any]] = []
        for ref in target_refs:
            for name in names:
                descriptor = (
                    dict(declared_contracts.get(name) or {})
                    if isinstance(declared_contracts.get(name), dict)
                    else ToolOutputContract(
                        slot_id=name,
                        source_path="business_data",
                    ).contract_descriptor()
                )
                records.append(
                    self.context_bundle.put_business_data(
                        entity_ref=ref,
                        name=name,
                        value=values.get(name),
                        data_time=str(task.as_of_time or ""),
                        contract=str(descriptor.get("contract") or name),
                        version=str(descriptor.get("version") or "1.0"),
                        schema_id=str(descriptor.get("schema_id") or ""),
                        provenance={
                            "producer_type": "worker",
                            "producer_id": str(task.worker_id or task.assigned_agent),
                            "task_id": str(task.task_id or ""),
                            "run_id": str(task.run_id or ""),
                            **dict(descriptor.get("provenance") or {}),
                        },
                    )
                )
        return records

    def _parameter_gate(self, task: GraphAgentTask, context: dict[str, Any], language: str) -> GraphWorkerResult | None:
        contracts = [CapabilityContract.from_dict(item) for item in task.contracts]
        resolution = self.parameter_resolver.resolve(contracts=contracts, business_parameters=task.business_parameters,
            available_parameters=dict(context.get("available_parameters") or {}))
        if resolution.satisfied:
            return None
        missing = [MissingContextItem(key=gap.parameter_id, description=gap.description,
            expected_format=gap.expected_format or "business parameter", reason="user_owned_parameter_missing",
            searched_sources=gap.searched_sources, blocking=True) for gap in resolution.gaps]
        result = GraphWorkerResult(task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.NEED_CONTEXT,
            output_type="CapabilityResult", data=None,
            error={"error_id": "user_input_required", "code": "user_input_required", "message": "缺少用户业务参数。",
                   "component": "business_parameter_resolver", "retryable": False}, focus_refs=task.focus_refs,
            summary="缺少执行当前任务所需的用户业务参数。" if language != "en" else "Required user business parameters are missing.",
            missing_items=missing, completion=non_success_completion_report(task, execution_status="need_context",
                reason="missing user business parameter", failure_kind="user_input_required"),
            metadata={"input_gate_owner": "runtime", "parameter_resolution": resolution.to_dict()})
        return result

    def run(self, task: GraphAgentTask, *, current_user_request: str, output_dir: str | Path,
            db_path: str | Path | None, default_top_k: int, language: str,
            execution_context: dict[str, Any] | None = None) -> GraphWorkerResult:
        started = time.perf_counter()
        context = dict(execution_context or {})
        context.update({"current_user_request": current_user_request, "language": language})
        activate_llm_audit_context(run_id=task.run_id, conversation_id=task.session_id, output_dir=output_dir,
            formal_entry_used=True, formal_entry_name="agent.collaboration.specialist_runtime", task_id=task.task_id,
            worker_id=task.worker_id, agent_id=task.assigned_agent)
        task.status = TaskStatus.RUNNING
        gate = self._parameter_gate(task, context, language)
        if gate is not None:
            task.status = TaskStatus.WAITING_CONTEXT
            return gate

        reused = self._provider_reuse(task)
        execution_task = self._provider_execution_task(task) if reused is None else task
        card = self.directory.get(task.worker_id or task.assigned_agent)
        allowed_tools = list(card.private_tool_ids)
        # W09 must not read RunContextStore/ContextBundle business data. Other
        # Workers keep the existing working-context behavior.
        working_context = {} if task.assigned_agent == ENTITY_ANALYST else self._working_context(execution_task)
        if reused is not None:
            result = reused
        else:
            try:
                if task.assigned_agent == EVIDENCE_COLLECTOR:
                    result = run_evidence(self.worker_tool_dag_runtime, execution_task, current_user_request,
                        output_dir, db_path, default_top_k, worker_prompt=str(card.private_worker_prompt or ""),
                        allowed_tool_names=allowed_tools)
                elif task.assigned_agent == PORTFOLIO_ANALYST:
                    result = run_internal_system(self.worker_tool_dag_runtime, execution_task, output_dir, db_path,
                        default_top_k, worker_prompt=str(card.private_worker_prompt or ""), allowed_tool_names=allowed_tools,
                        provider=self.provider)
                elif task.assigned_agent == DATABASE_WRITER:
                    result = run_graph_context(self.worker_tool_executor, execution_task, output_dir, db_path,
                        working_memory_context=working_context)
                elif task.assigned_agent == ENTITY_ANALYST:
                    result = run_entity_analysis(
                        self.llm_service, execution_task,
                        worker_results_context=context.get("worker_results_context"),
                        language=language,
                    )
                elif task.assigned_agent == GRAPH_RELATION_RETRIEVER:
                    result = run_graph_impact(self.worker_tool_dag_runtime, execution_task, working_memory_context=working_context,
                        worker_prompt=str(card.private_worker_prompt or ""), allowed_tool_names=allowed_tools,
                        output_dir=output_dir, db_path=db_path)
                elif task.assigned_agent == RISK_ANALYST:
                    result = run_risk(self.llm_service, self.worker_tool_dag_runtime, execution_task, output_dir, db_path,
                        working_memory_context=working_context, worker_prompt=str(card.private_worker_prompt or ""),
                        allowed_tool_names=allowed_tools, language=language)
                elif task.assigned_agent == STRATEGY_GUARD:
                    result = run_strategy_guard(self.llm_service, execution_task, current_user_request=current_user_request,
                        working_memory_context=working_context, output_dir=output_dir, db_path=db_path,
                        default_top_k=default_top_k, language=language, execution_context=context)
                elif task.assigned_agent == REPORT_WRITER:
                    result = run_report_writer(self.llm_service, execution_task, language,
                        request_bundle_results=context.get("request_bundle_results"),
                        presentation_policy=dict(context.get("presentation_policy") or {}))
                elif task.assigned_agent == SYSTEM_DIAGNOSTIC:
                    result = run_diagnostic(self.provider, execution_task)
                else:
                    raise RuntimeError(f"unknown_worker_agent:{task.assigned_agent}")
            except Exception as exc:
                exc_name = type(exc).__name__
                exc_text = str(exc)
                if exc_name == "LLMJSONError" and (
                    "ToolDagContractViolation" in exc_text
                    or "tool_dag_" in exc_text
                    or "worker_private_tool" in exc_text
                ):
                    kind = "worker_private_tool_planning_failure"
                elif exc_name == "LLMJSONError" or "json" in exc_name.lower():
                    kind = "structured_output_failure"
                else:
                    kind = "worker_execution_failure"
                # Execution/planning failures are recoverable at Coordinator level.
                # User/context blockers are represented separately as NEED_CONTEXT.
                retryable = True
                result = GraphWorkerResult(task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.FAILED,
                    output_type="CapabilityResult", data=None,
                    error={"code": kind, "message": exc_text, "component": task.assigned_agent,
                           "retryable": retryable},
                    focus_refs=task.focus_refs, summary="Worker执行失败。", warnings=[f"{exc_name}:{exc_text}"],
                    completion=non_success_completion_report(task, execution_status="failed", reason=exc_text, failure_kind=kind))

        published = [] if reused is not None else self._publish_business_data(task, result)
        values, produced = self._produced_data(result)
        result.metadata.update({"boundary_id": task.boundary_id, "attempt": task.attempt,
            "business_data_owner": "ContextBundle", "produced_data_names": produced,
            "working_memory_records_published": len(published), "can_mutate": bool(card.can_mutate),
            "business_validation_mode": "pass_through"})
        if not result.completion:
            result.completion = runtime_completion_report(task, result_status=result.status, output_type=result.output_type,
                                                          data=result.data, error=result.error)
        contracts = [CapabilityContract.from_dict(item) for item in task.contracts]
        reports = self.contract_validator.validate(contracts=contracts, produced_data_names=set(produced),
            materialized_data=values, result_status=result.status.value, result_payload=result.data,
            evidence_refs=[ref.node_id for ref in result.evidence_refs] or [f"worker_result:{task.task_id}"])
        result.status, result.completion, satisfied = canonicalize_completion_report(task, result_status=result.status,
            completion=result.completion, contract_reports=reports, produced_data_names=produced, result_data=result.data)
        if satisfied:
            result.error = None
            result.missing_items = []
        if bool(task.metadata.get("degraded_execution")) and result.status in {ResultStatus.COMPLETED, ResultStatus.PARTIAL}:
            missing_preferred = list(task.metadata.get("missing_preferred_data_names") or [])
            missing_optional = list(task.metadata.get("missing_optional_data_names") or [])
            result.warnings = list(dict.fromkeys([
                *result.warnings,
                *( ["degraded_missing_preferred:" + ",".join(missing_preferred)] if missing_preferred else [] ),
                *( ["degraded_missing_optional:" + ",".join(missing_optional)] if missing_optional else [] ),
            ]))
            result.metadata["completed_with_limitations"] = True
            result.metadata["missing_preferred_data_names"] = missing_preferred
            result.metadata["missing_optional_data_names"] = missing_optional
        decision = flow_decision(result.status, result.completion, output_type=result.output_type,
                                 retryable=bool((result.error or {}).get("retryable")))
        result.status = decision.result_status
        result.metadata.update({"semantic_satisfied": decision.semantic_satisfied, "should_freeze": decision.should_freeze,
            "reusable": decision.reusable, "replan_recommended": decision.replan_recommended,
            "failure_kind": decision.failure_kind, "freeze_reason": decision.freeze_reason})
        escalation = escalation_from_worker_result(task, result)
        if escalation is not None:
            result.metadata["worker_escalation_retryable"] = bool((result.error or {}).get("retryable"))
            result.error = escalation.to_dict()
            result.metadata["worker_escalation"] = escalation.to_dict()

        result.metadata["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        llm_timing = get_llm_execution_timing(task.run_id, task.task_id)
        tool_timing = get_tool_execution_timing(task.run_id, task.task_id)
        result.metadata["llm_execution_timing"] = llm_timing
        result.metadata["tool_execution_timing"] = tool_timing
        result.metadata["unattributed_worker_execution_ms"] = round(max(0.0, float(result.metadata["duration_ms"])
            - float(llm_timing.get("provider_transport_ms_sum") or 0.0) - float(tool_timing.get("wall_duration_ms") or 0.0)), 3)
        publish_agent_message(output_dir=output_dir, user_id=task.user_id, conversation_id=task.session_id,
            run_id=task.run_id, task_id=task.task_id, sender=task.assigned_agent, receiver="COORDINATOR",
            message_type=MessageType.WORKER_RESULT_AVAILABLE,
            payload={"status": result.status.value, "output_type": result.output_type, "summary": result.summary[:500],
                     "boundary_id": task.boundary_id, "produced_data_names": produced,
                     "expected_task_completed": bool(result.completion.get("expected_task_completed"))},
            payload_schema="capability_worker_result_available.v2", context_refs=[ref.to_dict() for ref in result.focus_refs[:20]],
            artifact_refs=list(result.artifact_refs[:20]), source_refs=[ref.to_dict() for ref in result.evidence_refs[:20]],
            warnings=list(result.warnings[:10]), error=dict(result.error or {}),
            metadata={"worker_id": task.worker_id, "boundary_id": task.boundary_id})
        task.status = (TaskStatus.COMPLETED if result.status in {ResultStatus.COMPLETED, ResultStatus.PROPOSAL_READY}
            else TaskStatus.PARTIAL if result.status == ResultStatus.PARTIAL
            else TaskStatus.WAITING_CONTEXT if result.status == ResultStatus.NEED_CONTEXT else TaskStatus.FAILED)
        return result


__all__ = ["SpecialistRuntime", "_dependency_results", "_refs_from_dependencies", "_safe"]
