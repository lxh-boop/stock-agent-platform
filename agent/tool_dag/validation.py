"""Deterministic, accept-or-reject validation for Worker-private Tool DAGs."""

from __future__ import annotations

from typing import Any, Iterable

from agent.tool_runtime import OP_READ, ToolRegistry
from agent.tool_runtime.validation import input_contracts_for, output_contracts_for
from agent.worker_tools import WorkerToolDirectory

from .contracts import ToolDagContractViolation, ToolDagPlan, ToolDagTask


def _iter_input_refs(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "from_context" in value or "from_tool_task_id" in value:
            yield value
            return
        for item in value.values():
            yield from _iter_input_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_input_refs(item)


def _validate_input_binding_shape(value: Any, *, path: str) -> None:
    """Accept only the canonical context/tool-result reference structures."""

    items = value if isinstance(value, list) else [value]
    if not items:
        raise ToolDagContractViolation("tool_task_input_binding_empty", path)
    for index, item in enumerate(items):
        item_path = f"{path}[{index}]" if isinstance(value, list) else path
        if not isinstance(item, dict):
            raise ToolDagContractViolation("tool_task_input_binding_must_be_object", item_path)
        has_context = "from_context" in item
        has_tool = "from_tool_task_id" in item
        if has_context == has_tool:
            raise ToolDagContractViolation("tool_input_ref_requires_one_source", item_path)
        if has_context:
            if set(item) != {"from_context"}:
                raise ToolDagContractViolation(
                    "tool_context_ref_has_unknown_fields", item_path, ",".join(sorted(set(item) - {"from_context"}))
                )
            if not str(item.get("from_context") or "").strip():
                raise ToolDagContractViolation("tool_context_ref_key_required", item_path)
        else:
            unknown = set(item) - {"from_tool_task_id", "output_slot"}
            if unknown:
                raise ToolDagContractViolation(
                    "tool_result_ref_has_unknown_fields", item_path, ",".join(sorted(unknown))
                )
            if not str(item.get("from_tool_task_id") or "").strip():
                raise ToolDagContractViolation("tool_result_ref_task_id_required", item_path)


def _matches_schema_type(value: Any, schema: dict[str, Any]) -> bool:
    wanted = str((schema or {}).get("type") or "")
    if not wanted:
        return True
    mapping: dict[str, Any] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    allowed = mapping.get(wanted)
    if allowed is None:
        return True
    if wanted in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, allowed)

