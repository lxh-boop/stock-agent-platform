"""Upfront MainAgent Worker-DAG planner.

Normal execution has one bounded planning phase before any Worker executes:

1. One merged LLM stage creates Need[] and binds every Need to Worker or reuse.
2. Runtime validates registered semantic requirements and Worker public contracts.
3. Runtime deterministically merges Worker calls and compiles the complete Worker DAG.
4. Each selected Worker plans its own private Tool DAG when applicable.

The raw user request is not re-interpreted by Worker selection or Worker-DAG compilation. Replan remains an exception-recovery path and reuses the original
request_need_contract.
"""

from __future__ import annotations

from typing import Any

from core.llm import LLMService
from core.llm.prompt_compaction import compact_json_dumps

from agent.console_trace import flow_event, runtime_debug_event
from agent.runtime_version import RUNTIME_VERSION
from agent.capabilities import (
    CapabilityPlanValidator,
    CapabilityRegistry,
    CapabilityTask,
    NeedRequirementCompiler,
    TaskDependencyCompiler,
    WorkerAssignmentValidator,
)
from agent.capabilities.data_names import data_name_matches_patterns

from .models import GraphAgentTask, GraphWorkerResult, ResultStatus
from .worker_catalog import WorkerDescriptionCatalog
from .worker_contracts import WorkerContractViolation


class CoordinatorPlanningError(RuntimeError):
    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


