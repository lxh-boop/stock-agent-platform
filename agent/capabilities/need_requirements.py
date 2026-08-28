"""Compile Request Need semantics into Working-Memory data contracts.

MainAgent decides the Need and selected Worker. Runtime owns semantic validation,
parameter requirements and deterministic execution dependencies. Business data
is never bound point-to-point; successful outputs are written to the run
ContextBundle under simple data-name labels.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agent.console_trace import runtime_debug_event

from .data_names import data_name_matches_patterns
from .registry import CapabilityRegistry


def _violation(code: str, path: str, detail: str = ""):
    from agent.collaboration.worker_contracts import WorkerContractViolation
    return WorkerContractViolation(code, path, detail)


class NeedRequirementCompiler:
    SCHEMA_VERSION = "need-requirement.v3"
    NECESSITY_VALUES = {"required", "preferred", "optional"}

    def __init__(self, registry: CapabilityRegistry, worker_directory: Any) -> None:
        self.registry = registry
        self.worker_directory = worker_directory

    def normalize_requirement(
        self,
        raw: dict[str, Any],
        *,
        need_id: str,
        index: int,
    ) -> dict[str, Any]:
        semantic_key = str(raw.get("semantic_key") or "").strip()
        if not semantic_key or not self.registry.semantic_requirement_exists(semantic_key):
            raise _violation(
                "unknown_need_semantic_requirement",
                f"$.needs[{need_id}].requirements[{index}]",
                semantic_key,
            )
        registered = self.registry.semantic_requirement(semantic_key)
        kind = str(registered.get("kind") or "data").strip().lower()
        direction = str(raw.get("direction") or "input").strip().lower()
        if direction not in {"input", "output", "parameter"}:
            raise _violation(
                "invalid_need_requirement_direction",
                f"$.needs[{need_id}].requirements[{index}].direction",
                direction,
            )
        if kind == "parameter" and direction != "parameter":
            raise _violation(
                "parameter_semantic_must_be_parameter_direction",
                f"$.needs[{need_id}].requirements[{index}]",
                semantic_key,
            )
        if kind != "parameter" and direction == "parameter":
            raise _violation(
                "data_semantic_cannot_be_parameter_direction",
                f"$.needs[{need_id}].requirements[{index}]",
                semantic_key,
            )
        if kind == "context" and direction != "input":
            raise _violation(
                "runtime_context_semantic_must_be_input",
                f"$.needs[{need_id}].requirements[{index}]",
                semantic_key,
            )

        necessity = str(raw.get("necessity") or "").strip().lower()
        if not necessity:
            # Canonical defaults are semantic, not backward-compatibility aliases:
            # outputs and runtime identity are hard requirements; analytical data inputs
            # are enhancement inputs unless the planner explicitly marks them required.
            necessity = "required" if direction in {"output", "parameter"} or kind == "context" else "preferred"
        if necessity not in self.NECESSITY_VALUES:
            raise _violation(
                "invalid_need_requirement_necessity",
                f"$.needs[{need_id}].requirements[{index}].necessity",
                necessity,
            )
        if direction in {"output", "parameter"} and necessity == "optional":
            raise _violation(
                "output_or_parameter_cannot_be_optional",
                f"$.needs[{need_id}].requirements[{index}].necessity",
                necessity,
            )

        row: dict[str, Any] = {
            "requirement_id": f"{need_id}-R{index + 1:02d}",
            "semantic_key": semantic_key,
            "direction": direction,
            "kind": kind,
            "semantic_role": str(registered.get("semantic_role") or semantic_key),
            "source_policy": str(registered.get("source_policy") or "system"),
            "satisfaction_rule": str(registered.get("satisfaction_rule") or "exists"),
            "necessity": necessity,
            "required_paths": list(dict.fromkeys(
                str(item).strip()
                for item in raw.get("required_paths") or []
                if str(item).strip()
            )),
        }
        if kind == "parameter":
            row.update({
                "parameter_id": str(registered.get("parameter_id") or semantic_key),
                "satisfy_by": list(registered.get("satisfy_by") or []),
                "description": str(registered.get("description") or row["semantic_role"]),
                "expected_format": str(registered.get("expected_format") or ""),
            })
        elif kind == "context":
            row["context_name"] = str(registered.get("context_name") or semantic_key)
        else:
            row["data_name"] = str(registered.get("data_name") or semantic_key)
        return row

    def normalize_need_requirements(
        self,
        *,
        need_id: str,
        raw_requirements: list[dict[str, Any]] | None,
        strict: bool,
    ) -> list[dict[str, Any]]:
        rows = [dict(item) for item in raw_requirements or [] if isinstance(item, dict)]
        if strict and not rows:
            raise _violation("request_need_requirements_required", f"$.needs[{need_id}].requirements")
        normalized = [
            self.normalize_requirement(row, need_id=need_id, index=index)
            for index, row in enumerate(rows)
        ]
        if strict and not any(
            item["direction"] == "output" and item.get("necessity") == "required"
            for item in normalized
        ):
            raise _violation("request_need_output_requirement_required", f"$.needs[{need_id}].requirements")
        return normalized

    @staticmethod
    def requirement_index(request_need_contract: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
        index: dict[str, tuple[str, dict[str, Any]]] = {}
        for need in request_need_contract.get("needs") or []:
            need_id = str(need.get("need_id") or "")
            for req in need.get("requirements") or []:
                requirement_id = str(req.get("requirement_id") or "")
                if requirement_id:
                    index[requirement_id] = (need_id, dict(req))
        return index

    @staticmethod
    def output_data_by_need(request_need_contract: dict[str, Any]) -> dict[str, set[str]]:
        result: dict[str, set[str]] = defaultdict(set)
        for need in request_need_contract.get("needs") or []:
            need_id = str(need.get("need_id") or "")
            for req in need.get("requirements") or []:
                if (
                    req.get("direction") == "output"
                    and req.get("kind") == "data"
                    and req.get("data_name")
                    and req.get("necessity") == "required"
                ):
                    result[need_id].add(str(req["data_name"]))
        return dict(result)

    def validate_worker_call_need_outputs(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_calls: list[dict[str, Any]],
    ) -> None:
        outputs_by_need = self.output_data_by_need(request_need_contract)
        for need in request_need_contract.get("needs") or []:
            if not bool(need.get("required", True)):
                continue
            need_id = str(need.get("need_id") or "")
            required_outputs = outputs_by_need.get(need_id, set())
            if not required_outputs:
                continue
            realized = {
                str(name)
                for call in worker_calls
                if need_id in {str(item) for item in call.get("covers_need_ids") or []}
                for name in call.get("desired_output_data_names") or []
                if str(name)
            }
            missing = sorted(required_outputs - realized)
            if missing:
                raise _violation(
                    "need_required_output_not_covered_by_worker_calls",
                    "$.worker_calls",
                    f"{need_id}:{','.join(missing)}",
                )

    @staticmethod
    def _data_requirement_from_semantic(req: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": str(req.get("data_name") or ""),
            "semantic_role": str(req.get("semantic_role") or req.get("semantic_key") or ""),
            "source_policy": str(req.get("source_policy") or "system"),
            "satisfaction_rule": str(req.get("satisfaction_rule") or "exists"),
            "required": str(req.get("necessity") or "required") == "required",
            "required_paths": list(req.get("required_paths") or []),
        }

    @staticmethod
    def _parameter_requirement_from_semantic(req: dict[str, Any]) -> dict[str, Any]:
        return {
            "parameter_id": str(req.get("parameter_id") or req.get("semantic_key") or ""),
            "semantic_role": str(req.get("semantic_role") or req.get("semantic_key") or ""),
            "source_policy": str(req.get("source_policy") or "user"),
            "satisfaction_rule": str(req.get("satisfaction_rule") or "one_of"),
            "required": str(req.get("necessity") or "required") == "required",
            "satisfy_by": list(req.get("satisfy_by") or []),
            "description": str(req.get("description") or req.get("semantic_role") or ""),
            "expected_format": str(req.get("expected_format") or ""),
        }

    def compile_task_requirements(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Assign Need input/parameter semantics to selected Worker calls."""
        calls = [dict(call) for call in worker_calls or [] if isinstance(call, dict)]
        if not calls:
            raise _violation("worker_calls_required", "$.worker_calls")

        call_by_id: dict[str, dict[str, Any]] = {}
        card_by_call: dict[str, Any] = {}
        scope_by_call: dict[str, dict[str, Any]] = {}
        for index, call in enumerate(calls):
            call_id = str(call.get("call_id") or f"WC{index + 1:02d}").strip()
            worker_id = str(call.get("worker_id") or "").strip().upper()
            if not call_id or call_id in call_by_id:
                raise _violation("duplicate_worker_call_id", "$.worker_calls", call_id)
            card = self.worker_directory.get(worker_id)
            call_by_id[call_id] = call
            card_by_call[call_id] = card
            scope_by_call[call_id] = self.registry.aggregate_scope(card.supported_boundary_ids)

        req_index = self.requirement_index(request_need_contract)
        outputs_by_need = self.output_data_by_need(request_need_contract)
        assignments = {
            call_id: {"call_id": call_id, "requirement_ids": [], "additional_required_data": []}
            for call_id in call_by_id
        }

        def call_need_ids(call: dict[str, Any]) -> set[str]:
            return {str(item) for item in call.get("covers_need_ids") or [] if str(item)}

        owner_calls_by_need: dict[str, list[str]] = {}
        for need_id, required_outputs in outputs_by_need.items():
            owners: list[str] = []
            for call_id, call in call_by_id.items():
                if need_id not in call_need_ids(call):
                    continue
                desired = {str(item) for item in call.get("desired_output_data_names") or [] if str(item)}
                if required_outputs.intersection(desired):
                    owners.append(call_id)
            if required_outputs and not owners:
                raise _violation(
                    "need_required_output_has_no_owner_worker",
                    "$.worker_calls",
                    f"{need_id}:{','.join(sorted(required_outputs))}",
                )
            owner_calls_by_need[need_id] = owners

        assigned_required: set[str] = set()
        for requirement_id, (need_id, req) in req_index.items():
            if req.get("direction") not in {"input", "parameter"}:
                continue
            owners = list(owner_calls_by_need.get(need_id) or []) or [
                call_id for call_id, call in call_by_id.items() if need_id in call_need_ids(call)
            ]
            if req.get("kind") == "context":
                # GraphRefs/runtime state are task/runtime context, never business data.
                if str(req.get("necessity") or "required") == "required":
                    assigned_required.add(requirement_id)
                continue
            if req.get("direction") == "parameter":
                eligible = []
                keys = [str(req.get("parameter_id") or ""), *[str(x) for x in req.get("satisfy_by") or []]]
                for call_id in owners:
                    patterns = scope_by_call[call_id].get("accepted_business_parameter_patterns") or []
                    if any(data_name_matches_patterns(key, patterns) for key in keys if key):
                        eligible.append(call_id)
            else:
                # Working-Memory consumers receive the whole relevant context.
                # They do not declare point-to-point required business-data names.
                eligible = [
                    call_id for call_id in owners
                    if str(getattr(card_by_call[call_id], "working_memory_mode", "none")) == "consumer"
                ]
                if not eligible:
                    data_name = str(req.get("data_name") or "")
                    eligible = [
                        call_id for call_id in owners
                        if data_name_matches_patterns(
                            data_name, scope_by_call[call_id].get("accepted_data_patterns") or []
                        )
                    ]
            if not eligible:
                if str(req.get("necessity") or "required") == "required":
                    raise _violation(
                        "need_requirement_has_no_consumer_worker",
                        "$.worker_calls",
                        f"{requirement_id}:{req.get('semantic_key') or ''}",
                    )
                continue
            for call_id in eligible:
                assignments[call_id]["requirement_ids"].append(requirement_id)
            if str(req.get("necessity") or "required") == "required":
                assigned_required.add(requirement_id)

        required_ids = {
            req_id for req_id, (_, req) in req_index.items()
            if str(req.get("necessity") or "required") == "required" and req.get("direction") in {"input", "parameter"}
        }
        missing = sorted(required_ids - assigned_required)
        if missing:
            raise _violation("required_need_requirement_unassigned", "$.worker_calls", ",".join(missing))

        result: list[dict[str, Any]] = []
        for call_id in call_by_id:
            row = assignments[call_id]
            row["requirement_ids"] = list(dict.fromkeys(row["requirement_ids"]))
            result.append(row)
        runtime_debug_event(
            "NEED_REQUIREMENTS_COMPILED",
            {
                "requirement_contract_version": self.SCHEMA_VERSION,
                "requirements": [
                    {
                        "requirement_id": req_id,
                        "need_id": need_id,
                        "semantic_key": req.get("semantic_key"),
                        "direction": req.get("direction"),
                        "kind": req.get("kind"),
                        "data_name": req.get("data_name"),
                        "context_name": req.get("context_name"),
                        "necessity": req.get("necessity"),
                    }
                    for req_id, (need_id, req) in req_index.items()
                ],
                "worker_calls": calls,
                "assignments": result,
                "runtime_context_requirements": [
                    req_id for req_id, (_, req) in req_index.items() if req.get("kind") == "context"
                ],
            },
        )
        return result

    def expand_compact_tasks(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_calls: list[dict[str, Any]],
        task_requirements: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        call_by_id = {str(call.get("call_id") or ""): dict(call) for call in worker_calls}
        if not call_by_id:
            raise _violation("worker_calls_required", "$.worker_calls")
        assignment_by_call: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(task_requirements or []):
            if not isinstance(raw, dict):
                raise _violation("compact_task_requirement_not_object", f"$.task_requirements[{index}]")
            call_id = str(raw.get("call_id") or "").strip()
            if call_id not in call_by_id:
                raise _violation("compact_task_unknown_call", f"$.task_requirements[{index}].call_id", call_id)
            if call_id in assignment_by_call:
                raise _violation("compact_task_duplicate_call", "$.task_requirements", call_id)
            assignment_by_call[call_id] = dict(raw)
        missing_calls = sorted(set(call_by_id) - set(assignment_by_call))
        if missing_calls:
            raise _violation("compact_task_assignment_missing_call", "$.task_requirements", ",".join(missing_calls))

        req_index = self.requirement_index(request_need_contract)
        tasks: list[dict[str, Any]] = []
        assigned_requirements: set[str] = set()
        for call_id, call in call_by_id.items():
            assignment = assignment_by_call[call_id]
            worker_id = str(call.get("worker_id") or "").strip().upper()
            card = self.worker_directory.get(worker_id)
            scope = self.registry.aggregate_scope(card.supported_boundary_ids)
            call_need_ids = {str(item) for item in call.get("covers_need_ids") or [] if str(item)}

            data_rows: list[dict[str, Any]] = []
            parameter_rows: list[dict[str, Any]] = []
            seen_data: set[str] = set()
            seen_parameters: set[str] = set()
            for requirement_id in assignment.get("requirement_ids") or []:
                requirement_id = str(requirement_id)
                if requirement_id not in req_index:
                    raise _violation("compact_task_unknown_requirement", f"$.task_requirements[{call_id}]", requirement_id)
                need_id, req = req_index[requirement_id]
                if need_id not in call_need_ids:
                    raise _violation("compact_requirement_assigned_outside_need_coverage", f"$.task_requirements[{call_id}]", requirement_id)
                if req.get("direction") == "output":
                    raise _violation("compact_output_requirement_cannot_be_input_assignment", f"$.task_requirements[{call_id}]", requirement_id)
                if req.get("kind") == "context":
                    assigned_requirements.add(requirement_id)
                    continue
                if req.get("direction") == "parameter":
                    parameter_id = str(req.get("parameter_id") or "")
                    if parameter_id and parameter_id not in seen_parameters:
                        parameter_rows.append(self._parameter_requirement_from_semantic(req))
                        seen_parameters.add(parameter_id)
                    assigned_requirements.add(requirement_id)
                    continue
                # Analysis/decision/write consumers inspect the relevant run
                # Working Memory as a whole. Do not turn semantic inputs into
                # point-to-point transport requirements.
                if str(getattr(card, "working_memory_mode", "none")) == "consumer":
                    assigned_requirements.add(requirement_id)
                    continue
                data_name = str(req.get("data_name") or "")
                if data_name and data_name not in seen_data:
                    data_rows.append(self._data_requirement_from_semantic(req))
                    seen_data.add(data_name)
                assigned_requirements.add(requirement_id)

            desired_outputs = [
                str(item) for item in call.get("desired_output_data_names") or [] if str(item)
            ]
            unsupported_outputs = sorted(
                name for name in desired_outputs
                if not data_name_matches_patterns(name, scope.get("produced_data_patterns") or [])
            )
            if unsupported_outputs:
                raise _violation(
                    "compiled_need_output_outside_worker_scope",
                    f"$.task_requirements[{call_id}]",
                    ",".join(unsupported_outputs),
                )
            tasks.append({
                "worker_id": worker_id,
                "objective": str(call.get("objective") or "").strip(),
                "priority": 1,
                "business_parameters": {},
                "contracts": [{
                    "description": str(call.get("objective") or "").strip(),
                    "required_data": data_rows,
                    "required_parameters": parameter_rows,
                    "promised_data": [
                        {"name": name, "required_paths": []} for name in desired_outputs
                    ],
                    "acceptance_rule_ids": list(scope.get("allowed_acceptance_rule_ids") or []),
                    "forbidden_data_names": [],
                    "criticality": "required",
                    "mutation_allowed": bool(scope.get("mutation_allowed", False)),
                }],
            })

        required_assignments = {
            req_id for req_id, (_, req) in req_index.items()
            if (
                str(req.get("necessity") or "required") == "required"
                and req.get("direction") in {"input", "parameter"}
                and req.get("kind") != "context"
            )
        }
        missing = sorted(required_assignments - assigned_requirements)
        runtime_debug_event(
            "WORKER_CALL_REQUIREMENTS_BOUND",
            {
                "task_requirements": task_requirements,
                "assigned_requirement_ids": sorted(assigned_requirements),
                "required_task_requirement_ids": sorted(required_assignments),
                "missing_required_task_requirement_ids": missing,
                "compact_tasks": tasks,
            },
            level="ERROR" if missing else "DEBUG",
        )
        if missing:
            raise _violation("required_need_requirement_unassigned", "$.task_requirements", ",".join(missing))
        return tasks


__all__ = ["NeedRequirementCompiler"]
