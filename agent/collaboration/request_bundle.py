from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.llm import LLMService
from core.llm.prompt_compaction import compact_json_dumps

from .context_binding import ContextBinding, EntityScope, FreshnessExpectation, ReferenceEntityType


# 实体提及角色六枚举（decompose 吸收实体提取后的合法角色集）
MENTION_ROLES = {"focus", "comparison", "cause", "impact_target", "context", "event"}


class RequestCategory(str, Enum):
    BUSINESS = "business"
    PRESENTATION = "presentation"


class RequestType(str, Enum):
    READ = "read"
    WRITE = "write"


class RequestStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    WAITING_CONTEXT = "waiting_context"
    WAITING_USER_INPUT = "waiting_user_input"
    WAITING_APPROVAL = "waiting_approval"
    UNSUPPORTED = "unsupported"
    TOOL_FAILED = "tool_failed"
    BUSINESS_EMPTY = "business_empty"
    BLOCKED = "blocked"
    PRESENTATION_APPLIED = "presentation_applied"
    FAILED = "failed"


class RequestBundleError(RuntimeError):
    pass


@dataclass
class PresentationRequest:
    language: str = ""
    style: str = ""
    length: str = ""
    format: str = ""
    scope: str = "current_turn"
    persist: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "style": self.style,
            "length": self.length,
            "format": self.format,
            "scope": self.scope,
            "persist": bool(self.persist),
        }


@dataclass
class RequestItem:
    request_id: str
    source_index: int
    category: RequestCategory
    objective: str
    request_type: RequestType = RequestType.READ
    proposal_required: bool = False
    target: dict[str, Any] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    scope: str = "current_turn"
    status: RequestStatus = RequestStatus.PENDING
    status_reason: str = ""
    action_type: str = ""
    presentation: PresentationRequest | None = None
    context_binding: ContextBinding = field(default_factory=ContextBinding)
    mentions: list[dict[str, Any]] = field(default_factory=list)  # 金融实体提及（decompose 吸收实体提取，元素为 {"text","role"}）

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_index": int(self.source_index),
            "category": self.category.value,
            "objective": self.objective,
            "request_type": self.request_type.value if self.category == RequestCategory.BUSINESS else "",
            "proposal_required": bool(self.proposal_required),
            "target": dict(self.target),
            "constraints": list(self.constraints),
            "depends_on": list(self.depends_on),
            "scope": self.scope,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "action_type": self.action_type,
            "presentation": self.presentation.to_dict() if self.presentation else None,
            "context_binding": self.context_binding.to_dict(),
            "mentions": [dict(item) for item in self.mentions],
        }


@dataclass
class RequestBundle:
    requests: list[RequestItem]
    raw_message: str
    schema_version: str = "request_bundle.v2"
    decomposition_source: str = "deterministic_structure+llm_semantics+program_validator"
    reuse_guards: dict[str, Any] = field(default_factory=dict)  # 复用校验保护面（按显示编号映射真实轮次，仅服务端持有，不进 prompt）

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "raw_message": self.raw_message,
            "request_count": len(self.requests),
            "decomposition_source": self.decomposition_source,
            "requests": [item.to_dict() for item in self.requests],
        }

    def business_requests(self) -> list[RequestItem]:
        return [item for item in self.requests if item.category == RequestCategory.BUSINESS]

    def read_requests(self) -> list[RequestItem]:
        return [
            item for item in self.requests
            if item.category == RequestCategory.BUSINESS and item.request_type == RequestType.READ
        ]

    def write_requests(self) -> list[RequestItem]:
        return [
            item for item in self.requests
            if item.category == RequestCategory.BUSINESS and item.request_type == RequestType.WRITE
        ]

    def presentation_requests(self) -> list[RequestItem]:
        return [item for item in self.requests if item.category == RequestCategory.PRESENTATION]


_NUMBERED_RE = re.compile(r"^\s*(\d{1,3})\s*[\.、\)）:]\s*(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")