class CoordinatorPlanner:
    """Decompose a normalized Request into Need[], then select Workers and compile the DAG."""

    def __init__(
        self,
        directory: Any,
        *,
        llm_service: LLMService,
        worker_tool_directory: Any | None = None,
    ) -> None:
        self.directory = directory
        self.llm_service = llm_service
        self.registry = CapabilityRegistry()
        self.worker_catalog = WorkerDescriptionCatalog(
            directory,
            self.registry,
            worker_tool_directory=worker_tool_directory,
        )
        self.validator = CapabilityPlanValidator(self.registry, directory)
        self.need_compiler = NeedRequirementCompiler(self.registry, directory)
        self.dependency_compiler = TaskDependencyCompiler(directory)
        self.assignment_validator = WorkerAssignmentValidator(self.registry, directory)

    @staticmethod
    def _initial_context_names(
        *,
        focus_refs: list[Any],
        context_refs: list[Any],
        memory_summary: str,
        extra_context_names: set[str] | None = None,
    ) -> set[str]:
        context_names = {
            "current_user_request",
            "user_identity",
            "permission_context",
            "reply_language",
            "as_of_time",
            "runtime_context",
            "business_parameters",
        }
        all_refs = [*list(focus_refs or []), *list(context_refs or [])]
        if focus_refs:
            context_names.add("authoritative_entity_refs")
        if context_refs:
            context_names.add("context_entity_refs")
        source_roles = {"source", "cause", "event", "relation_source"}
        target_roles = {"target", "impact_target", "portfolio", "holding", "relation_target"}
        if any(str(getattr(ref, "role", "") or "") in source_roles for ref in all_refs):
            context_names.add("source_entity_refs")
        if any(str(getattr(ref, "role", "") or "") in target_roles for ref in all_refs):
            context_names.add("target_entity_refs")
        if str(memory_summary or "").strip():
            context_names.add("session_summary")
        context_names.update(str(item) for item in set(extra_context_names or set()) if str(item))
        return context_names

    @staticmethod
    def _normalize_need_id(index: int) -> str:
        return f"N{index:02d}"

    def _load_worker_descriptions(self, *, effect_limit: str, run_id: str) -> list[dict[str, Any]]:
        descriptions = self.worker_catalog.descriptions(effect_limit=effect_limit)
        if not descriptions:
            raise WorkerContractViolation("worker_description_catalog_empty", "$.worker_descriptions")
        flow_event(
            "WORKER_DESCRIPTION_CATALOG_LOADED",
            {
                "worker_count": len(descriptions),
                "worker_ids": [row["worker_id"] for row in descriptions],
                "visibility": "all_public_descriptions_upfront",
                "private_tool_visibility": "none",
            },
            run_id=run_id,
        )
        return descriptions

    @staticmethod
    def _worker_output_patterns(worker: dict[str, Any]) -> list[str]:
        direct = [
            str(pattern)
            for pattern in worker.get("produced_data_patterns") or []
            if str(pattern)
        ]
        if direct:
            return list(dict.fromkeys(direct))
        # Compatibility for older tests/snapshots. The active MainAgent catalog
        # no longer exposes fine-grained boundaries.
        return list(dict.fromkeys(
            str(pattern)
            for boundary in worker.get("supported_boundaries") or []
            for pattern in boundary.get("produced_data_patterns") or []
            if str(pattern)
        ))

    @classmethod
    def _worker_supports_output(cls, worker: dict[str, Any], data_name: str) -> bool:
        if not data_name_matches_patterns(data_name, cls._worker_output_patterns(worker)):
            return False
        if str(worker.get("output_publication_mode") or "worker_synthesized") == "private_tool_passthrough":
            discoverable = {
                str(item)
                for item in worker.get("private_tool_semantic_outputs") or []
                if str(item)
            }
            return data_name in discoverable
        return True

    @classmethod
    def _worker_output_contract_error_detail(
        cls,
        worker: dict[str, Any],
        invalid_data_names: set[str] | list[str],
    ) -> str:
        """Return repair-ready details without closing the open 数据名称 namespace.

        Worker-synthesized business-data names remain open-ended.  The hard boundary is the
        Worker's declared ``produced_data_patterns`` namespace.  Keeping this
        detail machine-readable lets the existing single targeted-repair call
        rename an invalid semantic key instead of merely repairing JSON shape.
        """

        mode = str(worker.get("output_publication_mode") or "worker_synthesized")
        detail: dict[str, Any] = {
            "worker_id": str(worker.get("worker_id") or ""),
            "invalid_data_names": sorted({str(item) for item in invalid_data_names if str(item)}),
            "output_publication_mode": mode,
            "produced_data_patterns": cls._worker_output_patterns(worker),
            "output_data_examples": [
                str(item)
                for item in worker.get("output_data_examples") or []
                if str(item)
            ],
        }
        if mode == "private_tool_passthrough":
            detail["private_tool_semantic_outputs"] = [
                str(item)
                for item in worker.get("private_tool_semantic_outputs") or []
                if str(item)
            ]
            detail["repair_rule"] = (
                "Select an existing private_tool_semantic_outputs key; do not synthesize a new business-data name."
            )
        else:
            detail["repair_rule"] = (
                "Reuse an output_data_examples key when suitable, otherwise rename/create a semantic data name "
                "that literally matches at least one produced_data_patterns entry."
            )
        return compact_json_dumps(detail)

    def _select_recovery_worker_calls(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_descriptions: list[dict[str, Any]],
        effect_limit: str,
        run_id: str,
        initial_context_names: set[str],
        recovery_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Recovery-only LLM Worker selection for unresolved Needs.

        The LLM receives the real failed Worker results and decides the next
        Worker route. It does not emit Runtime business-data names; Runtime
        derives those deterministically from the immutable Need semantics.
        """

        worker_by_id = {str(row["worker_id"]): row for row in worker_descriptions}
        all_need_ids = {
            str(row.get("need_id") or "")
            for row in request_need_contract.get("needs") or []
            if str(row.get("need_id") or "")
        }
        unresolved_need_ids = {
            str(item)
            for item in recovery_context.get("unresolved_need_ids") or []
            if str(item)
        }
        if not unresolved_need_ids:
            unresolved_need_ids = {
                str(row.get("need_id") or "")
                for row in request_need_contract.get("needs") or []
                if bool(row.get("required", True)) and str(row.get("need_id") or "")
            }
        unresolved_need_ids &= all_need_ids
        recovery_need_contract = {
            **dict(request_need_contract),
            "needs": [
                dict(row)
                for row in request_need_contract.get("needs") or []
                if str(row.get("need_id") or "") in unresolved_need_ids
            ],
        }
        required_need_ids = {
            str(row.get("need_id") or "")
            for row in recovery_need_contract.get("needs") or []
            if bool(row.get("required", True)) and str(row.get("need_id") or "")
        }
        outputs_by_need = self.need_compiler.output_data_by_need(recovery_need_contract)

        def semantic_need_view(contract: dict[str, Any]) -> dict[str, Any]:
            return {
                "schema_version": str(contract.get("schema_version") or ""),
                "request_id": str(contract.get("request_id") or ""),
                "request_objective": str(contract.get("request_objective") or ""),
                "authoritative_target": dict(contract.get("authoritative_target") or {}),
                "effect_limit": str(contract.get("effect_limit") or "read"),
                "needs": [
                    {
                        "need_id": str(need.get("need_id") or ""),
                        "description": str(need.get("description") or ""),
                        "required": bool(need.get("required", True)),
                        "requirements": [
                            {
                                "semantic_key": str(req.get("semantic_key") or ""),
                                "direction": str(req.get("direction") or ""),
                                "kind": str(req.get("kind") or ""),
                                "semantic_role": str(req.get("semantic_role") or ""),
                                "necessity": str(req.get("necessity") or "required"),
                            }
                            for req in need.get("requirements") or []
                            if isinstance(req, dict)
                        ],
                    }
                    for need in contract.get("needs") or []
                    if isinstance(need, dict)
                ],
            }

        semantic_catalog = self.registry.semantic_requirement_catalog()
        planner_worker_descriptions: list[dict[str, Any]] = []
        for worker in worker_descriptions:
            supported_output_semantics = [
                str(item.get("semantic_key") or "")
                for item in semantic_catalog
                if item.get("kind") == "data"
                and str(item.get("data_name") or "")
                and self._worker_supports_output(worker, str(item.get("data_name") or ""))
            ]
            planner_worker_descriptions.append({
                "worker_id": str(worker.get("worker_id") or ""),
                "public_role": str(worker.get("public_role") or ""),
                "short_description": str(worker.get("short_description") or ""),
                "delegation_description": str(worker.get("delegation_description") or ""),
                "delegate_when": list(worker.get("delegate_when") or []),
                "supported_scenarios": list(worker.get("supported_scenarios") or []),
                "unsupported_scenarios": list(worker.get("unsupported_scenarios") or []),
                "limitations": list(worker.get("limitations") or []),
                "supports_output_semantics": list(dict.fromkeys(x for x in supported_output_semantics if x)),
            })

        def validate_llm_payload(payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                raise WorkerContractViolation("recovery_worker_calls_not_object", "$")
            unexpected = sorted(key for key in payload if key not in {"worker_calls", "selection_reason"})
            if unexpected:
                raise WorkerContractViolation("recovery_worker_calls_unexpected_field", "$", ",".join(unexpected))
            calls = payload.get("worker_calls")
            if not isinstance(calls, list) or not calls:
                raise WorkerContractViolation("worker_calls_required", "$.worker_calls")
            covered: set[str] = set()
            for index, raw in enumerate(calls):
                if not isinstance(raw, dict):
                    raise WorkerContractViolation("worker_call_not_object", f"$.worker_calls[{index}]")
                unexpected_call = sorted(
                    key for key in raw if key not in {"worker_id", "objective", "covers_need_ids"}
                )
                if unexpected_call:
                    raise WorkerContractViolation(
                        "recovery_worker_call_unexpected_field",
                        f"$.worker_calls[{index}]",
                        ",".join(unexpected_call),
                    )
                worker_id = str(raw.get("worker_id") or "").strip().upper()
                if worker_id not in worker_by_id:
                    raise WorkerContractViolation("unknown_worker_call", f"$.worker_calls[{index}].worker_id", worker_id)
                if not str(raw.get("objective") or "").strip():
                    raise WorkerContractViolation("worker_call_objective_required", f"$.worker_calls[{index}].objective")
                need_ids = {str(item) for item in raw.get("covers_need_ids") or [] if str(item)}
                if not need_ids:
                    raise WorkerContractViolation("worker_call_need_coverage_required", f"$.worker_calls[{index}].covers_need_ids")
                unknown = need_ids - unresolved_need_ids
                if unknown:
                    raise WorkerContractViolation(
                        "worker_call_outside_unresolved_need",
                        f"$.worker_calls[{index}].covers_need_ids",
                        ",".join(sorted(unknown)),
                    )
                covered.update(need_ids)
            missing = sorted(required_need_ids - covered)
            if missing:
                raise WorkerContractViolation("required_request_need_uncovered", "$.worker_calls", ",".join(missing))

        payload = self.llm_service.generate_json(
            stage="recovery_worker_call_selection",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你只负责Worker失败后的Recovery重新规划。原始Request/Need语义不可修改。"
                        "你会看到真实failed_worker_results、历史recovery_error_history、已经冻结成功的Need以及本轮unresolved_needs。"
                        "根据这些错误结果重新决定下一轮应由哪些Worker处理未完成Need；可以继续使用原Worker，也可以改选其他合法Worker。"
                        "不要选择Tool，不要生成DAG，不要输出任何Runtime内部数据名称字段。"
                        "worker_call只允许worker_id、objective、covers_need_ids；只覆盖unresolved_needs，不得重新覆盖frozen_completed_need_ids。"
                        "最多由Coordinator调用两轮Recovery；每轮都应利用之前错误避免机械重复。只输出JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "original_request_need_contract": semantic_need_view(request_need_contract),
                        "unresolved_need_contract": semantic_need_view(recovery_need_contract),
                        "available_initial_context_names": sorted(initial_context_names),
                        "worker_descriptions": planner_worker_descriptions,
                        "bounded_recovery_context": recovery_context,
                        "required_output_shape": {
                            "worker_calls": [{
                                "worker_id": "Wxx from worker_descriptions",
                                "objective": "本轮修复目标",
                                "covers_need_ids": ["only unresolved Need IDs"],
                            }],
                            "selection_reason": "基于真实错误结果说明为什么重新选择这些Worker",
                        },
                    }),
                },
            ],
            max_output_tokens=1800,
            validator=validate_llm_payload,
            operation=f"recovery_worker_calls:{effect_limit}",
            disable_thinking=False,
            repair_mode="targeted",
            repair_guidance=(
                "只修复Recovery Worker覆盖关系与合法Worker ID；只覆盖unresolved_needs；"
                "不得修改原Request/Need，不得选择Tool，不得输出Runtime内部数据名。"
            ),
        )

        calls: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.get("worker_calls") or [], start=1):
            row = dict(raw or {})
            need_ids = list(dict.fromkeys(str(item) for item in row.get("covers_need_ids") or [] if str(item)))
            desired_names = sorted({
                name
                for need_id in need_ids
                for name in outputs_by_need.get(need_id, set())
                if str(name)
            })
            worker_id = str(row.get("worker_id") or "").strip().upper()
            unsupported = {
                name for name in desired_names
                if not self._worker_supports_output(worker_by_id[worker_id], name)
            }
            if unsupported:
                raise WorkerContractViolation(
                    "worker_call_output_outside_worker",
                    f"$.worker_calls[{index - 1}]",
                    self._worker_output_contract_error_detail(worker_by_id[worker_id], unsupported),
                )
            calls.append({
                "call_id": f"RWC{index:02d}",
                "worker_id": worker_id,
                "objective": str(row.get("objective") or "").strip(),
                "covers_need_ids": need_ids,
                "desired_output_data_names": desired_names,
            })

        normalized = {"worker_calls": calls, "selection_reason": str(payload.get("selection_reason") or "").strip()}
        validate_llm_payload({
            "worker_calls": [
                {
                    "worker_id": row["worker_id"],
                    "objective": row["objective"],
                    "covers_need_ids": row["covers_need_ids"],
                }
                for row in calls
            ],
            "selection_reason": normalized["selection_reason"],
        })
        self.need_compiler.validate_worker_call_need_outputs(
            request_need_contract=recovery_need_contract,
            worker_calls=calls,
        )
        flow_event(
            "RECOVERY_WORKER_CALLS_SELECTED",
            {
                "worker_call_count": len(calls),
                "worker_ids": [row["worker_id"] for row in calls],
                "need_coverage": {row["call_id"]: row["covers_need_ids"] for row in calls},
                "unresolved_need_ids": sorted(unresolved_need_ids),
                "runtime_derived_output_names": True,
            },
            run_id=run_id,
        )
        return normalized

    def _plan_needs_with_bindings(
        self,
        *,
        query: str,
        effect_limit: str,
        run_id: str,
        language: str,
        initial_context_names: set[str],
        memory_summary: str,
        worker_descriptions: list[dict[str, Any]],
        context_binding: dict[str, Any] | None = None,
        request_id: str = "",
        authoritative_target: dict[str, Any] | None = None,
        request_constraints: list[str] | None = None,
        reuse_candidates: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """合并规划：一次 LLM 调用同时生成 Need 清单并为每个 Need 声明 binding。

        每个 Need 生成时直接二选一声明：
        - worker_binding：走计算路线（只选 Worker + 调用目标；产出数据名由注册语义合同编译）
        - reuse_binding：走复用路线（引用候选轮次显示编号 + 可选派生参数，不声明产出名）

        返回 (full_need_contract, worker_need_contract, worker_call_plan, reuse_decisions)：
        - full_need_contract：全部 Need（含复用满足的），供审计与静态节点生成
        - worker_need_contract：仅计算路线的 Need，供 Worker DAG 确定性编译
        - worker_call_plan：从 worker_binding 确定性归并出的 Worker 调用计划
        - reuse_decisions：复用决策清单（need_id/turn_id/derive_args）
        """

        worker_by_id = {str(row["worker_id"]): row for row in worker_descriptions}
        binding_context = dict(context_binding or {})
        freshness = str(binding_context.get("freshness_expectation") or "unspecified")
        # 允许的复用引用 = 规则层 1 精筛后实际注入的候选（LLM 只能引用看得见的）
        candidate_by_turn = {
            str(item.get("turn_id") or ""): dict(item)
            for item in list(reuse_candidates or [])
            if str(item.get("turn_id") or "").strip()
        }
        allowed_turn_ids = set(candidate_by_turn)

        def validate(payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                raise WorkerContractViolation("merged_planning_not_object", "$")
            raw_needs = payload.get("needs")
            if not isinstance(raw_needs, list) or not raw_needs:
                raise WorkerContractViolation("request_needs_required", "$.needs")
            proposal_output_seen = False
            for index, row in enumerate(raw_needs):
                path = f"$.needs[{index}]"
                if not isinstance(row, dict) or not str(row.get("description") or "").strip():
                    raise WorkerContractViolation("request_need_description_required", path)
                normalized = self.need_compiler.normalize_need_requirements(
                    need_id=f"N{index + 1:02d}",
                    raw_requirements=row.get("requirements") or [],
                    strict=True,
                )
                # binding 二选一必填：不能都空（悬空），不能都填（路线冲突）
                binding = row.get("binding") if isinstance(row.get("binding"), dict) else {}
                worker_binding = binding.get("worker_binding") if isinstance(binding.get("worker_binding"), dict) else None
                reuse_binding = binding.get("reuse_binding") if isinstance(binding.get("reuse_binding"), dict) else None
                if bool(worker_binding) == bool(reuse_binding):
                    raise WorkerContractViolation("need_binding_exactly_one_required", f"{path}.binding")
                if worker_binding:
                    unexpected_worker_binding = sorted(
                        key for key in worker_binding if key not in {"worker_id", "objective"}
                    )
                    if unexpected_worker_binding:
                        raise WorkerContractViolation(
                            "worker_binding_unexpected_fields",
                            f"{path}.binding.worker_binding",
                            ",".join(unexpected_worker_binding),
                        )
                    worker_id = str(worker_binding.get("worker_id") or "").strip().upper()
                    if worker_id not in worker_by_id:
                        raise WorkerContractViolation("unknown_worker_call", f"{path}.binding.worker_binding.worker_id", worker_id)
                    required_output_names = {
                        str(requirement.get("data_name") or "")
                        for requirement in normalized
                        if requirement.get("direction") == "output"
                        and requirement.get("kind") == "data"
                        and str(requirement.get("data_name") or "")
                    }
                    unsupported = {
                        name for name in required_output_names
                        if not self._worker_supports_output(worker_by_id[worker_id], name)
                    }
                    if unsupported:
                        raise WorkerContractViolation(
                            "worker_call_output_outside_worker",
                            f"{path}.binding.worker_binding.worker_id",
                            self._worker_output_contract_error_detail(worker_by_id[worker_id], unsupported),
                        )
                    for requirement in normalized:
                        if requirement.get("direction") != "output":
                            continue
                        if str(requirement.get("data_name") or "") in {"proposal", "rebalance"}:
                            proposal_output_seen = True
                if reuse_binding:
                    turn_id = str(reuse_binding.get("turn_id") or "").strip()
                    if turn_id not in allowed_turn_ids:
                        # 复用引用必须来自注入候选（防幻觉）
                        raise WorkerContractViolation("reuse_binding_unknown_turn", f"{path}.binding.reuse_binding.turn_id", turn_id)
                    candidate = dict(candidate_by_turn.get(turn_id) or {})
                    if effect_limit == "proposal":
                        # Proposal 请求必须真实计算产出方案，禁止复用
                        raise WorkerContractViolation("reuse_binding_forbidden_for_proposal", f"{path}.binding.reuse_binding")
                    if freshness == "latest" and str(candidate.get("freshness") or "") != "current_trade_day":
                        raise WorkerContractViolation("reuse_binding_stale_for_latest", f"{path}.binding.reuse_binding", turn_id)
                    required_outputs = {
                        str(req.get("data_name") or "")
                        for req in normalized
                        if req.get("direction") == "output"
                        and req.get("kind") == "data"
                        and req.get("necessity") == "required"
                        and str(req.get("data_name") or "")
                    }
                    reusable_outputs = {str(name) for name in candidate.get("reusable_outputs") or [] if str(name)}
                    if required_outputs and not required_outputs.issubset(reusable_outputs):
                        raise WorkerContractViolation(
                            "reuse_binding_output_not_materialized",
                            f"{path}.binding.reuse_binding",
                            f"turn={turn_id};missing={','.join(sorted(required_outputs - reusable_outputs))}",
                        )
            if effect_limit == "proposal" and not proposal_output_seen:
                raise WorkerContractViolation(
                    "proposal_request_missing_proposal_output_need",
                    "$.needs",
                    "proposal-capable READ Request must contain a Need whose output is a proposal/rebalance result",
                )

        semantic_catalog = self.registry.semantic_requirement_catalog()
        planner_worker_descriptions: list[dict[str, Any]] = []
        for worker in worker_descriptions:
            supported_output_semantics = [
                str(item.get("semantic_key") or "")
                for item in semantic_catalog
                if item.get("kind") == "data"
                and str(item.get("data_name") or "")
                and self._worker_supports_output(worker, str(item.get("data_name") or ""))
            ]
            planner_worker_descriptions.append({
                "worker_id": str(worker.get("worker_id") or ""),
                "public_role": str(worker.get("public_role") or ""),
                "short_description": str(worker.get("short_description") or ""),
                "delegation_description": str(worker.get("delegation_description") or ""),
                "delegate_when": list(worker.get("delegate_when") or []),
                "supported_scenarios": list(worker.get("supported_scenarios") or []),
                "unsupported_scenarios": list(worker.get("unsupported_scenarios") or []),
                "limitations": list(worker.get("limitations") or []),
                "supports_output_semantics": list(dict.fromkeys(x for x in supported_output_semantics if x)),
            })
        planner_semantic_catalog = [
            {
                "semantic_key": str(item.get("semantic_key") or ""),
                "kind": str(item.get("kind") or ""),
                "semantic_role": str(item.get("semantic_role") or ""),
                "source_policy": str(item.get("source_policy") or ""),
                "satisfaction_rule": str(item.get("satisfaction_rule") or ""),
            }
            for item in semantic_catalog
        ]
        authoritative_constraints = list(dict.fromkeys(
            str(item).strip() for item in (request_constraints or []) if str(item).strip()
        ))
        authoritative_target = dict(authoritative_target or {})
        candidate_view = [
            {
                "turn_id": str(item.get("turn_id") or ""),
                "trade_date": str(item.get("trade_date") or ""),
                "freshness": str(item.get("freshness") or ""),
                "reusable_outputs": list(item.get("reusable_outputs") or []),
                "answer_excerpt": str(item.get("answer_excerpt") or ""),
            }
            for item in list(reuse_candidates or [])
        ]
        payload = self.llm_service.generate_json(
            stage="upfront_merged_need_worker_planning",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是MainAgent的合并规划阶段。Request Decomposer已经完成业务语义规范化；"
                        "request_objective、authoritative_target、request_constraints是当前Request的权威语义，禁止重新改写、概括、扩大或缩小。"
                        "你的职责是一次性完成两件事：把完成该Request所必需的信息/分析/方案需求拆成少量、明确的needs；"
                        "并在生成每个Need时直接声明它的执行方式binding。"
                        "每个business Need必须至少包含一个direction=output的requirement。requirements只允许从semantic_requirement_catalog选择semantic_key；"
                        "不得自行发明数据名称/参数名。每个requirement必须声明necessity=required|preferred|optional。"
                        "direction=input表示该Need可能消费的系统事实；只有缺少它就根本无法完成该Need时才标required；"
                        "能增强结论但缺少后仍可降级完成的事实必须标preferred；纯增强项标optional。分析类Need中的外部证据、模型预测、排名等通常是preferred，"
                        "权威实体身份通常required。direction=output表示本Need必须产生的业务结果，通常necessity=required；"
                        "direction=parameter只用于用户必须明确决定、系统不可替用户决定的情景参数。"
                        "用户问‘应该怎么调整/应该配多少’时，目标仓位通常是系统应产生的output，不是用户parameter。"
                        "通用证券分析不得自行扩大用户要求。Business Request内部不要声明user_report/user_facing_report作为业务输出。"
                        "binding二选一必填：走计算路线填worker_binding（只填写worker_id和objective；worker_id必须来自worker_descriptions，"
                        "并且该Need的output semantic_key必须出现在该Worker的supports_output_semantics中）；"
                        "走复用路线填reuse_binding（turn_id只能引用reuse_candidates中出现的编号，derive_args只声明派生方向如{\"top_n\":5}，"
                        "不需要也不允许声明产出名，产出由系统从共享存储注册）。两者不能同时填，也不能同时空。"
                        "reuse_candidates是历史轮次的回答裁剪版并附带reusable_outputs；只有当候选真实物化了该Need所需output时才允许reuse_binding。"
                        "freshness_expectation=latest时，仅current_trade_day候选允许复用；旧交易日候选必须重新计算。"
                        "多个Need可以绑定同一个Worker；一个Need只能有一个binding。"
                        "不要选择Tool，不要生成DAG，不要输出私有Prompt。只输出JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "request_id": str(request_id or ""),
                        "request_objective": str(query or "").strip(),
                        "authoritative_target": authoritative_target,
                        "request_constraints": authoritative_constraints,
                        "effect_limit": effect_limit,
                        "reply_language": language,
                        "context_binding": binding_context,
                        "available_context_kinds": sorted(initial_context_names),
                        "authoritative_entity_refs_available": "authoritative_entity_refs" in initial_context_names,
                        "session_summary": str(memory_summary or "")[:1400],
                        "semantic_requirement_catalog": planner_semantic_catalog,
                        "worker_descriptions": planner_worker_descriptions,
                        "reuse_candidates": candidate_view,
                        "required_output_shape": {
                            "needs": [{
                                "description": "完成当前Request所必需的一个信息/分析/方案需求",
                                "required": True,
                                "requirements": [{
                                    "semantic_key": "must come from semantic_requirement_catalog",
                                    "direction": "input|output|parameter",
                                    "necessity": "required|preferred|optional",
                                    "required_paths": [],
                                }],
                                "binding": {
                                    "worker_binding": {
                                        "worker_id": "Wxx from worker_descriptions",
                                        "objective": "该Worker在本轮承担的业务目标",
                                    },
                                    "reuse_binding": None,
                                },
                            }],
                            "selection_reason": "只解释每个Need为什么走计算或复用",
                        },
                    }),
                },
            ],
            max_output_tokens=3000,
            validator=validate,
            operation=f"upfront_merged_planning:{effect_limit}",
            disable_thinking=False,
            repair_mode="targeted",
            repair_guidance=(
                "只修复Need、注册语义Requirement与binding。每个Need的binding二选一必填："
                "worker_binding只允许填写worker_id和objective；worker_id必须来自worker_descriptions，Need输出semantic_key必须属于该Worker的supports_output_semantics；"
                "reuse_binding的turn_id只能引用reuse_candidates中的编号；latest只允许复用current_trade_day且真实包含所需reusable_outputs的候选；proposal请求禁止reuse_binding；"
                "不得重新解释用户请求，不得输出Tool或DAG。"
            ),
        )

        # 归一化 Need 并沉淀 binding 注解
        normalized_needs: list[dict[str, Any]] = []
        reuse_decisions: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.get("needs") or [], start=1):
            row = dict(raw or {})
            need_id = self._normalize_need_id(index)
            raw_requirements = [dict(item) for item in row.get("requirements") or [] if isinstance(item, dict)]
            has_non_report_output = any(
                str(item.get("direction") or "") == "output"
                and str(item.get("semantic_key") or "") != "user_report"
                for item in raw_requirements
            )
            if has_non_report_output:
                raw_requirements = [
                    item for item in raw_requirements
                    if not (
                        str(item.get("direction") or "") == "output"
                        and str(item.get("semantic_key") or "") == "user_report"
                    )
                ]
            normalized_requirements = self.need_compiler.normalize_need_requirements(
                need_id=need_id, raw_requirements=raw_requirements, strict=True
            )
            binding = dict(row.get("binding") or {})
            if isinstance(binding.get("worker_binding"), dict) and binding.get("worker_binding"):
                worker_binding = dict(binding["worker_binding"])
                binding_annotation = {
                    "type": "worker",
                    "worker_id": str(worker_binding.get("worker_id") or "").strip().upper(),
                    "objective": str(worker_binding.get("objective") or "").strip(),
                    "desired_output_data_names": list(dict.fromkeys(
                        str(item.get("data_name") or "")
                        for item in normalized_requirements
                        if item.get("direction") == "output"
                        and item.get("kind") == "data"
                        and str(item.get("data_name") or "")
                    )),
                }
            else:
                reuse_binding = dict(binding.get("reuse_binding") or {})
                derive_args = reuse_binding.get("derive_args") if isinstance(reuse_binding.get("derive_args"), dict) else {}
                binding_annotation = {
                    "type": "reuse",
                    "turn_id": str(reuse_binding.get("turn_id") or "").strip(),
                    "derive_args": dict(derive_args),
                }
                candidate = dict(candidate_by_turn.get(binding_annotation["turn_id"]) or {})
                reuse_decisions.append({
                    "need_id": need_id,
                    "turn_id": binding_annotation["turn_id"],
                    "derive_args": dict(derive_args),
                    "freshness": str(candidate.get("freshness") or ""),
                    "reusable_outputs": list(candidate.get("reusable_outputs") or []),
                })
            normalized_needs.append({
                "need_id": need_id,
                "request_id": str(request_id or ""),
                "kind": "business",
                "description": str(row.get("description") or "").strip(),
                "required": bool(row.get("required", True)),
                "requirements": normalized_requirements,
                "binding": binding_annotation,
            })

        full_need_contract = {
            "schema_version": "request_need_contract.v2",
            "request_id": str(request_id or ""),
            "request_objective": str(query or "").strip(),
            "authoritative_target": authoritative_target,
            "requirement_contract_version": NeedRequirementCompiler.SCHEMA_VERSION,
            "needs": normalized_needs,
            "constraints": authoritative_constraints,
            "effect_limit": "proposal" if effect_limit == "proposal" else "read",
        }
        worker_needs = [
            dict(need) for need in normalized_needs
            if str(dict(need.get("binding") or {}).get("type") or "") == "worker"
        ]
        worker_need_contract = {**full_need_contract, "needs": worker_needs}

        # 从 worker_binding 确定性归并 Worker 调用计划（同 Worker 合并为一次调用）
        calls: list[dict[str, Any]] = []
        call_by_worker: dict[str, dict[str, Any]] = {}
        for need in worker_needs:
            binding = dict(need.get("binding") or {})
            worker_id = str(binding.get("worker_id") or "")
            call = call_by_worker.get(worker_id)
            if call is None:
                call = {
                    "call_id": f"WC{len(calls) + 1:02d}",
                    "worker_id": worker_id,
                    "objective": str(binding.get("objective") or "").strip(),
                    "covers_need_ids": [],
                    "desired_output_data_names": [],
                }
                call_by_worker[worker_id] = call
                calls.append(call)
            need_id = str(need.get("need_id") or "")
            if need_id and need_id not in call["covers_need_ids"]:
                call["covers_need_ids"].append(need_id)
            for name in binding.get("desired_output_data_names") or []:
                if str(name) and str(name) not in call["desired_output_data_names"]:
                    call["desired_output_data_names"].append(str(name))
        worker_call_plan = {
            "worker_calls": calls,
            "selection_reason": str(payload.get("selection_reason") or "").strip(),
        }
        if calls:
            # 计算路线 Need 的 required output 必须由覆盖它的 Worker 真实产出
            self.need_compiler.validate_worker_call_need_outputs(
                request_need_contract=worker_need_contract,
                worker_calls=calls,
            )

        runtime_debug_event(
            "MERGED_NEED_WORKER_BINDING_DETAIL",
            {
                "request_id": str(request_id or ""),
                "full_need_contract": full_need_contract,
                "worker_need_contract": worker_need_contract,
                "worker_call_plan": worker_call_plan,
                "reuse_decisions": reuse_decisions,
                "reuse_candidates": candidate_view,
            },
            run_id=run_id,
        )

        flow_event(
            "MERGED_NEED_WORKER_PLANNING_COMPLETED",
            {
                "request_id": str(request_id or ""),
                "need_count": len(normalized_needs),
                "worker_bound_need_count": len(worker_needs),
                "reuse_decisions": reuse_decisions,
                "worker_call_count": len(calls),
                "worker_ids": [row["worker_id"] for row in calls],
                "selection_reason": worker_call_plan["selection_reason"][:1200],
                "merged_planning_call": True,
            },
            run_id=run_id,
        )
        return full_need_contract, worker_need_contract, worker_call_plan, reuse_decisions

    def _normalize_task_ids(
        self, tasks: list[dict[str, Any]], *, task_id_prefix: str = ""
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for task_index, raw_task in enumerate(tasks or [], start=1):
            if not isinstance(raw_task, dict):
                continue
            task_id = f"{str(task_id_prefix or '')}T{task_index:02d}"
            row = dict(raw_task)
            row["task_id"] = task_id
            row["worker_id"] = str(row.get("worker_id") or "").strip().upper()
            # ``boundary_id`` is retained only as a compatibility/audit field.
            # MainAgent no longer selects a fine-grained sub-boundary. Runtime
            # deterministically labels the task with the selected Worker's
            # existing role, while contract validation uses that Worker's full
            # capability scope.
            try:
                row["boundary_id"] = str(self.directory.get(row["worker_id"]).role)
            except Exception:
                row["boundary_id"] = ""
            contracts: list[dict[str, Any]] = []
            for contract_index, raw_contract in enumerate(row.get("contracts") or [], start=1):
                if not isinstance(raw_contract, dict):
                    continue
                contract = dict(raw_contract)
                contract["contract_id"] = f"{task_id}-C{contract_index:02d}"
                contract.setdefault("description", str(row.get("objective") or ""))
                contract.setdefault("criticality", "required")
                contract.setdefault("effect_limit", row.get("effect_limit") or "read")
                contract.setdefault("allowed_terminal_states", ["completed", "business_empty", "business_insufficient"])
                contracts.append(contract)
            row["contracts"] = contracts
            normalized.append(row)
        return normalized

    def _goal_contract(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_calls: list[dict[str, Any]],
    ) -> dict[str, Any]:
        desired_data_names = list(dict.fromkeys(
            slot
            for call in worker_calls
            for slot in call.get("desired_output_data_names") or []
            if str(slot)
        ))
        return {
            "goal_summary": str(request_need_contract.get("request_objective") or "").strip(),
            "desired_outputs": desired_data_names,
            "required_context_names": [],
            "effect_limit": str(request_need_contract.get("effect_limit") or "read"),
            "request_need_ids": [str(row.get("need_id")) for row in request_need_contract.get("needs") or [] if row.get("need_id")],
        }

    def _generate_worker_dag(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_call_plan: dict[str, Any],
        worker_descriptions: list[dict[str, Any]],
        effect_limit: str,
        run_id: str,
        initial_context_names: set[str],
        recovery_context: dict[str, Any] | None = None,
        task_id_prefix: str = "",
    ) -> tuple[dict[str, Any], list[CapabilityTask]]:
        """Compile the Worker DAG from Need/Worker contracts without an LLM.

        V23.0.11 ends MainAgent semantic planning after Worker selection.  The
        selected WorkerCalls, registered Need requirements and public Worker
        scopes are sufficient to deterministically build CapabilityContracts.
        TaskDependencyCompiler 只推导执行顺序；业务数据统一通过 RunContextStore 共享。

        ``recovery_context`` may change Worker selection upstream, but it never
        causes a separate Worker-DAG LLM planning call.
        """

        calls = [dict(item) for item in worker_call_plan.get("worker_calls") or [] if isinstance(item, dict)]
        if not calls:
            raise WorkerContractViolation("worker_calls_required", "$.worker_calls")

        goal = self._goal_contract(request_need_contract=request_need_contract, worker_calls=calls)
        requirement_contract_version = str(request_need_contract.get("requirement_contract_version") or "")
        if requirement_contract_version != NeedRequirementCompiler.SCHEMA_VERSION:
            raise WorkerContractViolation(
                "need_requirement_contract_version_required",
                "$.request_need_contract.requirement_contract_version",
                NeedRequirementCompiler.SCHEMA_VERSION,
            )

        runtime_debug_event(
            "WORKER_DAG_COMPILER_INPUT",
            {
                "request_need_contract": request_need_contract,
                "worker_calls": calls,
                "initial_context_names": sorted(initial_context_names),
                "task_id_prefix": task_id_prefix,
            },
            run_id=run_id,
        )
        task_requirements = self.need_compiler.compile_task_requirements(
            request_need_contract=request_need_contract,
            worker_calls=calls,
        )
        raw_tasks = self.need_compiler.expand_compact_tasks(
            request_need_contract=request_need_contract,
            worker_calls=calls,
            task_requirements=task_requirements,
        )
        normalized_tasks = self._normalize_task_ids(raw_tasks, task_id_prefix=task_id_prefix)
        payload = {
            "goal_contract": goal,
            "tasks": normalized_tasks,
            "task_requirements": task_requirements,
            "contract_expansion_mode": "deterministic_need_worker_dag_compiler",
        }

        tasks = self.validator.validate(payload)
        selected_ids = {str(row.get("worker_id") or "") for row in calls}
        unknown = sorted({task.worker_id for task in tasks} - selected_ids)
        if unknown:
            raise WorkerContractViolation(
                "worker_dag_outside_selected_calls", "$.tasks[*].worker_id", ",".join(unknown)
            )
        if len(tasks) != len(calls):
            raise WorkerContractViolation(
                "worker_dag_call_task_count_mismatch",
                "$.tasks",
                f"calls={len(calls)},tasks={len(tasks)}",
            )
        for call, task in zip(calls, tasks):
            worker_id = str(call.get("worker_id") or "")
            if task.worker_id != worker_id:
                raise WorkerContractViolation(
                    "compiled_task_worker_order_mismatch",
                    "$.task_requirements",
                    f"{call.get('call_id')}:{task.worker_id}",
                )
            desired = {str(item) for item in call.get("desired_output_data_names") or [] if str(item)}
            missing = sorted(desired - set(task.output_data_names()))
            if missing:
                raise WorkerContractViolation(
                    "worker_call_not_realized_by_dag",
                    "$.task_requirements",
                    f"{call.get('call_id')}:{','.join(missing)}",
                )

        runtime_debug_event(
            "WORKER_DAG_COMPILER_OUTPUT",
            {
                "task_requirements": task_requirements,
                "normalized_tasks": normalized_tasks,
                "validated_tasks": [task.to_dict() for task in tasks],
            },
            run_id=run_id,
        )

        flow_event(
            "WORKER_DAG_COMPILED_DETERMINISTIC",
            {
                "task_count": len(tasks),
                "contract_count": sum(len(item.contracts) for item in tasks),
                "worker_ids": [item.worker_id for item in tasks],
                "compiler": "need_requirement_registry_context_compiler",
                "main_agent_llm_worker_dag_call": False,
                "dependency_owner": "task_dependency_compiler",
                "worker_private_tool_planning_preserved": True,
            },
            run_id=run_id,
        )
        flow_event(
            "UPFRONT_WORKER_DAG_VALIDATED",
            {
                "task_count": len(tasks),
                "contract_count": sum(len(item.contracts) for item in tasks),
                "worker_ids": [item.worker_id for item in tasks],
                "worker_scope_ids": [item.boundary_id for item in tasks],
                "contract_expansion_mode": payload["contract_expansion_mode"],
                "main_agent_worker_visibility": "all_public_descriptions_upfront",
                "main_agent_tool_visibility": "none",
                "raw_request_used": False,
            },
            run_id=run_id,
        )
        return payload, tasks

    def _compile(
        self,
        *,
        payload: dict[str, Any],
        tasks: list[CapabilityTask],
        effect_limit: str,
        session_id: str,
        run_id: str,
        user_id: str,
        focus_refs: list[Any],
        context_refs: list[Any],
        as_of_time: str,
        initial_context_names: set[str],
        planning_meta: dict[str, Any],
        external_producers: dict[str, list[dict[str, str]]] | None = None,
    ) -> tuple[list[GraphAgentTask], dict[str, Any]]:
        del external_producers
        dependencies = self.dependency_compiler.compile(tasks)
        resolved = self.assignment_validator.validate(tasks, dependencies=dependencies)
        goal = dict(payload.get("goal_contract") or {})
        request_need_contract = dict(planning_meta.get("request_need_contract") or {})
        worker_calls = list((planning_meta.get("worker_call_plan") or {}).get("worker_calls") or [])
        compiled: list[GraphAgentTask] = []
        for item in resolved:
            task = item.task
            selected_call = next(
                (
                    dict(call) for call in worker_calls
                    if str(call.get("worker_id") or "").upper() == str(item.assigned_worker_id or "").upper()
                ),
                {},
            )
            covered_need_ids = {str(value) for value in selected_call.get("covers_need_ids") or [] if str(value)}
            need_input_requirements = [
                {
                    "requirement_id": str(req.get("requirement_id") or ""),
                    "need_id": str(need.get("need_id") or ""),
                    "semantic_key": str(req.get("semantic_key") or ""),
                    "kind": str(req.get("kind") or ""),
                    "data_name": str(req.get("data_name") or ""),
                    "context_name": str(req.get("context_name") or ""),
                    "necessity": str(req.get("necessity") or "required"),
                    "satisfaction_rule": str(req.get("satisfaction_rule") or "exists"),
                }
                for need in request_need_contract.get("needs") or []
                if str(need.get("need_id") or "") in covered_need_ids
                for req in need.get("requirements") or []
                if req.get("direction") == "input"
            ]
            compiled.append(GraphAgentTask(
                task_id=task.task_id,
                run_id=run_id,
                session_id=session_id,
                assigned_agent=item.assigned_agent_id,
                objective=task.objective,
                user_id=user_id,
                boundary_id=task.boundary_id,
                contracts=[contract.to_dict() for contract in task.contracts],
                worker_id=item.assigned_worker_id,
                business_parameters=dict(task.business_parameters),
                dependency_task_ids=list(item.dependency_task_ids),
                expected_data_names=task.output_data_names(),
                effect_limit=effect_limit,
                execution_mode=item.execution_mode,
                focus_refs=list(focus_refs),
                context_refs=list(context_refs),
                as_of_time=as_of_time,
                priority=task.priority,
                metadata={
                    "goal_contract": goal,
                    "request_need_contract": request_need_contract,
                    "worker_call_plan": worker_calls,
                    "worker_assignment": item.to_audit_dict(),
                    "allowed_tool_ids": list(item.allowed_tool_ids),
                    "structured_capability_contract": True,
                    "upfront_worker_dag": True,
                    "request_id": str(planning_meta.get("request_id") or request_need_contract.get("request_id") or ""),
                    "business_data_transport": "run_context_store",
                    "initial_runtime_context_names": sorted(initial_context_names),
                    "covers_need_ids": sorted(covered_need_ids),
                    "input_requirements": need_input_requirements,
                    "dependency_semantics": "completion_order_only",
                },
            ))
        runtime_debug_event(
            "WORKER_TASK_RUNTIME_BINDING",
            {
                "compiled_tasks": [task.to_dict() for task in compiled],
                "task_dependencies": dependencies,
                "semantics": "dependencies_wait_for_terminal; input_requirements_control_block_or_degrade",
            },
            run_id=run_id,
        )
        meta = {
            "planner": "need_worker_assignment_runtime_compiler",
            "runtime_version": RUNTIME_VERSION,
            "planning_mode": "merged_need_binding_then_runtime_dependency_compile_then_private_tool_dag",
            "worker_selection_owner": "main_agent",
            "main_agent_llm_planning_stages": ["upfront_merged_need_worker_planning"],
            "worker_dag_build_owner": "runtime_deterministic_compiler",
            "worker_private_planning_owner": "specialist_worker",
            "business_data_owner": "run_context_store",
            "task_dependency_owner": "request_task_state",
            "worker_assignment_runtime_role": "validate_only",
            "capability_scope_mode": "worker_level",
            "raw_request_semantic_owner": "request_bundle.objective",
            "request_id": str(planning_meta.get("request_id") or request_need_contract.get("request_id") or ""),
            "task_count": len(compiled),
            "contract_count": sum(len(task.contracts) for task in compiled),
            "goal_contract": goal,
            "request_need_contract": request_need_contract,
            "worker_call_plan": planning_meta.get("worker_call_plan") or {},
            "worker_description_count": int(planning_meta.get("worker_description_count") or 0),
            "capability_plan": payload,
            "task_dependencies": dependencies,
            "assignment_audit": [item.to_audit_dict() for item in resolved],
        }
        return compiled, meta

    def plan(
        self,
        *,
        query: str,
        effect_limit: str,
        session_id: str,
        run_id: str,
        user_id: str,
        focus_refs: list,
        context_refs: list,
        memory_summary: str,
        language: str = "zh",
        as_of_time: str = "",
        context_binding: dict[str, Any] | None = None,
        request_id: str = "",
        task_id_prefix: str = "",
        external_producers: dict[str, list[dict[str, str]]] | None = None,
        authoritative_target: dict[str, Any] | None = None,
        request_constraints: list[str] | None = None,
        reuse_candidates: list[dict[str, Any]] | None = None,
    ) -> tuple[list[GraphAgentTask], dict[str, Any]]:
        # Request dependencies are execution-order state, not Worker business-data inputs.
        del external_producers
        request_effect_limit = str(effect_limit or "read").lower()
        if request_effect_limit not in {"read", "proposal"}:
            raise CoordinatorPlanningError(f"invalid_business_effect_limit:{request_effect_limit}")
        initial_context_names = self._initial_context_names(
            focus_refs=focus_refs,
            context_refs=context_refs,
            memory_summary=memory_summary,
        )
        try:
            descriptions = self._load_worker_descriptions(effect_limit=request_effect_limit, run_id=run_id)
            # 合并规划：一次 LLM 调用生成 Need 并声明 binding（替代原来的两次调用）
            full_need_contract, worker_need_contract, worker_call_plan, reuse_decisions = self._plan_needs_with_bindings(
                query=query,
                effect_limit=request_effect_limit,
                run_id=run_id,
                language=language,
                initial_context_names=initial_context_names,
                memory_summary=memory_summary,
                worker_descriptions=descriptions,
                context_binding=context_binding,
                request_id=request_id,
                authoritative_target=authoritative_target,
                request_constraints=request_constraints,
                reuse_candidates=reuse_candidates,
            )
            if worker_call_plan["worker_calls"]:
                payload, tasks = self._generate_worker_dag(
                    request_need_contract=worker_need_contract,
                    worker_call_plan=worker_call_plan,
                    worker_descriptions=descriptions,
                    effect_limit=request_effect_limit,
                    run_id=run_id,
                    initial_context_names=initial_context_names,
                    task_id_prefix=task_id_prefix,
                )
                compiled, meta = self._compile(
                    payload=payload,
                    tasks=tasks,
                    effect_limit=request_effect_limit,
                    session_id=session_id,
                    run_id=run_id,
                    user_id=user_id,
                    focus_refs=focus_refs,
                    context_refs=context_refs,
                    as_of_time=as_of_time,
                    initial_context_names=initial_context_names,
                    planning_meta={
                        "request_need_contract": full_need_contract,
                        "worker_call_plan": worker_call_plan,
                        "worker_description_count": len(descriptions),
                        "request_id": str(request_id or ""),
                    },
                )
            else:
                # 全复用零 Worker：所有 Need 均由复用满足，无 DAG 可编译
                goal = {
                    "goal_summary": str(full_need_contract.get("request_objective") or "").strip(),
                    "desired_outputs": [],
                    "required_context_names": [],
                    "effect_limit": str(full_need_contract.get("effect_limit") or "read"),
                    "request_need_ids": [str(row.get("need_id")) for row in full_need_contract.get("needs") or [] if row.get("need_id")],
                }
                compiled = []
                meta = {
                    "planner": "merged_need_binding_planner",
                    "runtime_version": RUNTIME_VERSION,
                    "planning_mode": "merged_need_binding_all_reuse",
                    "worker_selection_owner": "main_agent",
                    "main_agent_llm_planning_stages": ["upfront_merged_need_worker_planning"],
                    "worker_dag_build_owner": "runtime_deterministic_compiler",
                    "raw_request_semantic_owner": "request_bundle.objective",
                    "request_id": str(request_id or ""),
                    "task_count": 0,
                    "goal_contract": goal,
                    "request_need_contract": full_need_contract,
                    "worker_call_plan": worker_call_plan,
                    "capability_plan": {"goal_contract": goal, "tasks": [], "contract_expansion_mode": "all_needs_reuse_satisfied"},
                    "task_dependencies": {},
                }
                flow_event(
                    "WORKER_DAG_SKIPPED_ALL_REUSE",
                    {
                        "request_id": str(request_id or ""),
                        "reuse_decisions": reuse_decisions,
                    },
                    run_id=run_id,
                )
            meta["reuse_decisions"] = reuse_decisions
            meta["planning_gap_repair"] = {
                "repair_count": 0,
                "max_repairs": 0,
                "audit": [],
                "reason": "business sufficiency is not runtime-validated; W09 judges sufficiency from other Worker results",
            }
            return compiled, meta
        except (WorkerContractViolation, KeyError, ValueError) as exc:
            raise CoordinatorPlanningError(
                str(exc), diagnostics={"failure_kind": "upfront_worker_dag_planning_failure"}
            ) from exc

    @staticmethod
    def _request_need_contract_from_tasks(current_tasks: list[GraphAgentTask]) -> dict[str, Any]:
        for task in current_tasks:
            value = dict((task.metadata or {}).get("request_need_contract") or {})
            if value:
                return value
        return {}

    def replan_forward(
        self,
        *,
        query: str,
        effect_limit: str,
        session_id: str,
        run_id: str,
        user_id: str,
        focus_refs: list,
        context_refs: list,
        memory_summary: str,
        language: str,
        as_of_time: str,
        current_tasks: list[GraphAgentTask],
        current_results: dict[str, GraphWorkerResult],
        observations: list[dict[str, Any]],
        replan_round: int,
        error_history: list[dict[str, Any]] | None = None,
    ) -> tuple[list[GraphAgentTask], list[GraphAgentTask], dict[str, Any]]:
        del query, language
        request_need_contract = self._request_need_contract_from_tasks(current_tasks)
        if not request_need_contract:
            raise WorkerContractViolation("replan_missing_request_need_contract", "$.task.metadata.request_need_contract")

        all_required_need_ids = {
            str(need.get("need_id") or "")
            for need in request_need_contract.get("needs") or []
            if bool(need.get("required", True)) and str(need.get("need_id") or "")
        }
        task_by_id = {task.task_id: task for task in current_tasks}

        # Start from Workers that actually failed/need recovery.
        unresolved_need_ids: set[str] = set()
        for item in observations:
            if item.get("semantic_satisfied") or not item.get("replan_recommended"):
                continue
            task = task_by_id.get(str(item.get("task_id") or ""))
            if task is not None:
                unresolved_need_ids.update(
                    str(need_id)
                    for need_id in (task.metadata or {}).get("covers_need_ids") or []
                    if str(need_id)
                )

        # W09 may judge that a provider business semantic is missing even when
        # that provider Worker technically executed. Re-open only the provider
        # Need(s) that produce those semantic keys; do not reopen unrelated
        # successful Needs.
        missing_semantic_keys: set[str] = set()
        for result in current_results.values():
            error = dict(result.error or {})
            rows = error.get("missing_data_requirements")
            if not isinstance(rows, list):
                rows = (result.metadata or {}).get("missing_data_requirements")
            for row in rows or []:
                if isinstance(row, dict) and str(row.get("semantic_key") or "").strip():
                    missing_semantic_keys.add(str(row.get("semantic_key")).strip())
        reopened_provider_need_ids: set[str] = set()
        if missing_semantic_keys:
            for need in request_need_contract.get("needs") or []:
                need_id = str(need.get("need_id") or "")
                if not need_id:
                    continue
                output_semantics = {
                    str(req.get("semantic_key") or "")
                    for req in need.get("requirements") or []
                    if isinstance(req, dict)
                    and req.get("direction") == "output"
                    and str(req.get("semantic_key") or "")
                }
                if output_semantics.intersection(missing_semantic_keys):
                    reopened_provider_need_ids.add(need_id)
            unresolved_need_ids.update(reopened_provider_need_ids)

        unresolved_need_ids &= {
            str(need.get("need_id") or "")
            for need in request_need_contract.get("needs") or []
            if str(need.get("need_id") or "")
        }
        if not unresolved_need_ids:
            unresolved_need_ids = set(all_required_need_ids)

        completed_need_ids: set[str] = set()
        reusable_ids: set[str] = set()
        for task_id, result in current_results.items():
            task = task_by_id.get(task_id)
            if task is None:
                continue
            covered = {
                str(need_id)
                for need_id in (task.metadata or {}).get("covers_need_ids") or []
                if str(need_id)
            }
            completed_execution = (
                result.status in {ResultStatus.COMPLETED, ResultStatus.PROPOSAL_READY}
                and bool((result.completion or {}).get("expected_task_completed", True))
            )
            if completed_execution and not covered.intersection(unresolved_need_ids):
                reusable_ids.add(task_id)
                completed_need_ids.update(covered)

        frozen = [task for task in current_tasks if task.task_id in reusable_ids]
        task_id_prefix = str((current_tasks[0].metadata or {}).get("task_id_prefix") or "") if current_tasks else ""
        initial_context_names = self._initial_context_names(
            focus_refs=focus_refs, context_refs=context_refs, memory_summary=memory_summary,
            extra_context_names=set(),
        )

        failure_signatures: list[dict[str, Any]] = []
        failed_worker_results: list[dict[str, Any]] = []
        for item in observations[:30]:
            if item.get("semantic_satisfied"):
                continue
            error = item.get("worker_escalation") or item.get("error") or {}
            failure_signatures.append({
                "task_id": item.get("task_id"),
                "worker_id": item.get("worker_id"),
                "boundary_id": item.get("boundary_id"),
                "error_id": error.get("error_id") or error.get("code"),
                "operation": error.get("operation"),
                "reason": error.get("reason") or error.get("message"),
                "missing_business_data": item.get("missing_business_data") or item.get("missing_data_names") or [],
                "missing_context": item.get("missing_context") or [],
            })
            result = current_results.get(str(item.get("task_id") or ""))
            if result is not None:
                failed_worker_results.append({
                    "task_id": result.task_id,
                    "worker_id": str(item.get("worker_id") or ""),
                    "agent_id": result.agent_id,
                    "status": result.status.value,
                    "output_type": result.output_type,
                    "summary": result.summary,
                    "error": dict(result.error or {}),
                    "completion": dict(result.completion or {}),
                    "produced_data_names": list((result.completion or {}).get("produced_data_names") or []),
                    "missing_data_names": list((result.completion or {}).get("missing_data_names") or []),
                    "missing_items": [item.to_dict() for item in result.missing_items[:12]],
                })

        accumulated_history = [
            dict(item) for item in list(error_history or []) if isinstance(item, dict)
        ]
        seen_history = {
            (str(item.get("task_id") or ""), str((item.get("error") or {}).get("code") or (item.get("error") or {}).get("error_id") or ""), str((item.get("error") or {}).get("message") or (item.get("error") or {}).get("reason") or ""))
            for item in accumulated_history
        }
        for item in failed_worker_results:
            key = (
                str(item.get("task_id") or ""),
                str((item.get("error") or {}).get("code") or (item.get("error") or {}).get("error_id") or ""),
                str((item.get("error") or {}).get("message") or (item.get("error") or {}).get("reason") or ""),
            )
            if key not in seen_history:
                accumulated_history.append(item)
                seen_history.add(key)

        recovery_need_contract = {
            **dict(request_need_contract),
            "needs": [
                dict(need)
                for need in request_need_contract.get("needs") or []
                if str(need.get("need_id") or "") in unresolved_need_ids
            ],
        }
        recovery_context = {
            "round": int(replan_round),
            "failure_signatures": failure_signatures,
            "failed_worker_results": failed_worker_results,
            "recovery_error_history": accumulated_history,
            "frozen_completed_need_ids": sorted(completed_need_ids),
            "unresolved_need_ids": sorted(unresolved_need_ids),
            "reopened_provider_need_ids": sorted(reopened_provider_need_ids),
            "missing_semantic_keys_from_w09": sorted(missing_semantic_keys),
            "working_memory_reuse": True,
            "instruction": "根据真实错误结果只重规划未完成Need；成功Need冻结；由LLM决定下一轮Worker。",
        }
        descriptions = self._load_worker_descriptions(effect_limit=effect_limit, run_id=run_id)
        worker_call_plan = self._select_recovery_worker_calls(
            request_need_contract=request_need_contract, worker_descriptions=descriptions, effect_limit=effect_limit,
            run_id=run_id, initial_context_names=initial_context_names, recovery_context=recovery_context,
        )
        payload, capability_tasks = self._generate_worker_dag(
            request_need_contract=recovery_need_contract, worker_call_plan=worker_call_plan,
            worker_descriptions=descriptions, effect_limit=effect_limit,
            run_id=run_id, initial_context_names=initial_context_names,
            recovery_context=recovery_context, task_id_prefix=task_id_prefix,
        )
        start_index = len({task.task_id for task in current_tasks}) + 1
        remapped: list[CapabilityTask] = []
        for offset, task in enumerate(capability_tasks):
            new_id = f"{task_id_prefix}T{start_index + offset:02d}"
            row = task.to_dict()
            row["task_id"] = new_id
            for index, contract in enumerate(row.get("contracts") or [], start=1):
                contract["contract_id"] = f"{new_id}-C{index:02d}"
            remapped.append(CapabilityTask.from_dict(row, task_id=new_id))
        payload = dict(payload)
        payload["tasks"] = [task.to_dict() for task in remapped]
        new_tasks, meta = self._compile(
            payload=payload, tasks=remapped, effect_limit=effect_limit,
            session_id=session_id, run_id=run_id, user_id=user_id,
            focus_refs=focus_refs, context_refs=context_refs, as_of_time=as_of_time,
            initial_context_names=initial_context_names,
            planning_meta={
                "request_need_contract": recovery_need_contract,
                "worker_call_plan": worker_call_plan,
                "worker_description_count": len(descriptions),
            },
        )
        # Keep the immutable full Request Need contract on every Recovery task so
        # a second Recovery round never forgets already-frozen Needs.
        for task in new_tasks:
            task.metadata["request_need_contract"] = dict(request_need_contract)
            task.metadata["recovery_need_contract"] = dict(recovery_need_contract)
            task.metadata["replan_round"] = int(replan_round)

        full = [*frozen, *new_tasks]
        meta.update({
            "replan_round": int(replan_round),
            "recovery_only": True,
            "request_need_contract_reused": True,
            "recovery_need_contract": recovery_need_contract,
            "working_memory_reused": True,
            "frozen_task_ids": [task.task_id for task in frozen],
            "frozen_completed_need_ids": sorted(completed_need_ids),
            "unresolved_need_ids": sorted(unresolved_need_ids),
            "new_task_ids": [task.task_id for task in new_tasks],
            "failure_signatures": failure_signatures,
            "failed_worker_results": failed_worker_results,
            "recovery_error_history": accumulated_history,
        })
        return full, new_tasks, meta
