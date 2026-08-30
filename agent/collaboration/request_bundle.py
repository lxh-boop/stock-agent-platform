from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from core.llm import LLMService
from core.llm.prompt_compaction import compact_json_dumps

from .context_binding import ContextBinding, EntityScope, FreshnessExpectation, ReferenceEntityType


SEMANTIC_ENTITY_ROLES = {"focus", "comparison", "cause", "impact_target", "context", "event"}
SEMANTIC_ENTITY_SOURCES = {"explicit", "conversation_context"}
SEMANTIC_TARGET_STATUSES = {"identified", "missing", "not_required"}
SEMANTIC_TARGET_SOURCES = {"explicit", "conversation_context", "none"}


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


@dataclass(frozen=True)
class SemanticEntity:
    """LLM 产生的语义实体；这里只保存自然语言身份，不是权威 GraphRef。"""

    text: str
    entity_type: str = "unknown"
    role: str = "focus"
    source: str = "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "entity_type": self.entity_type,
            "role": self.role,
            "source": self.source,
        }


@dataclass(frozen=True)
class SemanticTarget:
    """进入代码侧权威实体解析之前的语义目标。"""

    entity_type: str = "none"
    display_text: str = ""
    source: str = "none"
    status: str = "not_required"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "display_text": self.display_text,
            "source": self.source,
            "status": self.status,
        }


@dataclass
class PresentationRequest:
    language: str = ""
    style: str = ""
    length: str = ""
    format: str = ""
    scope: str = "current_turn"
    persist: bool = False
    request_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "style": self.style,
            "length": self.length,
            "format": self.format,
            "scope": self.scope,
            "persist": bool(self.persist),
            "request_ids": list(self.request_ids),
        }


@dataclass
class RequestItem:
    request_id: str
    source_index: int
    category: RequestCategory
    objective: str
    request_type: RequestType = RequestType.READ
    proposal_required: bool = False
    semantic_target: SemanticTarget = field(default_factory=SemanticTarget)
    semantic_entities: list[SemanticEntity] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    scope: str = "current_turn"
    status: RequestStatus = RequestStatus.PENDING
    status_reason: str = ""
    action_type: str = ""
    presentation: PresentationRequest | None = None
    context_binding: ContextBinding = field(default_factory=ContextBinding)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_index": int(self.source_index),
            "category": self.category.value,
            "objective": self.objective,
            "request_type": self.request_type.value if self.category == RequestCategory.BUSINESS else "",
            "proposal_required": bool(self.proposal_required),
            "semantic_target": self.semantic_target.to_dict(),
            "semantic_entities": [item.to_dict() for item in self.semantic_entities],
            "constraints": list(self.constraints),
            "depends_on": list(self.depends_on),
            "scope": self.scope,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "action_type": self.action_type,
            "presentation": self.presentation.to_dict() if self.presentation else None,
            "context_binding": self.context_binding.to_dict(),
        }


@dataclass
class RequestBundle:
    requests: list[RequestItem]
    raw_message: str
    schema_version: str = "request_bundle.v3"
    decomposition_source: str = "explicit_boundaries+llm_semantics+program_validator"
    reuse_guards: dict[str, Any] = field(default_factory=dict)

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


