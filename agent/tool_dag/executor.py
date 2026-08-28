"""Topological parallel executor for validated Worker-private Tool DAGs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from agent.console_trace import flow_event
from agent.tool_runtime import ToolError, ToolExecutor, UnifiedToolResult
from agent.runtime_state import RuntimeResourceBudget

from .contracts import (
    ToolDagExecutionResult,
    ToolDagPlan,
    ToolDagTask,
    ToolNodeExecutionRecord,
)
from .validation import dependencies_from_inputs


class ToolDagExecutor:
    def __init__(
        self, tool_executor: ToolExecutor, *, max_parallel: int = 4,
        resource_budget: RuntimeResourceBudget | None = None,
    ) -> None:
        self.tool_executor = tool_executor
        self.resource_budget = resource_budget
        configured = resource_budget.max_parallel_tools if resource_budget is not None else max_parallel
        self.max_parallel = max(1, min(int(max_parallel), int(configured)))

    @staticmethod
    def _resolve_ref(
        spec: Any,
        *,
        context: dict[str, Any],
        results: dict[str, UnifiedToolResult],
    ) -> Any:
        if isinstance(spec, list):
            return [
                ToolDagExecutor._resolve_ref(item, context=context, results=results)
                for item in spec
            ]
        if not isinstance(spec, dict):
            return spec
        if "from_context" in spec:
            return context[str(spec.get("from_context") or "")]
        if "from_tool_task_id" in spec:
            result = results[str(spec.get("from_tool_task_id") or "")]
            output_slot = str(spec.get("output_slot") or "").strip()
            if output_slot:
                slots = (result.data or {}).get("slots")
                if not isinstance(slots, dict) or output_slot not in slots:
                    raise KeyError(f"tool_output_slot_missing:{output_slot}")
                return slots[output_slot]
            raise KeyError(f"tool_output_slot_required:{spec.get('from_tool_task_id') or ''}")
        return {
            key: ToolDagExecutor._resolve_ref(value, context=context, results=results)
            for key, value in spec.items()
        }

    def _arguments(
        self,
        task: ToolDagTask,
        *,
        context: dict[str, Any],
        results: dict[str, UnifiedToolResult],
    ) -> dict[str, Any]:
        arguments = dict(task.args or {})
        for name, spec in dict(task.inputs or {}).items():
            arguments[name] = self._resolve_ref(spec, context=context, results=results)
        return arguments

    @staticmethod
    def _result_summary(result: UnifiedToolResult) -> dict[str, Any]:
        data = dict(result.data or {})
        rows = data.get("results")
        if isinstance(rows, list):
            nested_record_count = sum(
                len(item.get("records") or [])
                for item in rows
                if isinstance(item, dict)
            )
        else:
            nested_record_count = 0
        record_count = data.get("record_count")
        if record_count is None:
            record_count = nested_record_count
        source_count = data.get("source_count")
        if source_count is None:
            source_count = sum(
                len(item.get("sources") or [])
                for item in rows or []
                if isinstance(item, dict)
            )
        coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
        return {
            "data_keys": sorted(data),
            "record_count": int(record_count or 0),
            "source_count": int(source_count or 0),
            "business_empty": bool(data.get("business_empty", False)),
            "coverage_satisfied": bool(coverage.get("coverage_satisfied", True)),
            "covered_entity_count": int(coverage.get("covered_entity_count") or 0),
            "missing_entity_ref_ids": list(coverage.get("missing_entity_ref_ids") or [])[:20],
            "message": str(result.message or "")[:500],
            "warning_count": len(result.warnings or []),
            "error_count": len(result.errors or []),
        }

    @staticmethod
    def _published_output_keys(result: UnifiedToolResult) -> set[str]:
        data = dict(result.data or {})
        keys = {str(key) for key in data}
        keys.update(
            str(item)
            for item in data.get("produced_information_slots") or []
            if str(item)
        )
        if isinstance(data.get("slots"), dict):
            keys.update(str(key) for key in data["slots"] if str(key))
        return keys

    @classmethod
    def _record_from_result(
        cls,
        task: ToolDagTask,
        result: UnifiedToolResult,
    ) -> ToolNodeExecutionRecord:
        published = cls._published_output_keys(result)
        produced = sorted(published)
        # Business-output sufficiency is intentionally not validated at the Tool
        # execution layer. Missing declared output keys remain diagnostic only.
        missing = sorted(set(task.expected_output_keys) - published)
        execution_success = bool(result.success)
        contract_valid = execution_success
        summary = cls._result_summary(result)
        status = "succeeded" if execution_success else "failed"
        completion_status = "completed" if execution_success else "not_completed"
        business_status = "not_evaluated" if execution_success else "unknown"

        retryable = bool((result.metadata or {}).get("retryable", False))
        reusable = execution_success
        should_freeze = bool(reusable or (status == "failed" and not retryable))
        if reusable:
            freeze_reason = "tool_execution_completed"
        elif should_freeze:
            freeze_reason = "non_retryable_failure_must_not_be_reexecuted"
        else:
            freeze_reason = "node_requires_retry_or_replacement"
        failure = {}
        if status != "succeeded":
            structured = result.error.to_dict() if getattr(result, "error", None) else {}
            failure = {
                "failure_kind": str((result.metadata or {}).get("failure_kind") or "tool_failure"),
                "error_id": str(structured.get("error_id") or result.error_type or "tool_reported_failure"),
                "operation": str(structured.get("operation") or task.objective),
                "reason": str(structured.get("reason") or result.error_message or ";".join(result.errors or []))[:2000],
                "retryable": retryable,
            }
        return ToolNodeExecutionRecord(
            tool_task_id=task.tool_task_id,
            tool_name=task.tool_name,
            objective=task.objective,
            status=status,
            depends_on=dependencies_from_inputs(task.inputs),
            execution_success=execution_success,
            contract_valid=contract_valid,
            completion_status=completion_status,
            business_status=business_status,
            produced_output_keys=produced,
            missing_output_keys=missing,
            result_ref=f"tool_result:{task.tool_task_id}",
            result_summary=summary,
            should_freeze=should_freeze,
            freeze_reason=freeze_reason,
            reusable=reusable,
            retryable=retryable,
            failure=failure,
            duration_ms=float(result.duration_ms or 0.0),
        )

    @staticmethod
    def _blocked_record(task: ToolDagTask, blocked_by: list[str]) -> ToolNodeExecutionRecord:
        return ToolNodeExecutionRecord(
            tool_task_id=task.tool_task_id,
            tool_name=task.tool_name,
            objective=task.objective,
            status="blocked",
            depends_on=dependencies_from_inputs(task.inputs),
            execution_success=False,
            contract_valid=False,
            completion_status="not_completed",
            business_status="unknown",
            produced_output_keys=[],
            missing_output_keys=list(task.expected_output_keys),
            result_ref="",
            result_summary={},
            should_freeze=False,
            freeze_reason="blocked_node_may_be_reconnected_by_local_replan",
            reusable=False,
            retryable=False,
            failure={
                "failure_kind": "upstream_tool_failed",
                "error_type": "blocked_by_failed_dependency",
                "error_message": "Blocked by failed Tool node(s): " + ",".join(blocked_by),
                "retryable": False,
                "blocked_by": list(blocked_by),
            },
            duration_ms=0.0,
        )

    def execute(
        self,
        plan: ToolDagPlan,
        *,
        available_context: dict[str, Any],
        execution_context: dict[str, Any],
        existing_results: dict[str, UnifiedToolResult] | None = None,
        only_task_ids: set[str] | None = None,
    ) -> ToolDagExecutionResult:
        by_id = {task.tool_task_id: task for task in plan.tasks}
        preexisting_ids = set(existing_results or {})
        results: dict[str, UnifiedToolResult] = dict(existing_results or {})
        node_records: list[ToolNodeExecutionRecord] = []
        execution_batches: list[list[str]] = []
        pending = {
            task_id
            for task_id in by_id
            if task_id not in results and (only_task_ids is None or task_id in only_task_ids)
        }
        succeeded = set(results)
        failed: set[str] = set()

        while pending:
            ready = sorted(
                [
                    task_id
                    for task_id in pending
                    if set(dependencies_from_inputs(by_id[task_id].inputs)).issubset(succeeded)
                ],
                key=lambda task_id: (by_id[task_id].priority, task_id),
            )
            if not ready:
                blocked_now: list[str] = []
                for task_id in sorted(pending):
                    dependencies = dependencies_from_inputs(by_id[task_id].inputs)
                    blocked_by = sorted(set(dependencies).intersection(failed))
                    if blocked_by:
                        node_records.append(self._blocked_record(by_id[task_id], blocked_by))
                        blocked_now.append(task_id)
                if blocked_now:
                    pending.difference_update(blocked_now)
                    failed.update(blocked_now)
                    flow_event(
                        "TOOL_DAG_NODES_BLOCKED",
                        {
                            "tool_task_ids": blocked_now,
                            "reason": "upstream_tool_failed",
                        },
                        run_id=str(execution_context.get("run_id") or ""),
                        task_id=plan.worker_task_id,
                        level="WARNING",
                    )
                    continue
                raise RuntimeError("tool_dag_execution_stalled:" + ",".join(sorted(pending)))

            execution_batches.append(ready)
            flow_event(
                "TOOL_DAG_BATCH_STARTED",
                {"tool_task_ids": ready, "parallel": len(ready) > 1},
                run_id=str(execution_context.get("run_id") or ""),
                task_id=plan.worker_task_id,
            )

            def run_one(task_id: str) -> tuple[str, UnifiedToolResult]:
                task = by_id[task_id]
                arguments = self._arguments(task, context=available_context, results=results)
                context = {
                    **dict(execution_context or {}),
                    "task_id": plan.worker_task_id,
                    "tool_task_id": task_id,
                    "agent_role": plan.worker_role,
                }
                if self.resource_budget is None:
                    result = self.tool_executor.execute(
                        task.tool_name, arguments, context=context, agent_type=plan.worker_role
                    )
                else:
                    with self.resource_budget.tool_slot():
                        result = self.tool_executor.execute(
                            task.tool_name, arguments, context=context, agent_type=plan.worker_role
                        )
                return task_id, result

            batch_success: list[str] = []
            batch_failed: list[str] = []
            with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(ready))) as pool:
                futures = {pool.submit(run_one, task_id): task_id for task_id in ready}
                for future in as_completed(futures):
                    task_id = futures[future]
                    try:
                        _, result = future.result()
                    except Exception as exc:
                        task = by_id[task_id]
                        result = UnifiedToolResult(
                            success=False,
                            tool_name=task.tool_name,
                            errors=[f"{type(exc).__name__}:{exc}"],
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                            error=ToolError.create(
                                error_id=type(exc).__name__,
                                operation=task.objective,
                                reason=str(exc),
                            ),
                            metadata={"failure_kind": "tool_failure", "retryable": True},
                        )
                    results[task_id] = result
                    record = self._record_from_result(by_id[task_id], result)
                    node_records.append(record)
                    if record.status == "succeeded":
                        batch_success.append(task_id)
                    else:
                        batch_failed.append(task_id)

            succeeded.update(batch_success)
            failed.update(batch_failed)
            pending.difference_update(ready)
            flow_event(
                "TOOL_DAG_BATCH_COMPLETED",
                {
                    "tool_task_ids": ready,
                    "success_count": len(batch_success),
                    "failure_count": len(batch_failed),
                },
                run_id=str(execution_context.get("run_id") or ""),
                task_id=plan.worker_task_id,
                level="WARNING" if batch_failed else "INFO",
            )

        final_results = [results[task_id] for task_id in plan.final_output_task_ids if task_id in results]
        records_by_id = {record.tool_task_id: record for record in node_records}
        # Tool-DAG success means the selected final Tool nodes executed
        # successfully. Whether their business content is sufficient for the
        # Worker/Request is evaluated elsewhere (currently skipped).
        success = (
            bool(final_results)
            and all(
                (
                    task_id in preexisting_ids
                    and task_id in results
                    and bool(results[task_id].success)
                )
                or (
                    records_by_id.get(task_id) is not None
                    and records_by_id[task_id].status == "succeeded"
                    and records_by_id[task_id].completion_status == "completed"
                )
                for task_id in plan.final_output_task_ids
            )
        )
        return ToolDagExecutionResult(
            plan=plan,
            results=results,
            node_records=node_records,
            execution_batches=execution_batches,
            final_output_task_ids=list(plan.final_output_task_ids),
            final_results=final_results,
            success=success,
        )


__all__ = ["ToolDagExecutor"]
