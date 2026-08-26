"""Upfront MainAgent Worker-DAG planner.

Normal execution has one bounded planning phase before any Worker executes:

1. Decompose one already-normalized Request into one request_need_contract.
2. Load every eligible Worker's full public description.
3. Select Worker calls that explicitly cover the request need contract needs.
4. Compile the complete Worker DAG deterministically from Need/business-data contracts.
5. Each selected Worker plans its own private Tool DAG when applicable.

The raw user request is not re-interpreted by Worker selection or Worker-DAG compilation. Replan remains an exception-recovery path and reuses the original
request_need_contract.
"""

from __future__ import annotations

from typing import Any

from core.llm import LLMService
from core.llm.prompt_compaction import compact_json_dumps

from agent.console_trace import flow_event
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

    def _plan_request_need_contract(
        self,
        *,
        query: str,
        effect_limit: str,
        run_id: str,
        language: str,
        initial_context_names: set[str],
        memory_summary: str,
        context_binding: dict[str, Any] | None = None,
        request_id: str = "",
        request_target: dict[str, Any] | None = None,
        request_constraints: list[str] | None = None,
    ) -> dict[str, Any]:
        """Generate Need[] directly from an already-normalized Request.

        Request Decomposer owns business-semantic normalization.  This MainAgent
        stage must not rewrite, summarize, broaden, or narrow the Request.  It
        only decomposes the authoritative Request objective/target/constraints
        into machine-validated Need requirements.
        """

        def validate(payload: dict[str, Any]) -> None:
            if not isinstance(payload, dict):
                raise WorkerContractViolation("request_need_contract_not_object", "$")
            unexpected_fields = sorted(key for key in payload if key != "needs")
            if unexpected_fields:
                raise WorkerContractViolation(
                    "request_need_unexpected_top_level_field",
                    "$",
                    ",".join(unexpected_fields),
                )
            raw_needs = payload.get("needs")
            if not isinstance(raw_needs, list) or not raw_needs:
                raise WorkerContractViolation("request_needs_required", "$.needs")
            proposal_output_seen = False
            for index, row in enumerate(raw_needs):
                if not isinstance(row, dict) or not str(row.get("description") or "").strip():
                    raise WorkerContractViolation("request_need_description_required", f"$.needs[{index}]")
                normalized = self.need_compiler.normalize_need_requirements(
                    need_id=f"N{index + 1:02d}",
                    raw_requirements=row.get("requirements") or [],
                    strict=True,
                )
                for requirement in normalized:
                    if requirement.get("direction") != "output":
                        continue
                    data_name = str(requirement.get("data_name") or "")
                    if data_name in {"proposal", "rebalance"}:
                        proposal_output_seen = True
            if effect_limit == "proposal" and not proposal_output_seen:
                raise WorkerContractViolation(
                    "proposal_request_missing_proposal_output_need",
                    "$.needs",
                    "proposal-capable READ Request must contain a Need whose output is a proposal/rebalance result",
                )

        semantic_catalog = self.registry.semantic_requirement_catalog()
        authoritative_constraints = list(dict.fromkeys(
            str(item).strip() for item in (request_constraints or []) if str(item).strip()
        ))
        authoritative_target = dict(request_target or {})
        payload = self.llm_service.generate_json(
            stage="upfront_request_need_planning",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是MainAgent的Need Decomposition阶段。Request Decomposer已经完成业务语义规范化；"
                        "request_objective、request_target、request_constraints是当前Request的权威语义，禁止重新改写、概括、扩大或缩小。"
                        "你的唯一职责是把完成该Request所必需的信息/分析/方案需求拆成少量、明确的needs，并为每个Need声明机器可校验requirements。"
                        "顶层只允许输出needs，不得生成新的objective、target、constraints或其他Request语义字段。"
                        "每个Need只能服务于request_objective；不得因为某能力可能有帮助而新增用户没有要求的分析维度。"
                        "每个business Need必须至少包含一个direction=output的requirement。requirements只允许从semantic_requirement_catalog选择semantic_key；"
                        "不得写Worker ID、Agent ID、Tool、Capability boundary或自行发明数据名称/参数名。"
                        "direction=input表示完成本Need必须先具备的系统事实；direction=output表示本Need希望系统产生的业务结果；"
                        "direction=parameter只用于用户必须明确决定、系统不可替用户决定的情景参数。"
                        "用户问‘应该怎么调整/应该配多少/你建议怎么配置’时，目标仓位通常是系统应产生的output，不是用户parameter；"
                        "只有用户明确指定比例/金额或要求在用户指定规模下做情景测算时，才使用target_allocation parameter。"
                        "通用证券分析不得自行扩大用户要求。用户只要求分析某只证券且未明确指定财务、估值、价格快照、资金流或同业比较时，"
                        "不要自动增加这些分析维度。"
                        "Business Request内部不要声明user_report/user_facing_report作为业务输出；用户可读报告由Bundle-level报告阶段统一生成。"
                        "对动态目标（如‘模型排名第一的股票’），可以声明完成该目标确实需要的market_ranking、entity_model_signals、entity_analysis等语义结果，"
                        "但不要把Worker内部Tool步骤写成Need依赖。"
                        "如果当前READ Request允许生成Proposal，必须包含真正产出状态变更方案的Need；不得执行Commit。"
                        "required_paths只用于用户需求明确要求具体字段时的最小字段约束；不知道数据结构时留空。"
                        "context_binding和available_context_kinds只用于判断当前可用/权威上下文，不得据此改变request_objective。"
                        "session_summary只是允许进入本阶段的背景，不是新的业务目标来源，也不能重新引入未绑定的历史金融实体。"
                        "只输出JSON对象，顶层只允许needs。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "request_id": str(request_id or ""),
                        "request_objective": str(query or "").strip(),
                        "request_target": authoritative_target,
                        "request_constraints": authoritative_constraints,
                        "effect_limit": effect_limit,
                        "reply_language": language,
                        "context_binding": dict(context_binding or {}),
                        "available_context_kinds": sorted(initial_context_names),
                        "authoritative_entity_refs_available": "authoritative_entity_refs" in initial_context_names,
                        "session_summary": str(memory_summary or "")[:1400],
                        "semantic_requirement_catalog": semantic_catalog,
                        "required_output_shape": {
                            "needs": [{
                                "description": "完成当前Request所必需的一个信息/分析/方案需求",
                                "required": True,
                                "requirements": [{
                                    "semantic_key": "must come from semantic_requirement_catalog",
                                    "direction": "input|output|parameter",
                                    "required": True,
                                    "required_paths": [],
                                }],
                            }],
                        },
                    }),
                },
            ],
            max_output_tokens=1500,
            validator=validate,
            operation=f"upfront_request_need_plan:{effect_limit}",
            disable_thinking=False,
            repair_mode="targeted",
            repair_guidance=(
                "只修复Need及注册语义Requirement；顶层只允许needs，不得生成新的Request语义字段，"
                "不得加入Worker、Tool、Capability名称或未注册semantic_key。Proposal请求必须有实际Proposal输出Need。"
            ),
        )

        normalized_needs: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.get("needs") or [], start=1):
            row = dict(raw or {})
            need_id = self._normalize_need_id(index)
            raw_requirements = [dict(item) for item in row.get("requirements") or [] if isinstance(item, dict)]
            # W06 is Bundle-level only. Remove a redundant per-Request report
            # output if the Need already has a real business output.
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
            normalized_needs.append({
                "need_id": need_id,
                "request_id": str(request_id or ""),
                "kind": "business",
                "description": str(row.get("description") or "").strip(),
                "required": bool(row.get("required", True)),
                "requirements": self.need_compiler.normalize_need_requirements(
                    need_id=need_id,
                    raw_requirements=raw_requirements,
                    strict=True,
                ),
            })

        request_need_contract = {
            "schema_version": "request_need_contract.v1",
            "request_id": str(request_id or ""),
            "request_objective": str(query or "").strip(),
            "request_target": authoritative_target,
            "requirement_contract_version": NeedRequirementCompiler.SCHEMA_VERSION,
            "needs": normalized_needs,
            "constraints": authoritative_constraints,
            "effect_limit": "proposal" if effect_limit == "proposal" else "read",
        }
        flow_event(
            "REQUEST_NEED_CONTRACT_CREATED",
            {
                "request_id": str(request_id or ""),
                "request_objective": request_need_contract["request_objective"],
                "request_target": request_need_contract["request_target"],
                "need_count": len(request_need_contract["needs"]),
                "needs": request_need_contract["needs"],
                "requirement_contract_version": request_need_contract["requirement_contract_version"],
                "effect_limit": request_need_contract["effect_limit"],
                "request_semantic_source": "request_bundle.objective",
                "request_semantic_reinterpretation_allowed": False,
            },
            run_id=run_id,
        )
        return request_need_contract

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

    @staticmethod
    def _lift_worker_call_shape_echo(payload: dict[str, Any]) -> None:
        """Normalize the model echoing the prompt's shape example as a wrapper.

        This is structural-only normalization: it does not add, remove, rename,
        or reinterpret any Worker or business-data name.  It prevents the single repair budget
        from being spent only on removing ``required_output_shape`` before the
        real semantic contract error can be surfaced.
        """

        if isinstance(payload.get("worker_calls"), list):
            return
        echoed = payload.get("required_output_shape")
        if not isinstance(echoed, dict) or not isinstance(echoed.get("worker_calls"), list):
            return
        payload["worker_calls"] = echoed["worker_calls"]
        if not str(payload.get("selection_reason") or "").strip():
            payload["selection_reason"] = str(echoed.get("selection_reason") or "").strip()

    def _select_worker_calls(
        self,
        *,
        request_need_contract: dict[str, Any],
        worker_descriptions: list[dict[str, Any]],
        effect_limit: str,
        run_id: str,
        initial_context_names: set[str],
        recovery_context: dict[str, Any] | None = None,
        planning_gap_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Select Worker calls only from the request need contract + public descriptions."""

        worker_by_id = {str(row["worker_id"]): row for row in worker_descriptions}
        required_need_ids = {
            str(row["need_id"])
            for row in request_need_contract.get("needs") or []
            if bool(row.get("required", True))
        }

        def validate(payload: dict[str, Any]) -> None:
            self._lift_worker_call_shape_echo(payload)
            calls = payload.get("worker_calls")
            if not isinstance(calls, list) or not calls:
                raise WorkerContractViolation("worker_calls_required", "$.worker_calls")
            covered: set[str] = set()
            seen_call_ids: set[str] = set()
            for index, raw in enumerate(calls):
                if not isinstance(raw, dict):
                    raise WorkerContractViolation("worker_call_not_object", f"$.worker_calls[{index}]")
                worker_id = str(raw.get("worker_id") or "").strip().upper()
                if worker_id not in worker_by_id:
                    raise WorkerContractViolation("unknown_worker_call", f"$.worker_calls[{index}].worker_id", worker_id)
                call_id = str(raw.get("call_id") or f"WC{index+1:02d}").strip()
                if call_id in seen_call_ids:
                    raise WorkerContractViolation("duplicate_worker_call_id", "$.worker_calls")
                seen_call_ids.add(call_id)
                need_ids = {str(item) for item in raw.get("covers_need_ids") or [] if str(item)}
                unknown_needs = need_ids - {str(row["need_id"]) for row in request_need_contract.get("needs") or []}
                if unknown_needs:
                    raise WorkerContractViolation("worker_call_unknown_need", f"$.worker_calls[{index}].covers_need_ids", ",".join(sorted(unknown_needs)))
                covered.update(need_ids)
                desired_data_names = {str(item) for item in raw.get("desired_output_data_names") or [] if str(item)}
                if not desired_data_names:
                    raise WorkerContractViolation("worker_call_output_data_required", f"$.worker_calls[{index}].desired_output_data_names")
                unsupported = {
                    name for name in desired_data_names
                    if not self._worker_supports_output(worker_by_id[worker_id], name)
                }
                if unsupported:
                    raise WorkerContractViolation(
                        "worker_call_output_outside_worker",
                        f"$.worker_calls[{index}].desired_output_data_names",
                        self._worker_output_contract_error_detail(worker_by_id[worker_id], unsupported),
                    )
            missing_needs = sorted(required_need_ids - covered)
            if missing_needs:
                raise WorkerContractViolation("required_request_need_uncovered", "$.worker_calls", ",".join(missing_needs))
            # Covers_need_ids is only a responsibility claim. For V23.0.10
            # request Needs, the selected calls must also prove that every
            # required Need output semantic is produced by a covering Worker.
            self.need_compiler.validate_worker_call_need_outputs(
                request_need_contract=request_need_contract,
                worker_calls=[dict(item) for item in calls if isinstance(item, dict)],
            )

        user_payload: dict[str, Any] = {
            "request_need_contract": request_need_contract,
            "available_initial_context_names": sorted(initial_context_names),
            "worker_descriptions": worker_descriptions,
            "required_output_shape": {
                "worker_calls": [{
                    "call_id": "WC01",
                    "worker_id": "Wxx from worker_descriptions",
                    "objective": "该Worker在本轮承担的业务目标",
                    "covers_need_ids": ["N01"],
                    "desired_output_data_names": ["符合该Worker produced_data_patterns 的稳定业务数据名称；已有能力优先复用output_data_examples"],
                }],
                "selection_reason": "只解释request_need_contract中的Need如何被这些Worker覆盖",
            },
        }
        if recovery_context:
            user_payload["bounded_recovery_context"] = recovery_context
        if planning_gap_context:
            user_payload["planning_gap_context"] = planning_gap_context
        selection_stage = (
            "planning_gap_worker_call_repair" if planning_gap_context
            else "recovery_worker_call_selection" if recovery_context
            else "upfront_worker_call_selection"
        )
        planning_gap_instruction = (
            "当前是Planning Gap能力补全，不是重新规划业务意图。planning_gap_context中的existing_worker_calls必须保留，"
            "不得删除其Worker或既有desired_output_data_names；允许从公开Worker descriptions中增加必要的支持Worker，"
            "使存在producer_candidate_worker_ids的missing required 数据获得生产者。新增支持Worker可以covers_need_ids=[]，"
            "因为它服务的是既有业务Worker的输入合同，而不是新增用户Need。业务数据通过ContextBundle共享，不得把消费者Worker与某个生产者Worker ID写成固定绑定。"
            if planning_gap_context else ""
        )
        payload = self.llm_service.generate_json(
            stage=selection_stage,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是MainAgent的Worker委派阶段。request_need_contract中的request_objective、request_target和constraints来自已规范化Request，属于权威输入；禁止重新解释、扩大或缩小。"
                        "你现在一次性看到所有可用Worker的完整公开description。每个Worker是一块完整的专业能力范围，不是一组需要你继续挑选的子能力。"
                        "使用delegation_description、delegate_when、produced_data_patterns、output_data_examples、output_publication_mode和private_tool_semantic_outputs判断应该委派给谁。private_tool_semantic_outputs只公开该Worker可确定性产出的业务数据名称，不暴露Tool身份或参数。"
                        "produced_data_patterns是硬命名合同，不是主题提示。output_publication_mode=worker_synthesized时允许创建新数据名称，但每个新Key必须字面匹配至少一个produced_data_patterns：例如risk.*只能生成risk.xxx，analysis.risk*只能生成analysis.risk开头的Key；不能改写成concentration_risk_fragment这类命名。没有通配符的pattern只能原样使用。已有能力优先复用output_data_examples。private_tool_passthrough仍只能从private_tool_semantic_outputs选择。"
                        "required_output_shape只是格式示意，不得把它作为返回包装层；顶层必须直接返回worker_calls和selection_reason。"
                        "选择能够覆盖全部required need的最小充分Worker调用集合。每个required need必须由covers_need_ids显式覆盖。"
                        "request_need_contract中的每个Need已经包含程序校验过的requirements。对direction=output的requirement，"
                        "负责覆盖该Need的WorkerCall集合必须在desired_output_data_names中真实产出对应data_name；covers_need_ids本身不等于业务完成。"
                        "direction=input/parameter不要在本阶段重复转换成合同，下一阶段只负责把这些已注册Requirement分配给已选Worker。"
                        "Business Request内部不选择结果写作Worker；用户可读报告由Bundle-level报告阶段统一生成。"
                        "entity_analysis Worker只负责分析目标实体，Runtime会从本轮Working Memory提供已查询数据，因此不要为了给它绑定上游数据绑定而制造额外Worker依赖。"
                        + planning_gap_instruction +
                        "不要选择Tool，不要生成DAG，不要输出私有Prompt。只输出JSON。"
                    ),
                },
                {"role": "user", "content": compact_json_dumps(user_payload)},
            ],
            max_output_tokens=1800,
            validator=validate,
            operation=f"upfront_worker_calls:{effect_limit}",
            disable_thinking=False,
            repair_mode="targeted",
            repair_guidance=(
                ("Planning Gap修复时必须保留existing_worker_calls及其既有desired_output_data_names，只允许增加解决缺口所需的Worker/数据名称；" if planning_gap_context else "")
                + "只修复need覆盖、Worker公开产出业务数据和Worker ID；不得重新解释用户请求。"
                "若validation_error.contract_code=worker_call_output_outside_worker，必须读取validation_error.detail中的"
                "worker_id、invalid_data_names、produced_data_patterns、output_data_examples和output_publication_mode："
                "worker_synthesized可在pattern命名空间内重新命名/创建Key；private_tool_passthrough只能选择private_tool_semantic_outputs；"
                "不得原样保留不匹配pattern的数据名称。"
            ),
        )
        calls: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.get("worker_calls") or [], start=1):
            row = dict(raw or {})
            calls.append({
                "call_id": f"WC{index:02d}",
                "worker_id": str(row.get("worker_id") or "").strip().upper(),
                "objective": str(row.get("objective") or "").strip(),
                "covers_need_ids": list(dict.fromkeys(str(item) for item in row.get("covers_need_ids") or [] if str(item))),
                "desired_output_data_names": list(dict.fromkeys(str(item) for item in row.get("desired_output_data_names") or [] if str(item))),
            })
        normalized = {"worker_calls": calls, "selection_reason": str(payload.get("selection_reason") or "").strip()}
        validate(normalized)
        flow_event(
            "UPFRONT_WORKER_CALLS_SELECTED",
            {
                "worker_call_count": len(calls),
                "worker_ids": [row["worker_id"] for row in calls],
                "need_coverage": {row["call_id"]: row["covers_need_ids"] for row in calls},
                "selection_reason": normalized["selection_reason"][:1200],
                "raw_request_used": False,
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
        request_target: dict[str, Any] | None = None,
        request_constraints: list[str] | None = None,
        reuse_candidates: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """合并规划：一次 LLM 调用同时生成 Need 清单并为每个 Need 声明 binding。

        每个 Need 生成时直接二选一声明：
        - worker_binding：走计算路线（选 Worker + 调用目标 + 计划产出名）
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
        allowed_turn_ids = {
            str(item.get("turn_id") or "")
            for item in list(reuse_candidates or [])
            if str(item.get("turn_id") or "").strip()
        }

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
                    worker_id = str(worker_binding.get("worker_id") or "").strip().upper()
                    if worker_id not in worker_by_id:
                        raise WorkerContractViolation("unknown_worker_call", f"{path}.binding.worker_binding.worker_id", worker_id)
                    desired_names = {str(item) for item in worker_binding.get("desired_output_data_names") or [] if str(item)}
                    if not desired_names:
                        raise WorkerContractViolation("worker_call_output_data_required", f"{path}.binding.worker_binding.desired_output_data_names")
                    unsupported = {
                        name for name in desired_names
                        if not self._worker_supports_output(worker_by_id[worker_id], name)
                    }
                    if unsupported:
                        raise WorkerContractViolation(
                            "worker_call_output_outside_worker",
                            f"{path}.binding.worker_binding.desired_output_data_names",
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
                    if freshness == "latest":
                        # latest 意图禁任何复用绑定
                        raise WorkerContractViolation("reuse_binding_forbidden_for_latest", f"{path}.binding.reuse_binding")
                    if effect_limit == "proposal":
                        # Proposal 请求必须真实计算产出方案，禁止复用
                        raise WorkerContractViolation("reuse_binding_forbidden_for_proposal", f"{path}.binding.reuse_binding")
            if effect_limit == "proposal" and not proposal_output_seen:
                raise WorkerContractViolation(
                    "proposal_request_missing_proposal_output_need",
                    "$.needs",
                    "proposal-capable READ Request must contain a Need whose output is a proposal/rebalance result",
                )

        semantic_catalog = self.registry.semantic_requirement_catalog()
        authoritative_constraints = list(dict.fromkeys(
            str(item).strip() for item in (request_constraints or []) if str(item).strip()
        ))
        authoritative_target = dict(request_target or {})
        candidate_view = [
            {
                "turn_id": str(item.get("turn_id") or ""),
                "trade_date": str(item.get("trade_date") or ""),
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
                        "request_objective、request_target、request_constraints是当前Request的权威语义，禁止重新改写、概括、扩大或缩小。"
                        "你的职责是一次性完成两件事：把完成该Request所必需的信息/分析/方案需求拆成少量、明确的needs；"
                        "并在生成每个Need时直接声明它的执行方式binding。"
                        "每个business Need必须至少包含一个direction=output的requirement。requirements只允许从semantic_requirement_catalog选择semantic_key；"
                        "不得自行发明数据名称/参数名。direction=input表示完成本Need必须先具备的系统事实；direction=output表示本Need希望系统产生的业务结果；"
                        "direction=parameter只用于用户必须明确决定、系统不可替用户决定的情景参数。"
                        "用户问‘应该怎么调整/应该配多少’时，目标仓位通常是系统应产生的output，不是用户parameter。"
                        "通用证券分析不得自行扩大用户要求。Business Request内部不要声明user_report/user_facing_report作为业务输出。"
                        "binding二选一必填：走计算路线填worker_binding（worker_id必须来自worker_descriptions，"
                        "desired_output_data_names必须字面匹配该Worker的produced_data_patterns硬命名合同，已有能力优先复用output_data_examples，"
                        "private_tool_passthrough只能从private_tool_semantic_outputs选择）；"
                        "走复用路线填reuse_binding（turn_id只能引用reuse_candidates中出现的编号，derive_args只声明派生方向如{\"top_n\":5}，"
                        "不需要也不允许声明产出名，产出由系统从共享存储注册）。两者不能同时填，也不能同时空。"
                        "reuse_candidates是历史轮次的完整回答裁剪版：只有当你确认某轮回答的内容确实满足该Need的分析方向时才允许reuse_binding；"
                        "方向不同、内容不满足、或freshness_expectation=latest时禁止reuse_binding。"
                        "多个Need可以绑定同一个Worker；一个Need只能有一个binding。"
                        "不要选择Tool，不要生成DAG，不要输出私有Prompt。只输出JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "request_id": str(request_id or ""),
                        "request_objective": str(query or "").strip(),
                        "request_target": authoritative_target,
                        "request_constraints": authoritative_constraints,
                        "effect_limit": effect_limit,
                        "reply_language": language,
                        "context_binding": binding_context,
                        "available_context_kinds": sorted(initial_context_names),
                        "authoritative_entity_refs_available": "authoritative_entity_refs" in initial_context_names,
                        "session_summary": str(memory_summary or "")[:1400],
                        "semantic_requirement_catalog": semantic_catalog,
                        "worker_descriptions": worker_descriptions,
                        "reuse_candidates": candidate_view,
                        "required_output_shape": {
                            "needs": [{
                                "description": "完成当前Request所必需的一个信息/分析/方案需求",
                                "required": True,
                                "requirements": [{
                                    "semantic_key": "must come from semantic_requirement_catalog",
                                    "direction": "input|output|parameter",
                                    "required": True,
                                    "required_paths": [],
                                }],
                                "binding": {
                                    "worker_binding": {
                                        "worker_id": "Wxx from worker_descriptions",
                                        "objective": "该Worker在本轮承担的业务目标",
                                        "desired_output_data_names": ["符合该Worker produced_data_patterns 的稳定业务数据名称"],
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
                "worker_binding的worker_id必须来自worker_descriptions且产出名匹配produced_data_patterns；"
                "reuse_binding的turn_id只能引用reuse_candidates中的编号，latest意图与proposal请求禁止reuse_binding；"
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
            binding = dict(row.get("binding") or {})
            if isinstance(binding.get("worker_binding"), dict) and binding.get("worker_binding"):
                worker_binding = dict(binding["worker_binding"])
                binding_annotation = {
                    "type": "worker",
                    "worker_id": str(worker_binding.get("worker_id") or "").strip().upper(),
                    "objective": str(worker_binding.get("objective") or "").strip(),
                    "desired_output_data_names": list(dict.fromkeys(
                        str(item) for item in worker_binding.get("desired_output_data_names") or [] if str(item)
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
                reuse_decisions.append({
                    "need_id": need_id,
                    "turn_id": binding_annotation["turn_id"],
                    "derive_args": dict(derive_args),
                })
            normalized_needs.append({
                "need_id": need_id,
                "request_id": str(request_id or ""),
                "kind": "business",
                "description": str(row.get("description") or "").strip(),
                "required": bool(row.get("required", True)),
                "requirements": self.need_compiler.normalize_need_requirements(
                    need_id=need_id,
                    raw_requirements=raw_requirements,
                    strict=True,
                ),
                "binding": binding_annotation,
            })

        full_need_contract = {
            "schema_version": "request_need_contract.v1",
            "request_id": str(request_id or ""),
            "request_objective": str(query or "").strip(),
            "request_target": authoritative_target,
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
        TaskDependencyCompiler derives execution order only; ContextBundle carries business data.

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
                    "business_data_transport": "context_bundle_working_memory",
                    "initial_runtime_context_names": sorted(initial_context_names),
                },
            ))
        meta = {
            "planner": "need_worker_assignment_runtime_compiler",
            "runtime_version": RUNTIME_VERSION,
            "planning_mode": "merged_need_binding_then_runtime_dependency_compile_then_private_tool_dag",
            "worker_selection_owner": "main_agent",
            "main_agent_llm_planning_stages": ["upfront_merged_need_worker_planning"],
            "worker_dag_build_owner": "runtime_deterministic_compiler",
            "worker_private_planning_owner": "specialist_worker",
            "business_data_owner": "context_bundle_working_memory",
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
        request_target: dict[str, Any] | None = None,
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
                request_target=request_target,
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
                "reason": "business data sufficiency is evaluated by specialist Workers against ContextBundle",
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
    ) -> tuple[list[GraphAgentTask], list[GraphAgentTask], dict[str, Any]]:
        del query, language
        request_need_contract = self._request_need_contract_from_tasks(current_tasks)
        if not request_need_contract:
            raise WorkerContractViolation("replan_missing_request_need_contract", "$.task.metadata.request_need_contract")

        reusable_ids = {
            task_id for task_id, result in current_results.items()
            if result.status in {ResultStatus.COMPLETED, ResultStatus.PROPOSAL_READY}
            and bool((result.completion or {}).get("expected_task_completed", True))
        }
        frozen = [task for task in current_tasks if task.task_id in reusable_ids]
        task_id_prefix = str((current_tasks[0].metadata or {}).get("task_id_prefix") or "") if current_tasks else ""
        initial_context_names = self._initial_context_names(
            focus_refs=focus_refs, context_refs=context_refs, memory_summary=memory_summary,
            extra_context_names=set(),
        )
        failure_signatures = []
        for item in observations[:20]:
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
        recovery_context = {
            "round": int(replan_round),
            "failure_signatures": failure_signatures,
            "working_memory_reuse": True,
            "instruction": "只修复失败能力，保持request_need_contract不变；已成功业务数据继续从ContextBundle复用。",
        }
        descriptions = self._load_worker_descriptions(effect_limit=effect_limit, run_id=run_id)
        worker_call_plan = self._select_worker_calls(
            request_need_contract=request_need_contract, worker_descriptions=descriptions, effect_limit=effect_limit,
            run_id=run_id, initial_context_names=initial_context_names, recovery_context=recovery_context,
        )
        payload, capability_tasks = self._generate_worker_dag(
            request_need_contract=request_need_contract, worker_call_plan=worker_call_plan,
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
                "request_need_contract": request_need_contract,
                "worker_call_plan": worker_call_plan,
                "worker_description_count": len(descriptions),
            },
        )
        full = [*frozen, *new_tasks]
        meta.update({
            "replan_round": int(replan_round),
            "recovery_only": True,
            "request_need_contract_reused": True,
            "working_memory_reused": True,
            "frozen_task_ids": [task.task_id for task in frozen],
            "new_task_ids": [task.task_id for task in new_tasks],
            "failure_signatures": failure_signatures,
        })
        return full, new_tasks, meta