def explicit_request_boundaries(query: str) -> dict[str, Any]:
    """只返回用户显式任务边界事实，不重复传入分段原文。"""

    indexes: list[int] = []
    types: set[str] = set()
    auto_index = 1
    for raw_line in str(query or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        numbered = _NUMBERED_RE.match(line)
        if numbered:
            index = int(numbered.group(1))
            indexes.append(index)
            types.add("numbered")
            auto_index = max(auto_index, index + 1)
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            indexes.append(auto_index)
            types.add("bullet")
            auto_index += 1
    unique_indexes = list(dict.fromkeys(indexes))
    boundary_type = "none"
    if types == {"numbered"}:
        boundary_type = "numbered"
    elif types == {"bullet"}:
        boundary_type = "bullet"
    elif types:
        boundary_type = "mixed"
    return {
        "has_explicit_boundaries": bool(unique_indexes),
        "boundary_type": boundary_type,
        "boundary_count": len(unique_indexes),
        "source_indexes": unique_indexes,
    }


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
    """把一次用户消息转换成唯一生效的语义 RequestBundle。

    第一次 LLM 只负责当前轮语义理解：它可以根据会话摘要补出自然语言实体名称，
    但绝不能生成权威 ID；GraphRef 始终由后续代码确定性解析。
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
        del run_id
        boundaries = explicit_request_boundaries(query)
        relation = _relation_type(execution_context)
        candidate_turn_ids = {
            str(item.get("turn_id") or "")
            for item in list((reuse_candidates or {}).get("items") or [])
            if str(item.get("turn_id") or "").strip()
        }
        candidate_view = [
            {
                "turn_id": str(item.get("turn_id") or ""),
                "freshness": str(item.get("freshness") or "non_trade"),
                "user_summary": str(item.get("user_summary") or "")[:500],
                "assistant_summary": str(item.get("assistant_summary") or "")[:700],
            }
            for item in list((reuse_candidates or {}).get("items") or [])
            if str(item.get("turn_id") or "").strip()
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
                old_fields = {"target", "mentions"}.intersection(raw)
                if old_fields:
                    raise RequestBundleError(
                        f"legacy_request_semantic_fields_forbidden:{index}:{','.join(sorted(old_fields))}"
                    )
                category = str(raw.get("category") or "").strip().lower()
                if category not in allowed_categories:
                    raise RequestBundleError(f"invalid_request_category:{index}:{category}")
                if category == RequestCategory.BUSINESS.value and not str(raw.get("objective") or "").strip():
                    raise RequestBundleError(f"request_objective_required:{index}")
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
                    semantic_target = raw.get("semantic_target")
                    if not isinstance(semantic_target, dict):
                        raise RequestBundleError(f"semantic_target_required:{index}")
                    target_status = str(semantic_target.get("status") or "").strip().lower()
                    target_source = str(semantic_target.get("source") or "").strip().lower()
                    target_text = str(semantic_target.get("display_text") or "").strip()
                    if target_status not in SEMANTIC_TARGET_STATUSES:
                        raise RequestBundleError(f"invalid_semantic_target_status:{index}:{target_status}")
                    if target_source not in SEMANTIC_TARGET_SOURCES:
                        raise RequestBundleError(f"invalid_semantic_target_source:{index}:{target_source}")
                    if target_status == "identified" and not target_text:
                        raise RequestBundleError(f"identified_semantic_target_requires_text:{index}")
                    if target_status == "missing" and target_source != "none":
                        raise RequestBundleError(f"missing_semantic_target_source_must_be_none:{index}")
                    entities = raw.get("semantic_entities")
                    if not isinstance(entities, list):
                        raise RequestBundleError(f"semantic_entities_must_be_array:{index}")
                    if len(entities) > 20:
                        raise RequestBundleError(f"too_many_semantic_entities:{index}")
                    for entity in entities:
                        if not isinstance(entity, dict) or not str(entity.get("text") or "").strip():
                            raise RequestBundleError(f"invalid_semantic_entity:{index}")
                        if str(entity.get("role") or "focus") not in SEMANTIC_ENTITY_ROLES:
                            raise RequestBundleError(f"invalid_semantic_entity_role:{index}")
                        if str(entity.get("source") or "") not in SEMANTIC_ENTITY_SOURCES:
                            raise RequestBundleError(f"invalid_semantic_entity_source:{index}")
                presentation = raw.get("presentation")
                if category == RequestCategory.PRESENTATION.value:
                    if not isinstance(presentation, dict):
                        raise RequestBundleError(f"presentation_fields_required:{index}")
                    if presentation.get("request_indexes") is not None and not isinstance(
                        presentation.get("request_indexes"), list
                    ):
                        raise RequestBundleError(f"presentation_request_indexes_must_be_array:{index}")
                binding = raw.get("context_binding")
                if not isinstance(binding, dict):
                    raise RequestBundleError(f"request_context_binding_required:{index}")
                if str(binding.get("entity_scope") or "none") not in {item.value for item in EntityScope}:
                    raise RequestBundleError(f"invalid_request_entity_scope:{index}")
                if str(binding.get("reference_entity_type") or "none") not in {item.value for item in ReferenceEntityType}:
                    raise RequestBundleError(f"invalid_request_reference_type:{index}")
                freshness = str(binding.get("freshness_expectation") or FreshnessExpectation.UNSPECIFIED.value)
                if freshness not in {item.value for item in FreshnessExpectation}:
                    raise RequestBundleError(f"invalid_freshness_expectation:{index}:{freshness}")
                reuse_reference = binding.get("reuse_reference")
                if reuse_reference is not None and not isinstance(reuse_reference, list):
                    raise RequestBundleError(f"reuse_reference_must_be_array:{index}")
                reuse_ids = [str(value or "").strip() for value in (reuse_reference or []) if str(value or "").strip()]
                if freshness in {FreshnessExpectation.LATEST.value, FreshnessExpectation.UNSPECIFIED.value} and reuse_ids:
                    raise RequestBundleError(f"reuse_reference_forbidden_for_freshness:{index}:{freshness}")
                unknown_reuse = sorted(set(reuse_ids) - candidate_turn_ids)
                if unknown_reuse:
                    raise RequestBundleError(f"reuse_reference_unknown_turn:{index}:{','.join(unknown_reuse)}")
                if category == RequestCategory.BUSINESS.value and str(raw.get("request_type") or "read").lower() == "write" and reuse_ids:
                    raise RequestBundleError(f"write_request_cannot_reuse:{index}")

        payload = self.llm_service.generate_json(
            stage="request_bundle_decomposition",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是MainAgent入口唯一的Request语义理解器。你只把当前用户消息变成RequestBundle语义合同；"
                        "禁止拆Need、选择Worker、规划Tool或生成任何权威实体ID。"
                        "一个输入可以包含多个同类BUSINESS Request，也可以同时包含BUSINESS和PRESENTATION Request；Request不等于Worker Task。"
                        "显式编号/项目符号只由explicit_request_boundaries保护；显式边界只保护用户原始边界和source_index，不限制一个边界只能产生一个Request。"
                        "如果没有显式边界，Request数量仍由独立用户诉求决定；同一条原始消息可以按语义产生多个Request。"
                        "category只能business/presentation。READ包括查询、分析、比较、建议、生成/修订待审批Proposal；"
                        "WRITE只允许明确确认执行已有Proposal或拒绝/取消已有Proposal。"
                        "语言、风格、长度、格式属于PRESENTATION。PRESENTATION是独立的用户诉求，不是BUSINESS的修饰字段。"
                        "当同一句、同一条原始消息或同一个显式边界中同时存在业务目标与明确呈现要求时，必须分别生成BUSINESS Request和PRESENTATION Request；"
                        "二者可以使用相同source_index。不得把明确呈现要求并入BUSINESS objective、semantic_target、semantic_entities或constraints，也不得忽略。"
                        "objective只描述用户要完成什么：BUSINESS objective填写规范化业务目标；PRESENTATION objective填写简短的呈现要求摘要。"
                        "不得把目标对象、约束或执行步骤塞入BUSINESS objective。"
                        "semantic_target表示当前Request在自然语言层面针对什么对象。它允许三种status："
                        "identified=已经从本轮原文或session_summary识别出具体语义对象；missing=业务需要具体对象但当前上下文仍无法知道；"
                        "not_required=该业务本身不要求一个具体对象。identified必须提供display_text。"
                        "semantic_target.source只能explicit、conversation_context或none。"
                        "当用户说‘它/刚才那只股票/继续看那个事件’时，你可以根据session_summary补出自然语言实体名，"
                        "并把source标记conversation_context；这只是语义实体补全，不得生成证券代码、node_id、GraphRef或其他权威身份。"
                        "semantic_entities记录需要进入后续权威图解析的现实金融对象；每项包含text/entity_type/role/source。"
                        "不要把portfolio/account/global等范围词伪造为具体证券实体。"
                        "context_binding只声明实体范围、是否允许继承焦点、指代类型和新鲜度。"
                        "reuse_candidates是历史回答摘要候选；你只做粗筛，把‘大概可能有用’的turn_id写入reuse_reference，"
                        "最终是否复用由后续Need规划决定。latest或unspecified时reuse_reference必须为空。"
                        "PRESENTATION的request_indexes放在presentation内部，用于指定呈现要求作用于哪些Request；不得再通过业务语义字段表达展示作用范围。"
                        "例如‘这只股票适合我吗？回答不要带表情包’必须生成一个BUSINESS Request和一个PRESENTATION Request；"
                        "后者的objective可为‘回答不要带表情包’，presentation.style可为‘不要带表情包’。"
                        "例如‘分析贵州茅台，用英文表格回答’必须同时生成BUSINESS和PRESENTATION，后者设置presentation.language=en、presentation.format=table。"
                        "depends_on只使用当前requests数组1-based位置。严格只输出JSON。"
                    ),
                },
                {
                    "role": "user",
                    "content": compact_json_dumps({
                        "user_message": str(query or ""),
                        "conversation_context": {
                            "session_summary": str(memory_summary or "")[:3000],
                            "reuse_candidates": candidate_view,
                        },
                        "protocol_relation": relation,
                        "explicit_request_boundaries": boundaries,
                        "current_reply_language": language,
                        "required_output_shape": {
                            "requests": [{
                                "source_index": 1,
                                "category": "business|presentation",
                                "objective": "BUSINESS填写只描述要完成什么的规范化业务目标；PRESENTATION填写简短的呈现要求摘要",
                                "request_type": "read|write",
                                "proposal_required": False,
                                "semantic_target": {
                                    "entity_type": "security|portfolio|account|event|global_market|none|unknown",
                                    "display_text": "自然语言对象名；未知时为空",
                                    "source": "explicit|conversation_context|none",
                                    "status": "identified|missing|not_required",
                                },
                                "semantic_entities": [{
                                    "text": "自然语言实体名",
                                    "entity_type": "security|portfolio|account|event|industry|company|unknown",
                                    "role": "focus|comparison|cause|impact_target|context|event",
                                    "source": "explicit|conversation_context",
                                }],
                                "constraints": ["仅用户明确提出的约束"],
                                "depends_on": [1],
                                "scope": "current_turn",
                                "status": "pending|unsupported",
                                "reason": "",
                                "action_type": "confirm_execute|reject|cancel|",
                                "presentation": {
                                    "language": "zh|en|",
                                    "style": "",
                                    "length": "",
                                    "format": "",
                                    "scope": "request|whole_bundle|current_turn|session",
                                    "persist": False,
                                    "request_indexes": [1],
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
                "只修复Request语义合同。禁止旧字段target/mentions；BUSINESS必须使用semantic_target/semantic_entities。"
                "category只能business/presentation；同一条原始消息可以同时包含BUSINESS和PRESENTATION。"
                "如果用户同时提出业务目标和明确呈现要求，修复时必须保留为两个独立Request，不得把PRESENTATION并入BUSINESS或忽略。"
                "BUSINESS objective是规范化业务目标；PRESENTATION objective可为简短呈现要求摘要。"
                "semantic_target.status只能identified/missing/not_required；identified必须有display_text；"
                "semantic_entities只能输出自然语言语义实体，绝不能生成GraphRef/node_id/证券代码作为权威身份。"
                "reuse_reference只能引用conversation_context.reuse_candidates中的turn_id；不得输出Worker/Need/Tool。"
            ),
        )

        raw_rows = [dict(item) for item in payload.get("requests") or [] if isinstance(item, dict)]
        if not raw_rows:
            raise RequestBundleError("request_bundle_empty_after_llm")
        explicit_source_indexes = set(boundaries.get("source_indexes") or [])
        if explicit_source_indexes:
            returned_indexes: set[int] = set()
            for index, raw in enumerate(raw_rows, start=1):
                try:
                    returned_indexes.add(int(raw.get("source_index", index)))
                except (TypeError, ValueError):
                    returned_indexes.add(index)
            missing = sorted(explicit_source_indexes - returned_indexes)
            if missing:
                raise RequestBundleError(
                    "explicit_request_boundary_lost:" + ",".join(str(item) for item in missing)
                )

        items: list[RequestItem] = []
        raw_presentation_indexes: dict[str, list[int]] = {}
        for index, raw in enumerate(raw_rows, start=1):
            request_id = f"R{index:02d}"
            category = RequestCategory(str(raw.get("category") or "business").strip().lower())
            status_text = str(raw.get("status") or "pending").strip().lower()
            status = RequestStatus.UNSUPPORTED if status_text == "unsupported" else RequestStatus.PENDING
            try:
                source_index = max(1, int(raw.get("source_index", index)))
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
            raw_target = dict(raw.get("semantic_target") or {})
            semantic_target = SemanticTarget(
                entity_type=str(raw_target.get("entity_type") or "none").strip().lower(),
                display_text=str(raw_target.get("display_text") or "").strip(),
                source=str(raw_target.get("source") or "none").strip().lower(),
                status=str(raw_target.get("status") or "not_required").strip().lower(),
            )
            semantic_entities = [
                SemanticEntity(
                    text=str(entity.get("text") or "").strip(),
                    entity_type=str(entity.get("entity_type") or "unknown").strip().lower(),
                    role=str(entity.get("role") or "focus").strip().lower(),
                    source=str(entity.get("source") or "explicit").strip().lower(),
                )
                for entity in raw.get("semantic_entities") or []
                if isinstance(entity, dict) and str(entity.get("text") or "").strip()
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
                raw_presentation_indexes[request_id] = [
                    int(value) for value in p.get("request_indexes") or []
                    if str(value).strip().isdigit()
                ]
            items.append(RequestItem(
                request_id=request_id,
                source_index=source_index,
                category=category,
                objective=str(raw.get("objective") or "").strip(),
                request_type=(
                    RequestType(str(raw.get("request_type") or "read").strip().lower())
                    if category == RequestCategory.BUSINESS else RequestType.READ
                ),
                proposal_required=bool(raw.get("proposal_required")) if category == RequestCategory.BUSINESS else False,
                semantic_target=semantic_target,
                semantic_entities=semantic_entities,
                constraints=[str(value).strip() for value in raw.get("constraints") or [] if str(value).strip()],
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
            ))

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
            if item.presentation is not None:
                ids: list[str] = []
                for position in raw_presentation_indexes.get(item.request_id, []):
                    if 1 <= position <= len(items) and position != index:
                        candidate = items[position - 1].request_id
                        if candidate not in ids:
                            ids.append(candidate)
                item.presentation.request_ids = ids

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
                target.semantic_target = SemanticTarget()
                target.semantic_entities = []
                target.context_binding = ContextBinding()
            else:
                for item in items:
                    item.request_id = f"R{int(item.request_id[1:]) + 1:02d}"
                    item.depends_on = [f"R{int(dep[1:]) + 1:02d}" for dep in item.depends_on]
                    if item.presentation:
                        item.presentation.request_ids = [
                            f"R{int(dep[1:]) + 1:02d}" for dep in item.presentation.request_ids
                        ]
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
                    semantic_target=SemanticTarget(),
                    semantic_entities=[],
                    context_binding=ContextBinding(),
                ))

        bundle = RequestBundle(requests=items, raw_message=str(query or ""))
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
    "SemanticEntity",
    "SemanticTarget",
    "explicit_request_boundaries",
]
