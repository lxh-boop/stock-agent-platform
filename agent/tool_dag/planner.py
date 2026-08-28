"""LLM planner for Worker-private Tool DAGs.

Each Worker sees its own fixed private Tool contracts. Runtime does not guess
producer/consumer bindings before DAG planning; the DAG explicitly decides which
context values and upstream Tool outputs satisfy each Tool input.
"""

from __future__ import annotations

from typing import Any

from core.llm import LLMService
from core.llm.prompt_compaction import catalog_for_prompt, compact_json_dumps, schema_for_prompt

from agent.console_trace import flow_event, runtime_debug_event
from agent.worker_tools import WorkerToolDirectory

from .contracts import TOOL_DAG_OUTPUT_SCHEMA, ToolDagContractViolation, ToolDagPlan
from .validation import ToolDagValidator




def _safe_planning_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return "<max_depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, (list, tuple)):
        return [_safe_planning_value(item, depth=depth + 1) for item in list(value)[:20]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            name = str(key)
            lowered = name.lower()
            if any(marker in lowered for marker in ("password", "secret", "api_key", "token")):
                result[name] = "<redacted>"
            else:
                result[name] = _safe_planning_value(item, depth=depth + 1)
        return result
    if hasattr(value, "to_dict"):
        try:
            return _safe_planning_value(value.to_dict(), depth=depth + 1)
        except Exception:
            pass
    return str(value)[:500]


class WorkerToolDagPlanner:
    def __init__(
        self,
        *,
        llm_service: LLMService,
        directory: WorkerToolDirectory,
        validator: ToolDagValidator,
    ) -> None:
        self.llm_service = llm_service
        self.directory = directory
        self.validator = validator

    def _select_tool_candidates(
        self,
        *,
        worker_task_id: str,
        worker_role: str,
        available_tool_names: set[str],
        required_output_keys: list[str],
        run_id: str,
    ) -> list[str]:
        """Expose the full allowed private Tool catalog to the Worker planner.

        This is deterministic and intentionally does not infer reachability from
        input/output names. The final Tool DAG is the only place where concrete
        producer/consumer bindings are declared.
        """

        selected = self.directory.candidate_tool_names(
            worker_role,
            allowed_tool_names=available_tool_names,
        )
        if not selected:
            raise ToolDagContractViolation(
                "no_worker_private_tool",
                "$.private_tool_catalog",
                worker_role,
            )
        runtime_debug_event(
            "WORKER_PRIVATE_TOOL_EXECUTION_CATALOG_READY",
            {
                "worker_task_id": worker_task_id,
                "worker_role": worker_role,
                "selected_tool_ids": selected,
                "required_output_keys_for_planner_context_only": [str(item) for item in required_output_keys if str(item)],
                "business_goal_coverage_validation": "skipped",
            },
            run_id=run_id,
            task_id=worker_task_id,
            level="DEBUG",
        )
        flow_event(
            "TOOL_PRIVATE_CATALOG_SELECTED",
            {
                "tool_ids": selected,
                "selection_mode": "all_worker_private_tools",
                "candidate_llm_call_used": False,
            },
            run_id=run_id,
            task_id=worker_task_id,
        )
        return selected

    def plan(
        self,
        *,
        worker_task_id: str,
        worker_role: str,
        worker_objective: str,
        boundary_id: str,
        worker_prompt: str,
        available_context: dict[str, Any],
        required_output_keys: list[str],
        completion_criteria: list[str],
        allowed_tool_names: list[str],
        run_id: str,
        read_only: bool,
    ) -> ToolDagPlan:
        available_keys = set(available_context)
        requested_allowed = set(allowed_tool_names)
        selected = self._select_tool_candidates(
            worker_task_id=worker_task_id,
            worker_role=worker_role,
            available_tool_names=requested_allowed,
            required_output_keys=required_output_keys,
            run_id=run_id,
        )
        allowed = set(selected)
        private_catalog = catalog_for_prompt(self.directory.load_details(worker_role, selected))
        flow_event(
            "TOOL_DETAILS_LOADED",
            {
                "worker_role": worker_role,
                "loaded_tool_ids": selected,
                "tool_detail_count": len(private_catalog),
                "main_agent_visibility": "none",
            },
            run_id=run_id,
            task_id=worker_task_id,
        )
        fixed_goal_contract = {
            "goal_summary": str(worker_objective),
            "required_output_keys": list(required_output_keys),
            "completion_criteria": list(completion_criteria),
        }

        def validate(payload: dict[str, Any]) -> None:
            candidate = {**dict(payload or {}), "goal_contract": fixed_goal_contract}
            self.validator.validate_payload(
                candidate,
                worker_role=worker_role,
                worker_task_id=worker_task_id,
                available_context_keys=available_keys,
                allowed_tool_names=allowed,
                read_only=read_only,
            )

        def emit(event: str, payload: dict[str, Any]) -> None:
            names = {
                "request_started": "TOOL_DAG_PLANNING_STARTED",
                "response_received": "TOOL_DAG_PLANNING_RESPONSE_RECEIVED",
                "candidate_generated": "TOOL_DAG_CANDIDATE_GENERATED",
                "validation_succeeded": "TOOL_DAG_VALIDATION_SUCCEEDED",
                "validation_failed": "TOOL_DAG_VALIDATION_FAILED",
                "repair_started": "TOOL_DAG_REPAIR_STARTED",
                "repair_response_received": "TOOL_DAG_REPAIR_RESPONSE_RECEIVED",
                "repair_candidate_generated": "TOOL_DAG_REPAIR_CANDIDATE_GENERATED",
                "repair_validation_succeeded": "TOOL_DAG_REPAIR_SUCCEEDED",
                "repair_failed": "TOOL_DAG_REPAIR_FAILED",
            }
            flow_event(
                names.get(event, f"TOOL_DAG_{event.upper()}"),
                payload,
                run_id=run_id,
                task_id=worker_task_id,
                level="ERROR" if "failed" in event else "INFO",
            )

        system = (
            "你是专业Worker内部的Tool DAG Planner。private_tool_details_catalog包含该Worker可用的固定Tool合同。"
            "只能从 private_tool_details_catalog 选择Tool；不得选择其他Worker或修改Worker DAG。"
            "根据任务目标和available_context生成可执行的最小Tool DAG。required_output_keys只用于理解Worker目标，不是执行前的业务覆盖校验。Tool彼此独立；只有本次DAG决定哪个输入引用哪个上下文或上游Tool输出。args只放普通常量，权威值通过inputs引用。"
            "当输入来自前序Tool时，使用{from_tool_task_id, output_slot}；当输入合同cardinality=many时，inputs对应值必须是由一个或多个引用组成的List。"
            "output_slot必须来自上游Tool的output_contract；不要猜测records/items等Python内部data_key。final_output_task_ids必须指向完成目标的末端任务。"
            "不要输出goal_contract或expected_output_keys。严格输出JSON。"
        )
        payload = self.llm_service.generate_json(
            stage="worker_private_tool_dag_planner",
            messages=[
                {"role": "system", "content": system + "\nWorker private boundary:\n" + str(worker_prompt or "")},
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "worker_task_id": worker_task_id,
                        "worker_role": worker_role,
                        "boundary_id": boundary_id,
                        "worker_objective": worker_objective,
                        "available_context_keys": sorted(available_keys),
                        "available_context": _safe_planning_value(available_context),
                        "fixed_goal_contract": fixed_goal_contract,
                        "private_tool_details_catalog": private_catalog,
                        "tool_dag_output_schema": schema_for_prompt(TOOL_DAG_OUTPUT_SCHEMA),
                    }),
                },
            ],
            max_output_tokens=2000,
            validator=validate,
            operation=f"worker_private_tool_dag_plan:{worker_role}:{boundary_id}",
            disable_thinking=True,
            event_callback=emit,
            repair_mode="targeted",
            repair_guidance=(
                "只修复Tool、参数引用、依赖、最终输出或无用节点。不得增加未加载Tool。保持最小DAG。"
            ),
        )
        return self.validator.validate_payload(
            {**dict(payload or {}), "goal_contract": fixed_goal_contract},
            worker_role=worker_role,
            worker_task_id=worker_task_id,
            available_context_keys=available_keys,
            allowed_tool_names=allowed,
            read_only=read_only,
        )

    def replan(
        self,
        *,
        previous_plan: ToolDagPlan,
        node_records: list[dict[str, Any]],
        reusable_results: dict[str, Any],
        available_context: dict[str, Any],
        worker_prompt: str,
        allowed_tool_names: list[str],
        run_id: str,
        read_only: bool,
    ) -> ToolDagPlan:
        reusable_ids = set(reusable_results)

        # Local Tool-DAG replanning is entered only after a real Tool execution
        # failure. Business-output sufficiency is intentionally not evaluated here.

        frozen_rows = [
            task.planning_dict()
            for task in previous_plan.tasks
            if task.tool_task_id in reusable_ids
        ]
        frozen_signatures = {
            task.tool_task_id: task.to_dict()
            for task in previous_plan.tasks
            if task.tool_task_id in reusable_ids
        }
        previous_ids = {task.tool_task_id for task in previous_plan.tasks}
        available_keys = set(available_context)
        requested_allowed = set(allowed_tool_names)
        frozen_tool_names = {
            str(task.tool_name)
            for task in previous_plan.tasks
            if task.tool_task_id in reusable_ids
        }
        selected = self._select_tool_candidates(
            worker_task_id=previous_plan.worker_task_id,
            worker_role=previous_plan.worker_role,
            available_tool_names=requested_allowed.union(frozen_tool_names),
            required_output_keys=list(previous_plan.goal_contract.get("required_output_keys") or []),
            run_id=run_id,
        )
        allowed = set(selected).union(frozen_tool_names)
        private_catalog = catalog_for_prompt(
            self.directory.load_details(previous_plan.worker_role, sorted(allowed))
        )
        flow_event(
            "TOOL_DETAILS_LOADED",
            {
                "worker_role": previous_plan.worker_role,
                "loaded_tool_ids": sorted(allowed),
                "tool_detail_count": len(private_catalog),
                "replan": True,
            },
            run_id=run_id,
            task_id=previous_plan.worker_task_id,
        )

        def validate(payload: dict[str, Any]) -> None:
            candidate = {**dict(payload or {}), "goal_contract": dict(previous_plan.goal_contract or {})}
            self.validator.validate_payload(
                candidate,
                worker_role=previous_plan.worker_role,
                worker_task_id=previous_plan.worker_task_id,
                available_context_keys=available_keys,
                allowed_tool_names=allowed,
                read_only=read_only,
                frozen_task_signatures=frozen_signatures,
                previous_task_ids=previous_ids,
            )

        def emit(event: str, payload: dict[str, Any]) -> None:
            flow_event(
                f"TOOL_DAG_REPLAN_{event.upper()}",
                payload,
                run_id=run_id,
                task_id=previous_plan.worker_task_id,
                level="ERROR" if "failed" in event else "INFO",
            )

        payload = self.llm_service.generate_json(
            stage="progressive_worker_tool_dag_replanner",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你负责Worker内部Tool DAG局部重规划。保留frozen_reusable_tasks，只新增完成剩余目标的最小替代子图。"
                        "新节点可以通过{from_tool_task_id, output_slot}直接消费frozen_reusable_tasks的语义Output Slot，不要重复执行已冻结成功节点，也不要引用其Python内部data_key。"
                        "只能使用已加载Tool详情，不修改Worker目标。若现有冻结结果已经满足目标，不应进入本阶段。"
                        "严格输出JSON。\n" + str(worker_prompt or "")
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "fixed_goal_contract": previous_plan.goal_contract,
                        "available_context_keys": sorted(available_keys),
                        "available_context": _safe_planning_value(available_context),
                        "frozen_reusable_tasks": frozen_rows,
                        "node_execution_records": node_records,
                        "previous_task_ids": sorted(previous_ids),
                        "private_tool_details_catalog": private_catalog,
                        "tool_dag_output_schema": schema_for_prompt(TOOL_DAG_OUTPUT_SCHEMA),
                    }),
                },
            ],
            max_output_tokens=2400,
            validator=validate,
            operation=f"progressive_worker_tool_dag_replan:{previous_plan.worker_role}",
            disable_thinking=True,
            event_callback=emit,
            repair_mode="targeted",
            repair_guidance="保留冻结结果，只修复未完成部分；跨Tool传递使用output_slot，不得猜测data_key；不得返回空DAG或使用未加载Tool。",
        )
        return self.validator.validate_payload(
            {**dict(payload or {}), "goal_contract": dict(previous_plan.goal_contract or {})},
            worker_role=previous_plan.worker_role,
            worker_task_id=previous_plan.worker_task_id,
            available_context_keys=available_keys,
            allowed_tool_names=allowed,
            read_only=read_only,
            frozen_task_signatures=frozen_signatures,
            previous_task_ids=previous_ids,
        )


__all__ = ["WorkerToolDagPlanner"]