def dependencies_from_inputs(inputs: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for ref in _iter_input_refs(inputs):
        task_id = str(ref.get("from_tool_task_id") or "").strip()
        if task_id and task_id not in rows:
            rows.append(task_id)
    return rows




def _input_contract(definition: Any, slot_id: str) -> Any | None:
    for item in input_contracts_for(definition):
        if str(item.slot_id) == str(slot_id):
            return item
    return None


def _output_contract(definition: Any, slot_id: str) -> Any | None:
    for item in output_contracts_for(definition):
        if str(item.slot_id) == str(slot_id):
            return item
    return None


def _schema_compatible(producer: Any | None, consumer: Any | None) -> bool:
    if producer is None or consumer is None:
        return True
    left = str(getattr(producer, "schema_id", "") or "").strip()
    right = str(getattr(consumer, "schema_id", "") or "").strip()
    return not left or not right or left == right


def _artifact_contract_compatible(producer: Any | None, consumer: Any | None) -> bool:
    if producer is None or consumer is None:
        return True
    producer_contract = str(getattr(producer, "contract", "") or "").strip()
    consumer_contract = str(getattr(consumer, "contract", "") or "").strip()
    if producer_contract and consumer_contract and producer_contract != consumer_contract:
        return False
    producer_version = str(getattr(producer, "version", "") or "1.0").strip()
    accepted_versions = tuple(
        str(item).strip()
        for item in (getattr(consumer, "accepted_versions", ()) or ())
        if str(item).strip()
    )
    consumer_version = str(getattr(consumer, "version", "") or "1.0").strip()
    accepted = accepted_versions or (consumer_version,)
    return producer_version in set(accepted)

class ToolDagValidator:
    """Validate whether a Worker-private Tool DAG is safe and executable.

    This validator deliberately does not judge business-goal coverage or result
    sufficiency.  It only enforces registered Tool availability/permission,
    argument/input contracts, dependency references and DAG structure.
    """

    def __init__(self, registry: ToolRegistry, directory: WorkerToolDirectory) -> None:
        self.registry = registry
        self.directory = directory

    def validate_payload(
        self,
        payload: dict[str, Any],
        *,
        worker_role: str,
        worker_task_id: str,
        available_context_keys: set[str],
        allowed_tool_names: set[str] | None = None,
        read_only: bool = False,
        frozen_task_signatures: dict[str, dict[str, Any]] | None = None,
        previous_task_ids: set[str] | None = None,
    ) -> ToolDagPlan:
        if not isinstance(payload, dict):
            raise ToolDagContractViolation("tool_dag_payload_must_be_object", "$")
        goal = payload.get("goal_contract")
        rows = payload.get("tasks")
        finals = payload.get("final_output_task_ids")
        if not isinstance(goal, dict):
            raise ToolDagContractViolation("tool_dag_goal_contract_required", "$.goal_contract")
        if not isinstance(rows, list) or not rows:
            raise ToolDagContractViolation("tool_dag_tasks_required", "$.tasks")
        if not isinstance(finals, list) or not finals:
            raise ToolDagContractViolation("tool_dag_final_outputs_required", "$.final_output_task_ids")

        allowed = set(allowed_tool_names or self.directory.allowed_tool_names(worker_role))
        task_ids: list[str] = []
        tasks: list[ToolDagTask] = []
        definitions: dict[str, Any] = {}
        signatures = dict(frozen_task_signatures or {})
        previous = set(previous_task_ids or set())
        for index, row in enumerate(rows):
            path = f"$.tasks[{index}]"
            if not isinstance(row, dict):
                raise ToolDagContractViolation("tool_dag_task_must_be_object", path)
            task_id = str(row.get("tool_task_id") or "").strip()
            tool_name = str(row.get("tool_name") or "").strip()
            objective = str(row.get("objective") or "").strip()
            args = row.get("args")
            inputs = row.get("inputs")
            if not task_id:
                raise ToolDagContractViolation("tool_task_id_required", f"{path}.tool_task_id")
            if task_id in task_ids:
                raise ToolDagContractViolation("duplicate_tool_task_id", f"{path}.tool_task_id", task_id)
            if task_id in previous and task_id not in signatures:
                raise ToolDagContractViolation("tool_replan_reused_superseded_task_id", f"{path}.tool_task_id", task_id)
            if not objective:
                raise ToolDagContractViolation("tool_task_objective_required", f"{path}.objective")
            if tool_name not in allowed or not self.directory.allows(worker_role, tool_name):
                raise ToolDagContractViolation("worker_private_tool_not_allowed", f"{path}.tool_name", tool_name)
            definition = self.registry.get(tool_name)
            if definition is None or not definition.enabled:
                raise ToolDagContractViolation("tool_not_available", f"{path}.tool_name", tool_name)
            if read_only and definition.operation_type != OP_READ:
                raise ToolDagContractViolation("read_only_worker_selected_non_read_tool", f"{path}.tool_name", tool_name)
            if not isinstance(args, dict):
                raise ToolDagContractViolation("tool_task_args_must_be_object", f"{path}.args")
            if not isinstance(inputs, dict):
                raise ToolDagContractViolation("tool_task_inputs_must_be_object", f"{path}.inputs")
            properties = definition.input_schema.get("properties") or {}
            overlapping = sorted(set(args) & set(inputs))
            if overlapping:
                raise ToolDagContractViolation(
                    "tool_task_argument_declared_in_args_and_inputs",
                    path,
                    ",".join(overlapping),
                )
            supplied_keys = set(args) | set(inputs)
            unknown = sorted(supplied_keys - set(properties))
            if unknown:
                raise ToolDagContractViolation("tool_task_unknown_argument", path, ",".join(unknown))
            missing = sorted(set(definition.input_schema.get("required") or []) - supplied_keys)
            if missing:
                raise ToolDagContractViolation("tool_task_missing_required_argument", path, ",".join(missing))
            for name, value in args.items():
                if name in properties and not _matches_schema_type(value, properties.get(name) or {}):
                    raise ToolDagContractViolation(
                        "tool_task_argument_type_mismatch",
                        f"{path}.args.{name}",
                        str((properties.get(name) or {}).get("type") or ""),
                    )
            for name, spec in inputs.items():
                contract = _input_contract(definition, name)
                cardinality = str(getattr(contract, "cardinality", "one") or "one")
                if cardinality == "many":
                    if not isinstance(spec, list) or not spec:
                        raise ToolDagContractViolation(
                            "tool_input_many_requires_non_empty_list",
                            f"{path}.inputs.{name}",
                        )
                elif isinstance(spec, list):
                    raise ToolDagContractViolation(
                        "tool_input_one_requires_single_binding",
                        f"{path}.inputs.{name}",
                    )
                _validate_input_binding_shape(spec, path=f"{path}.inputs.{name}")
                refs = list(_iter_input_refs(spec))
                if not refs:
                    raise ToolDagContractViolation(
                        "tool_task_input_must_reference_context_or_tool",
                        f"{path}.inputs.{name}",
                    )
                if contract is not None and contract.accepted_sources:
                    allowed_sources = set(contract.accepted_sources)
                    for ref in refs:
                        source_kind = "context" if str(ref.get("from_context") or "").strip() else "upstream_tool"
                        if source_kind not in allowed_sources:
                            raise ToolDagContractViolation(
                                "tool_input_source_not_allowed",
                                f"{path}.inputs.{name}",
                                source_kind,
                            )
            for ref in _iter_input_refs(inputs):
                context_key = str(ref.get("from_context") or "").strip()
                upstream_id = str(ref.get("from_tool_task_id") or "").strip()
                if bool(context_key) == bool(upstream_id):
                    raise ToolDagContractViolation("tool_input_ref_requires_one_source", path, str(ref)[:500])
                if context_key and context_key not in available_context_keys:
                    raise ToolDagContractViolation("tool_input_context_key_not_available", path, context_key)
            # Required Tool result fields are compiled from the registered Tool
            # output schema. The LLM does not choose or rename them.
            declared_outputs = {
                str(item)
                for item in definition.output_schema.get("required_data_keys") or []
                if str(item).strip()
            }
            task = ToolDagTask(
                tool_task_id=task_id,
                tool_name=tool_name,
                objective=objective,
                args=dict(args),
                inputs=dict(inputs),
                expected_output_keys=sorted(declared_outputs),
                priority=max(0, min(10, int(row.get("priority", 1)))),
            )
            if task_id in signatures and task.to_dict() != signatures[task_id]:
                raise ToolDagContractViolation("tool_replan_modified_frozen_task", path, task_id)
            task_ids.append(task_id)
            tasks.append(task)
            definitions[task_id] = definition

        frozen_ids = set(signatures)
        known_current = set(task_ids)
        known = known_current | frozen_ids
        task_by_id = {task.tool_task_id: task for task in tasks}

        for index, task in enumerate(tasks):
            unknown_dependencies = sorted(set(dependencies_from_inputs(task.inputs)) - known)
            if unknown_dependencies:
                raise ToolDagContractViolation(
                    "tool_task_dependency_not_found",
                    f"$.tasks[{index}].inputs",
                    ",".join(unknown_dependencies),
                )
            consumer_definition = definitions[task.tool_task_id]
            for input_name, spec in task.inputs.items():
                consumer_contract = _input_contract(consumer_definition, input_name)
                for ref in _iter_input_refs(spec):
                    upstream_id = str(ref.get("from_tool_task_id") or "").strip()
                    if not upstream_id:
                        continue
                    output_slot = str(ref.get("output_slot") or "").strip()
                    if upstream_id in task_by_id:
                        producer_definition = definitions[upstream_id]
                    else:
                        signature = signatures.get(upstream_id) or {}
                        producer_definition = self.registry.get(str(signature.get("tool_name") or ""))
                    if producer_definition is None:
                        raise ToolDagContractViolation(
                            "tool_task_dependency_definition_not_found",
                            f"$.tasks[{index}].inputs.{input_name}",
                            upstream_id,
                        )
                    if output_slot:
                        producer_contract = _output_contract(producer_definition, output_slot)
                        if producer_contract is None:
                            raise ToolDagContractViolation(
                                "tool_output_slot_not_produced",
                                f"$.tasks[{index}].inputs.{input_name}",
                                f"{upstream_id}:{output_slot}",
                            )
                        if not _schema_compatible(producer_contract, consumer_contract):
                            raise ToolDagContractViolation(
                                "tool_slot_schema_mismatch",
                                f"$.tasks[{index}].inputs.{input_name}",
                                f"{producer_contract.schema_id}->{consumer_contract.schema_id}",
                            )
                        if not _artifact_contract_compatible(producer_contract, consumer_contract):
                            producer_descriptor = producer_contract.contract_descriptor()
                            consumer_descriptor = consumer_contract.contract_descriptor()
                            raise ToolDagContractViolation(
                                "tool_slot_artifact_contract_mismatch",
                                f"$.tasks[{index}].inputs.{input_name}",
                                (
                                    f"{producer_descriptor['contract']}@{producer_descriptor['version']}"
                                    "->"
                                    f"{consumer_descriptor['contract']}@{consumer_descriptor['version']}"
                                ),
                            )
                    else:
                        raise ToolDagContractViolation(
                            "tool_output_slot_required",
                            f"$.tasks[{index}].inputs.{input_name}",
                            upstream_id,
                        )

        final_ids = [str(item).strip() for item in finals if str(item or "").strip()]
        if len(final_ids) != len(set(final_ids)):
            raise ToolDagContractViolation("duplicate_final_tool_task_id", "$.final_output_task_ids")
        missing_finals = sorted(set(final_ids) - known)
        if missing_finals:
            raise ToolDagContractViolation("final_tool_task_not_found", "$.final_output_task_ids", ",".join(missing_finals))

        self._validate_acyclic(tasks, externally_satisfied=frozen_ids)

        return ToolDagPlan(
            worker_task_id=str(worker_task_id),
            worker_role=str(worker_role),
            goal_contract=dict(goal),
            tasks=tasks,
            final_output_task_ids=final_ids,
        )

    @staticmethod
    def _validate_acyclic(
        tasks: list[ToolDagTask],
        *,
        externally_satisfied: set[str] | None = None,
    ) -> None:
        by_id = {task.tool_task_id: task for task in tasks}
        remaining = set(by_id)
        completed: set[str] = set(externally_satisfied or set())
        while remaining:
            ready = [
                task_id
                for task_id in remaining
                if set(dependencies_from_inputs(by_id[task_id].inputs)).issubset(completed)
            ]
            if not ready:
                raise ToolDagContractViolation("tool_dag_cycle_or_stalled", "$.tasks", ",".join(sorted(remaining)))
            completed.update(ready)
            remaining.difference_update(ready)

    @staticmethod
    def _validate_contribution(
        tasks: list[ToolDagTask],
        final_ids: list[str],
        *,
        external_ids: set[str] | None = None,
    ) -> None:
        by_id = {task.tool_task_id: task for task in tasks}
        external = set(external_ids or set())
        needed = set(final_ids)
        stack = [task_id for task_id in final_ids if task_id in by_id]
        while stack:
            task_id = stack.pop()
            for dependency in dependencies_from_inputs(by_id[task_id].inputs):
                if dependency not in needed:
                    needed.add(dependency)
                    if dependency in by_id:
                        stack.append(dependency)
                elif dependency in by_id and dependency not in external:
                    # Already marked needed; no second traversal required.
                    pass
        unused = sorted(set(by_id) - needed)
        if unused:
            raise ToolDagContractViolation("tool_dag_task_has_no_final_contribution", "$.tasks", ",".join(unused))


__all__ = ["ToolDagValidator", "dependencies_from_inputs"]