def deterministic_structure_parse(query: str) -> list[dict[str, Any]]:
    """Preserve explicit user task boundaries without interpreting semantics."""

    rows: list[dict[str, Any]] = []
    auto_index = 1
    for raw_line in str(query or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered:
            rows.append({
                "source_index": int(numbered.group(1)),
                "text": numbered.group(2).strip(),
                "boundary_source": "explicit_number",
            })
            auto_index = max(auto_index, int(numbered.group(1)) + 1)
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            rows.append({
                "source_index": auto_index,
                "text": bullet.group(1).strip(),
                "boundary_source": "explicit_bullet",
            })
            auto_index += 1
    if rows:
        return rows
    return [{"source_index": 1, "text": str(query or "").strip(), "boundary_source": "whole_message"}]


def _relation_type(context: dict[str, Any] | None) -> str:
    raw = dict(context or {})
    state = raw.get("conversation_state") if isinstance(raw.get("conversation_state"), dict) else {}
    turn = raw.get("turn_resolution") if isinstance(raw.get("turn_resolution"), dict) else {}
    return str(state.get("relation_type") or turn.get("relation_type") or raw.get("relation_type") or "").lower()


class RequestBundleValidator:
    ALLOWED_WRITE_ACTIONS = {"confirm_execute", "reject", "cancel"}
    ALLOWED_PRESENTATION_SCOPES = {"request", "whole_bundle", "current_turn", "session"}
    ALLOWED_LANGUAGES = {"", "zh", "en"}

    def validate(self, bundle: RequestBundle) -> RequestBundle:
        if not bundle.requests:
            raise RequestBundleError("request_bundle_empty")
        ids = [item.request_id for item in bundle.requests]
        if len(ids) != len(set(ids)):
            raise RequestBundleError("request_id_not_unique")
        known = set(ids)
        for item in bundle.requests:
            if not item.objective.strip() and item.category != RequestCategory.PRESENTATION:
                raise RequestBundleError(f"request_objective_required:{item.request_id}")
            if item.category == RequestCategory.BUSINESS:
                if item.request_type == RequestType.READ:
                    if item.action_type:
                        raise RequestBundleError(f"read_request_cannot_have_write_action:{item.request_id}")
                elif item.request_type == RequestType.WRITE:
                    if item.proposal_required:
                        raise RequestBundleError(f"write_request_cannot_plan_proposal:{item.request_id}")
                    if item.action_type not in self.ALLOWED_WRITE_ACTIONS:
                        raise RequestBundleError(f"invalid_write_action:{item.request_id}:{item.action_type}")
                else:
                    raise RequestBundleError(f"invalid_request_type:{item.request_id}")
            elif item.category == RequestCategory.PRESENTATION:
                if item.presentation is None:
                    raise RequestBundleError(f"presentation_fields_required:{item.request_id}")
                if item.presentation.scope not in self.ALLOWED_PRESENTATION_SCOPES:
                    raise RequestBundleError(f"invalid_presentation_scope:{item.request_id}")
                if item.presentation.language not in self.ALLOWED_LANGUAGES:
                    raise RequestBundleError(f"invalid_presentation_language:{item.request_id}")
                if not any([
                    item.presentation.language,
                    item.presentation.style,
                    item.presentation.length,
                    item.presentation.format,
                ]):
                    raise RequestBundleError(f"presentation_fields_empty:{item.request_id}")
            unknown = [dep for dep in item.depends_on if dep not in known]
            if unknown:
                raise RequestBundleError(f"request_dependency_unknown:{item.request_id}:{','.join(unknown)}")
            if item.request_id in item.depends_on:
                raise RequestBundleError(f"request_self_dependency:{item.request_id}")
        self._validate_acyclic(bundle.requests)
        return bundle

    @staticmethod
    def _validate_acyclic(items: list[RequestItem]) -> None:
        deps = {item.request_id: list(item.depends_on) for item in items}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(request_id: str) -> None:
            if request_id in visited:
                return
            if request_id in visiting:
                raise RequestBundleError(f"request_dependency_cycle:{request_id}")
            visiting.add(request_id)
            for dep in deps.get(request_id, []):
                visit(dep)
            visiting.remove(request_id)
            visited.add(request_id)

        for request_id in deps:
            visit(request_id)


class RequestDecomposer:
    """Decompose one message into a validated RequestBundle.

    Structure comes from deterministic parsing, semantics from one LLM call, and
    protocol legality from program validation. The LLM never creates Worker,
    Need, Tool or Capability identifiers here.
    """

    def __init__(self, *, llm_service: LLMService) -> None:
        self.llm_service = llm_service
        self.validator = RequestBundleValidator()

    def decompose(
        self,
        *,
        query: str,
        memory_summary: str,
        execution_context: dict[str, Any] | None,
        language: str,
        run_id: str,
        reuse_candidates: dict[str, Any] | None = None,
    ) -> RequestBundle:
        structural = deterministic_structure_parse(query)
        relation = _relation_type(execution_context)
        # 复用候选显示编号集（校验 reuse_reference 防幻觉引用的依据）
        candidate_turn_ids = {
            str(item.get("turn_id") or "")
            for item in list((reuse_candidates or {}).get("items") or [])
            if str(item.get("turn_id") or "").strip()
        }
        candidate_lines = [
            "- {turn_id} [{freshness}] 用户: {user_summary} | 回答: {assistant_summary}".format(
                turn_id=str(item.get("turn_id") or ""),
                freshness=str(item.get("freshness") or "non_trade"),
                user_summary=str(item.get("user_summary") or ""),
                assistant_summary=str(item.get("assistant_summary") or ""),
            )
            for item in list((reuse_candidates or {}).get("items") or [])
        ]

        def validate_payload(payload: dict[str, Any]) -> None:
            rows = payload.get("requests")
            if not isinstance(rows, list) or not rows:
                raise RequestBundleError("request_decomposer_requests_required")
            if len(rows) > 20:
                raise RequestBundleError("request_bundle_too_large")
            allowed_categories = {item.value for item in RequestCategory}
            for index, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    raise RequestBundleError(f"request_item_not_object:{index}")
                category = str(raw.get("category") or "").strip().lower()
                if category not in allowed_categories:
                    raise RequestBundleError(f"invalid_request_category:{index}:{category}")
                if category == RequestCategory.BUSINESS.value and not str(raw.get("objective") or "").strip():
                    raise RequestBundleError(f"request_objective_required:{index}")
                if raw.get("target") is not None and not isinstance(raw.get("target"), dict):
                    raise RequestBundleError(f"request_target_must_be_object:{index}")
                if raw.get("constraints") is not None and not isinstance(raw.get("constraints"), list):
                    raise RequestBundleError(f"request_constraints_must_be_array:{index}")
                if raw.get("depends_on") is not None and not isinstance(raw.get("depends_on"), list):
                    raise RequestBundleError(f"request_depends_on_must_be_array:{index}")
                forbidden_planning_fields = {
                    "need", "needs", "worker", "workers", "tool", "tools",
                    "capability", "capabilities", "task", "tasks", "steps", "task_dag",
                }
                leaked = sorted(forbidden_planning_fields.intersection(str(key).lower() for key in raw))
                if leaked:
                    raise RequestBundleError(
                        f"request_decomposer_planning_fields_forbidden:{index}:{','.join(leaked)}"
                    )
                if category == RequestCategory.BUSINESS.value:
                    request_type = str(raw.get("request_type") or "read").lower()
                    if request_type not in {"read", "write"}:
                        raise RequestBundleError(f"invalid_business_request_type:{index}")
                    action_type = str(raw.get("action_type") or "").lower()
                    if request_type == "write" and action_type not in {"confirm_execute", "reject", "cancel"}:
                        raise RequestBundleError(f"invalid_write_action:{index}")
                    if request_type == "read" and action_type:
                        raise RequestBundleError(f"read_request_has_write_action:{index}")
                binding = raw.get("context_binding")
                if not isinstance(binding, dict):
                    raise RequestBundleError(f"request_context_binding_required:{index}")
                if str(binding.get("entity_scope") or "none") not in {item.value for item in EntityScope}:
                    raise RequestBundleError(f"invalid_request_entity_scope:{index}")
                if str(binding.get("reference_entity_type") or "none") not in {item.value for item in ReferenceEntityType}:
                    raise RequestBundleError(f"invalid_request_reference_type:{index}")
                # 复用字段校验（新增三条 + 写请求拦截）
                freshness = str(binding.get("freshness_expectation") or FreshnessExpectation.UNSPECIFIED.value)
                if freshness not in {item.value for item in FreshnessExpectation}:
                    raise RequestBundleError(f"invalid_freshness_expectation:{index}:{freshness}")
                reuse_reference = binding.get("reuse_reference")
                if reuse_reference is not None and not isinstance(reuse_reference, list):
                    raise RequestBundleError(f"reuse_reference_must_be_array:{index}")
                reuse_ids = [str(value or "").strip() for value in (reuse_reference or []) if str(value or "").strip()]
                if freshness in {FreshnessExpectation.LATEST.value, FreshnessExpectation.UNSPECIFIED.value} and reuse_ids:
                    # 交叉一致性：latest/unspecified 意图下禁止标记复用引用
                    raise RequestBundleError(f"reuse_reference_forbidden_for_freshness:{index}:{freshness}")
                unknown_reuse = sorted(set(reuse_ids) - candidate_turn_ids)
                if unknown_reuse:
                    # 防幻觉：reuse_reference 必须来自注入的候选集
                    raise RequestBundleError(f"reuse_reference_unknown_turn:{index}:{','.join(unknown_reuse)}")
                if category == RequestCategory.BUSINESS.value and str(raw.get("request_type") or "read").lower() == "write" and reuse_ids:
                    # 写请求永不复用
                    raise RequestBundleError(f"write_request_cannot_reuse:{index}")
                # 实体提及校验（decompose 吸收实体提取）
                mentions = raw.get("mentions")
                if mentions is not None:
                    if not isinstance(mentions, list):
                        raise RequestBundleError(f"request_mentions_must_be_array:{index}")
                    if len(mentions) > 20:
                        raise RequestBundleError(f"too_many_entity_mentions:{index}")
                    for mention in mentions:
                        if not isinstance(mention, dict) or not str(mention.get("text") or "").strip():
                            raise RequestBundleError(f"invalid_entity_mention:{index}")
                        if str(mention.get("role") or "focus") not in MENTION_ROLES:
                            raise RequestBundleError(f"invalid_entity_role:{index}")

        payload = self.llm_service.generate_json(
            stage="request_bundle_decomposition",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是MainAgent入口的Request Decomposer。你的唯一职责是把一次用户消息拆成用户真正要求完成的Request清单，"
                        "不是拆Need、不是选择Worker、不是规划Tool。一个输入可以包含多个同类BUSINESS Request；Request不等于Worker Task。"
                        "category只能是business、presentation。Business Request顶层request_type只能是read或write。"
                        "查询、分析、比较、风险判断、生成建议、生成或修订待审批Proposal全部属于READ；"
                        "即使用户说‘加入/减仓/修改持仓’，只要本轮还没有明确确认已有Proposal，就仍是READ，并设置proposal_required=true。"
                        "只有用户明确确认/授权执行一个已有Proposal时才是WRITE，action_type=confirm_execute；"
                        "拒绝或取消已有Proposal也走确定性WRITE控制动作，action_type=reject|cancel。语言、风格、长度、格式属于presentation。"
                        "unsupported不是category；若某一项明显超出金融Agent能力，可把status写unsupported并给reason，其余Request仍保留。"
                        "如果用户明确分条，必须尊重deterministic_segments的source_index和原始边界；未显式分条时可以按语义拆成多个Request。"
                        "你必须在这一次拆分调用中直接完成objective业务语义规范化：objective应是明确、稳定、无歧义、可直接交给MainAgent做Need Decomposition的业务目标。"
                        "去除‘帮我看看/给我瞅瞅/最近咋样/我感觉有点危险’等口语或无业务价值表达，转换为分析、查询、评估、比较、获取、生成等明确业务动作，但绝不能改变或扩大用户真实目标。"
                        "objective只描述‘要完成什么’，不要把target、constraints、presentation、Worker、Tool、Need、Capability或执行步骤塞进objective。"
                        "例如‘分析贵州茅台最近一个月的风险，只看最近一个月’应拆为objective=‘分析目标股票风险’，target中保存贵州茅台，constraints中保存最近一个月；"
                        "不能扩写成‘获取行情、新闻、持仓并分析贵州茅台最近一个月风险’，因为那已经提前规划执行步骤。"
                        "用户只要求‘分析持仓集中度风险’时，objective不能扩展为市场风险、流动性风险、行业风险等额外目标。完成该目标所需的内部Need由后续MainAgent决定。"
                        "显式编号/项目符号只决定Request边界和source_index；原始segment不是最终objective。你返回的规范化objective将作为该Request后续语义权威。"
                        "target只保存用户目标对象/业务对象，不保存执行步骤；constraints只保存用户明确约束。展示语言、风格、长度、格式只进入presentation。"
                        "depends_on使用当前输出requests数组中的1-based位置编号，例如第3项依赖第1、2项则写[1,2]；没有依赖写[]。"
                        "proposal_id/token属于Runtime协议状态，不能由你编造；protocol_relation若明确是confirmation/cancellation，request_type/action_type必须服从该协议事实。"
                        "PRESENTATION字段仅允许language/style/length/format/scope/persist；同一句里‘先中文，改成英文’按后一个明确要求为准。"
                        "如果呈现要求只作用于某一个Request，scope=request并在target.request_indexes中用当前requests数组的1-based位置指向目标Request；"
                        "PRESENTATION一般不作为业务执行依赖节点，depends_on只用于真正的执行先后关系；如果作用于整轮回答，使用whole_bundle或current_turn。"
                        "context_binding继续用于GraphRef解析：直接点名证券用explicit_entities/security；‘刚刚那只股票’用conversation_focus、"
                        "inherit_previous_focus=true、security；完整持仓用portfolio/portfolio；无金融实体可用none。"
                        "你还要在同一调用中为每个business Request提取金融实体提及mentions：只提取用户明确指向、且需要进入金融图解析的现实金融对象、新闻/公告/研报或事件。"
                        "portfolio、account、global、none是业务范围，不能把‘我的持仓’、‘当前账户’等范围词误当成单只证券实体；"
                        "只有请求中明确出现具体证券、公司、行业、事件或已命名组合对象时才输出mention；不要从常识补充对象，不要决定最终实体ID；"
                        "没有需要GraphRef解析的明确对象时输出空数组mentions=[]。mention角色只能是focus、comparison、cause、impact_target、context、event。"
                        "reuse_candidates是历史对话轮次摘要清单。你只能根据摘要判断某轮回答‘可能’满足当前Request，"
                        "在context_binding.reuse_reference中标记候选turn编号（可多个），这只是粗筛标记，不是最终复用决定，最终由后续规划确认。"
                        "context_binding.freshness_expectation四态：latest=必须最新数据（此时reuse_reference必须为空）；"
                        "reusable=旧结果可能直接复用；reference=旧结果作为新计算的参考输入；unspecified=未明确（默认值，此时reuse_reference必须为空）。"
                        "reuse_reference只能引用reuse_candidates中出现过的turn_id，禁止编造；write类型Request禁止标记reuse_reference；"
                        "reuse_candidates为空时不得标记任何reuse_reference。"
                        "严格输出JSON，不得输出Worker ID、Need ID、Tool、Capability或实现步骤。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "user_message": str(query or ""),
                        "deterministic_segments": structural,
                        "protocol_relation": relation,
                        "session_summary": str(memory_summary or "")[:3000],
                        "reuse_candidates": candidate_lines,
                        "current_reply_language": language,
                        "required_output_shape": {
                            "requests": [{
                                "source_index": 1,
                                "category": "business|presentation",
                                "objective": "已规范化、只描述要完成什么的业务目标",
                                "request_type": "read|write",
                                "proposal_required": False,
                                "target": {"business_object": "用户明确目标对象；展示Request可使用request_indexes"},
                                "constraints": ["仅用户明确提出的约束"],
                                "depends_on": [1],
                                "scope": "current_turn",
                                "status": "pending|unsupported",
                                "reason": "",
                                "action_type": "confirm_execute|reject|cancel|",
                                "mentions": [{"text": "具体金融对象", "role": "focus|comparison|cause|impact_target|context|event"}],
                                "presentation": {
                                    "language": "zh|en|",
                                    "style": "",
                                    "length": "",
                                    "format": "",
                                    "scope": "request|whole_bundle|current_turn|session",
                                    "persist": False,
                                },
                                "context_binding": {
                                    "entity_scope": "explicit_entities|conversation_focus|portfolio|account|global|none",
                                    "inherit_previous_focus": False,
                                    "reference_entity_type": "security|portfolio|account|event|unknown|none",
                                    "reason": "",
                                    "freshness_expectation": "latest|reusable|reference|unspecified",
                                    "reuse_reference": ["turn_01"],
                                },
                            }]
                        },
                    }),
                },
            ],
            max_output_tokens=8000,
            validator=validate_payload,
            operation="request_bundle_decompose",
            disable_thinking=False,
            repair_mode="targeted",
            repair_guidance=(
                "只修复Request清单协议。category只能business/presentation；Business request_type只能read/write；depends_on使用数组位置；"
                "objective必须是规范化业务目标，target/constraints/presentation必须分离；不得输出Worker、Need、Tool、Capability、Task或执行步骤。"
                "mentions必须是数组且角色只能focus/comparison/cause/impact_target/context/event；"
                "freshness_expectation只能latest/reusable/reference/unspecified，latest或unspecified时reuse_reference必须为空；"
                "reuse_reference只能引用reuse_candidates中的turn_id；write类型Request禁止reuse_reference。"
            ),
        )

        raw_rows = [dict(item) for item in payload.get("requests") or [] if isinstance(item, dict)]
        if not raw_rows:
            raise RequestBundleError("request_bundle_empty_after_llm")
        explicit_source_indexes = {
            int(item["source_index"])
            for item in structural
            if str(item.get("boundary_source") or "").startswith("explicit_")
        }
        if explicit_source_indexes:
            returned_indexes = set()
            for index, raw in enumerate(raw_rows, start=1):
                try:
                    returned_indexes.add(int(raw.get("source_index", index)))
                except (TypeError, ValueError):
                    returned_indexes.add(index)
            missing_structural = sorted(explicit_source_indexes - returned_indexes)
            if missing_structural:
                raise RequestBundleError(
                    "explicit_request_boundary_lost:" + ",".join(str(item) for item in missing_structural)
                )

        # Runtime owns stable request IDs. Dependencies returned as positional
        # indexes are mapped only after IDs are allocated. Explicit numbered/bullet
        # segments remain authoritative only for Request boundaries/source_index;
        # the single decomposition LLM owns the normalized business objective.
        items: list[RequestItem] = []
        for index, raw in enumerate(raw_rows, start=1):
            request_id = f"R{index:02d}"
            category = RequestCategory(str(raw.get("category") or "business").strip().lower())
            status_text = str(raw.get("status") or "pending").strip().lower()
            status = RequestStatus.UNSUPPORTED if status_text == "unsupported" else RequestStatus.PENDING
            source_index = raw.get("source_index", index)
            try:
                source_index = max(1, int(source_index))
            except (TypeError, ValueError):
                source_index = index
            raw_binding = dict(raw.get("context_binding") or {})
            binding = ContextBinding(
                entity_scope=EntityScope(str(raw_binding.get("entity_scope") or EntityScope.NONE.value)),
                inherit_previous_focus=bool(raw_binding.get("inherit_previous_focus")),
                reference_entity_type=ReferenceEntityType(
                    str(raw_binding.get("reference_entity_type") or ReferenceEntityType.NONE.value)
                ),
                reason=str(raw_binding.get("reason") or "")[:500],
                freshness_expectation=FreshnessExpectation(
                    str(raw_binding.get("freshness_expectation") or FreshnessExpectation.UNSPECIFIED.value)
                ),
                reuse_reference=tuple(
                    str(value).strip()
                    for value in raw_binding.get("reuse_reference") or []
                    if str(value or "").strip()
                )[:10],
            )
            # 实体提及（decompose 吸收实体提取）：只保留 text 非空的合法条目
            mentions = [
                {
                    "text": str(mention.get("text") or "").strip(),
                    "role": str(mention.get("role") or "focus").strip(),
                }
                for mention in raw.get("mentions") or []
                if isinstance(mention, dict) and str(mention.get("text") or "").strip()
            ][:20]
            presentation = None
            if category == RequestCategory.PRESENTATION:
                p = dict(raw.get("presentation") or {})
                presentation = PresentationRequest(
                    language=str(p.get("language") or "").strip().lower(),
                    style=str(p.get("style") or "").strip(),
                    length=str(p.get("length") or "").strip(),
                    format=str(p.get("format") or "").strip(),
                    scope=str(p.get("scope") or raw.get("scope") or "current_turn").strip().lower(),
                    persist=bool(p.get("persist")),
                )
            objective = str(raw.get("objective") or "").strip()
            items.append(RequestItem(
                request_id=request_id,
                source_index=source_index,
                category=category,
                objective=objective,
                request_type=(
                    RequestType(str(raw.get("request_type") or "read").strip().lower())
                    if category == RequestCategory.BUSINESS else RequestType.READ
                ),
                proposal_required=(
                    bool(raw.get("proposal_required"))
                    if category == RequestCategory.BUSINESS else False
                ),
                target=dict(raw.get("target") or {}) if isinstance(raw.get("target"), dict) else {},
                constraints=[str(item).strip() for item in raw.get("constraints") or [] if str(item).strip()],
                depends_on=[],
                scope=str(raw.get("scope") or "current_turn").strip().lower(),
                status=status,
                status_reason=str(raw.get("reason") or "")[:500],
                action_type=(
                    str(raw.get("action_type") or "").strip().lower()
                    if category == RequestCategory.BUSINESS else ""
                ),
                presentation=presentation,
                context_binding=binding,
                mentions=mentions,
            ))

        # Positional dependency IDs are intentionally normalized by program.
        for index, (item, raw) in enumerate(zip(items, raw_rows), start=1):
            deps: list[str] = []
            for value in raw.get("depends_on") or []:
                try:
                    position = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= position <= len(items) and position != index:
                    dep_id = items[position - 1].request_id
                    if dep_id not in deps:
                        deps.append(dep_id)
            item.depends_on = deps

            # Request-scoped presentation targeting is not an execution
            # dependency.  The LLM uses positional indexes; Runtime converts
            # them to stable Request IDs after allocation.
            raw_target = dict(raw.get("target") or {}) if isinstance(raw.get("target"), dict) else {}
            target_ids: list[str] = []
            for value in raw_target.get("request_indexes") or []:
                try:
                    position = int(value)
                except (TypeError, ValueError):
                    continue
                if 1 <= position <= len(items) and position != index:
                    request_target_id = items[position - 1].request_id
                    if request_target_id not in target_ids:
                        target_ids.append(request_target_id)
            # Compatibility for callers that already provide stable Request IDs.
            for value in raw_target.get("request_ids") or []:
                request_target_id = str(value or "").strip()
                if request_target_id in {row.request_id for row in items} and request_target_id != item.request_id:
                    if request_target_id not in target_ids:
                        target_ids.append(request_target_id)
            single_target_id = str(raw_target.get("request_id") or "").strip()
            if single_target_id in {row.request_id for row in items} and single_target_id != item.request_id:
                if single_target_id not in target_ids:
                    target_ids.append(single_target_id)
            if target_ids:
                raw_target["request_ids"] = target_ids
            raw_target.pop("request_indexes", None)
            item.target = raw_target

        # Protocol relation is a hard fact. Confirmation never re-enters READ
        # planning: Runtime creates one deterministic WRITE request.
        hard_action = (
            "confirm_execute" if relation == "confirmation"
            else "cancel" if relation == "cancellation"
            else ""
        )
        normalized_query = "".join(str(query or "").strip().lower().split())
        if not hard_action and normalized_query in {
            "确认", "确认执行", "执行刚才方案", "确认刚才方案", "确认执行刚才方案",
            "confirm", "confirmexecute", "approve", "approveandexecute",
        }:
            hard_action = "confirm_execute"
        if not hard_action and normalized_query in {
            "取消刚才方案", "不要刚才方案", "拒绝刚才方案", "取消方案", "拒绝方案",
            "cancel", "reject",
        }:
            hard_action = "cancel" if "取消" in normalized_query or normalized_query == "cancel" else "reject"
        if hard_action:
            business = [item for item in items if item.category == RequestCategory.BUSINESS]
            if business:
                target = business[0]
                target.request_type = RequestType.WRITE
                target.proposal_required = False
                target.action_type = hard_action
                target.context_binding = ContextBinding()
            else:
                for item in items:
                    item.request_id = f"R{int(item.request_id[1:]) + 1:02d}"
                    item.depends_on = [f"R{int(dep[1:]) + 1:02d}" for dep in item.depends_on]
                items.insert(0, RequestItem(
                    request_id="R01",
                    source_index=0,
                    category=RequestCategory.BUSINESS,
                    objective=(
                        "确认并执行已有待审批方案" if hard_action == "confirm_execute"
                        else "拒绝或取消已有待审批方案"
                    ),
                    request_type=RequestType.WRITE,
                    proposal_required=False,
                    action_type=hard_action,
                    context_binding=ContextBinding(),
                ))

        bundle = RequestBundle(requests=items, raw_message=str(query or ""))
        # 复用校验保护面随 bundle 传递（仅服务端使用，不进任何 prompt）
        bundle.reuse_guards = dict((reuse_candidates or {}).get("guards") or {})
        return self.validator.validate(bundle)


__all__ = [
    "PresentationRequest",
    "RequestBundle",
    "RequestBundleError",
    "RequestBundleValidator",
    "RequestCategory",
    "RequestType",
    "RequestDecomposer",
    "RequestItem",
    "RequestStatus",
    "deterministic_structure_parse",
]
