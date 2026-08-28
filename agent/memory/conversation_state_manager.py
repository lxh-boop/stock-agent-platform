from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any

from database.repositories.agent_repository import AgentRepository


RELATION_NEW_GOAL = "new_goal"
RELATION_RETRY_GOAL = "retry_goal"
RELATION_FOLLOW_UP = "follow_up"
RELATION_CLARIFICATION_ANSWER = "clarification_answer"
RELATION_CORRECTION = "correction"
RELATION_CONTINUATION = "continuation"
RELATION_CONFIRMATION = "confirmation"
RELATION_CANCELLATION = "cancellation"
RELATION_TOPIC_SWITCH = "topic_switch"

_CONFIRM = {"确认", "确认执行", "同意", "执行", "可以执行", "yes", "confirm", "confirmed", "execute", "proceed"}
_CANCEL = {"取消", "不用了", "停止", "终止", "算了", "撤销", "cancel", "stop", "abort", "never mind"}
_TOPIC_SWITCH = ("换个问题", "换一个问题", "另外一个问题", "另外问", "先不说这个", "先看别的", "重新开始", "新问题", "new question", "change topic", "something else")
_CORRECTION = ("不是", "不对", "更正", "改成", "应该是", "我说的是", "我的意思是", "纠正", "no,", "not that", "i mean", "change it to", "correction")
_CONTINUATION = (
    "继续", "接着", "往下", "展开", "详细一点", "再详细", "再分析", "进一步",
    "还需要更详细", "更详细的分析", "更详细", "深入分析", "详细分析", "补充分析",
    "接着分析", "继续分析", "继续说", "展开说说", "然后呢", "还有呢",
    "continue", "go on", "more detail", "more detailed", "elaborate", "next",
)
_FRESHNESS = ("当前", "现在", "最新", "重新查询", "重新查", "刷新", "今天", "实时", "latest", "current", "refresh", "re-query")
_STOCK_CODE = re.compile(r"(?<![\d.])([034689]\d{5})(?:\.(?:SH|SZ|BJ))?(?!\d)", re.IGNORECASE)
_STOCK_AMOUNT_SUFFIX = re.compile(r"^(?:元|万|万元|亿|亿元|%|％)")
_REFERENCE = ("这只股票", "该股票", "这只", "该股", "它", "这个", "上述", "上面", "刚才", "之前", "前面", "这个结果", "这个方案", "这个计划", "这条新闻", "为什么", "怎么", "如何", "那风险", "那收益", "this stock", "that stock", "it", "this result", "that result", "the previous", "above", "why", "how", "what about")
_QUESTION = ("为什么", "怎么", "如何", "什么原因", "是否", "能不能", "可以吗", "多少", "哪", "谁", "什么", "why", "how", "what", "which", "where", "when", "can ", "could ", "is ", "are ")
_CLARIFY = ("请提供", "请说明", "请明确", "请补充", "请指定", "具体是哪", "哪一", "哪个", "哪只", "which", "please provide", "please specify", "please clarify")

_EMPTY = ("", None, [], {})
_NON_ENTITY = {
    "user_id", "run_id", "trace_id", "message_id", "conversation_id", "context_id", "task_id",
    "source_run_id", "source_id", "artifact_id", "observation_id", "handoff_id", "approval_id",
    "commit_id", "created_at", "updated_at", "finished_at", "started_at", "timestamp", "status",
    "success", "confidence", "reason", "reason_summary", "message", "answer", "query", "raw_message",
    "resolved_message",
}
_PARAMETER_CONTAINERS = {
    "parameters", "explicit_parameters", "inherited_parameters", "system_generated_parameters",
    "answered_parameters", "active_entities", "arguments", "resolved_arguments",
}
_ENTITY_KEY = re.compile(
    r"(?:^|_)(?:code|symbol|ticker|name|date|time|amount|weight|ratio|top_k|model|industry|"
    r"sector|plan|order|account|stock|portfolio|strategy|target|period|window|limit|count)(?:$|_)",
    re.IGNORECASE,
)
_PLAN = re.compile(r"\b(agent_plan_[A-Za-z0-9_-]+)\b")
_DATE = re.compile(r"(?<!\d)(20\d{2})[-/.年](0?[1-9]|1[0-2])[-/.月](0?[1-9]|[12]\d|3[01])日?(?!\d)")
_PERCENT = re.compile(r"(?<!\d)(-?\d+(?:\.\d+)?)\s*%")
_TOPK = re.compile(r"(?:top\s*|前\s*)(\d{1,3})(?:\s*(?:只|个|名))?", re.IGNORECASE)
_AMOUNT = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(万元|万|元|亿元|亿|million|billion)", re.IGNORECASE)


# Only durable business entities may be inherited between turns. Runtime
# counters, tool names, hashes and bulk ranking rows are deliberately excluded.
_ACTIVE_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "stock_code": ("stock_code", "ts_code", "symbol"),
    "stock_codes": ("stock_codes",),
    "as_of_date": ("as_of_date", "trade_date", "prediction_date", "date"),
    "model_name": ("model_name", "model"),
    "top_k": ("top_k",),
    "amount": ("amount", "capital", "available_capital"),
    "requested_weight": (
        "requested_weight",
        "target_weight",
        "position_weight",
        "percentage",
    ),
    "target_position_count": ("target_position_count",),
    "target_cash_weight": ("target_cash_weight", "cash_weight"),
    "max_single_weight": ("max_single_weight", "max_single_position"),
    "max_industry_weight": (
        "max_industry_weight",
        "max_industry_position",
        "max_industry_exposure",
    ),
    "plan_id": ("plan_id",),
    "strategy_id": ("strategy_id",),
    "strategy_name": ("strategy_name",),
    "portfolio_id": ("portfolio_id",),
    "target_portfolio_id": ("target_portfolio_id",),
}

_ENTITY_WRAPPER_KEYS = {
    "conversation_state",
    "parameters",
    "explicit_parameters",
    "inherited_parameters",
    "system_generated_parameters",
    "answered_parameters",
    "resolved_arguments",
    "arguments",
    "data",
    "result",
    "summary",
    "user_goal",
    "decomposition",
    "profile_summary",
    "constraints",
    "risk_assessment",
    "investment_goal",
}

_BULK_ENTITY_KEYS = {
    "records",
    "rows",
    "items",
    "positions",
    "holdings",
    "ranking",
    "ranking_candidates",
    "candidate_stocks",
    "target_positions",
    "worker_results",
    "tool_calls",
    "observations",
    "execution_batches",
    "replan_audit",
}


@dataclass(frozen=True)
class ConversationMessage:
    role: str
    content: str
    message_id: str = ""
    run_id: str = ""
    created_at: str = ""
    agent_result: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content[:1600],
            "message_id": self.message_id,
            "run_id": self.run_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ResolvedTurn:
    raw_message: str
    resolved_message: str
    relation_type: str
    conversation_id: str
    previous_user_goal: dict[str, Any] = field(default_factory=dict)
    previous_result_summary: str = ""
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    explicit_parameters: dict[str, Any] = field(default_factory=dict)
    inherited_parameters: dict[str, Any] = field(default_factory=dict)
    active_entities: dict[str, Any] = field(default_factory=dict)
    reference_turn_ids: list[str] = field(default_factory=list)
    reference_artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    reference_mode: str = ""
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def planner_context(self) -> dict[str, Any]:
        is_follow_up = self.relation_type in {
            RELATION_FOLLOW_UP, RELATION_CLARIFICATION_ANSWER, RELATION_CORRECTION, RELATION_CONTINUATION,
        }
        return {
            "current_message": self.raw_message,
            "resolved_message": self.resolved_message,
            "conversation_id": self.conversation_id,
            "previous_user_goal": dict(self.previous_user_goal),
            "previous_result_summary": self.previous_result_summary,
            "pending_clarification": dict(self.pending_clarification),
            "explicit_parameters": dict(self.explicit_parameters),
            "inherited_parameters": dict(self.inherited_parameters),
            "active_entities": dict(self.active_entities),
            "recent_messages": list(self.recent_messages),
            "turn_resolution": {
                "relation_type": self.relation_type,
                "reference_turn_ids": list(self.reference_turn_ids),
                "confidence": self.confidence,
                "warnings": list(self.warnings),
            },
            "follow_up": {
                "is_follow_up": is_follow_up,
                "reference_source": "conversation_state",
                "reference_turn_ids": list(self.reference_turn_ids),
                "reference_artifact_refs": list(self.reference_artifact_refs),
                "reference_mode": self.reference_mode,
                "reference_summary": self.previous_result_summary,
            },
        }


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _row_to_message(row: dict[str, Any]) -> ConversationMessage:
    metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else row.get("metadata")
    metadata = _json_dict(metadata)
    agent_result = metadata.get("agent_result") if isinstance(metadata.get("agent_result"), dict) else {}
    return ConversationMessage(
        role=str(row.get("role") or "").lower(),
        content=_text(row.get("content")),
        message_id=str(row.get("message_id") or ""),
        run_id=str(metadata.get("run_id") or row.get("run_id") or ""),
        created_at=str(row.get("created_at") or ""),
        agent_result=dict(agent_result),
    )


def _load_messages(*, user_id: str, conversation_id: str, db_path: str | None, limit: int) -> tuple[list[ConversationMessage], list[str]]:
    if not conversation_id:
        return [], []
    try:
        rows = AgentRepository(db_path or None).list_recent_messages(
            conversation_id, user_id=user_id, limit=max(4, int(limit)), offset=0
        )
    except Exception as exc:
        return [], [f"conversation_history_load_failed:{type(exc).__name__}"]
    messages = [_row_to_message(dict(row)) for row in list(rows or []) if isinstance(row, dict)]
    messages.sort(key=lambda item: (item.created_at, item.message_id))
    return messages[-max(4, int(limit)):], []


def _drop_current_echo(messages: list[ConversationMessage], raw: str) -> list[ConversationMessage]:
    result = list(messages)
    current = _text(raw)
    for index in range(len(result) - 1, -1, -1):
        if result[index].role != "user":
            continue
        if _text(result[index].content) == current:
            del result[index]
        break
    return result


def _walk(value: Any, depth: int = 0):
    if depth > 10:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            yield from _walk(item, depth + 1)


def _first_mapping(value: Any, target: str, depth: int = 0) -> dict[str, Any]:
    if depth > 10:
        return {}
    if isinstance(value, dict):
        candidate = value.get(target)
        if isinstance(candidate, dict) and candidate:
            return dict(candidate)
        for item in value.values():
            found = _first_mapping(item, target, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            found = _first_mapping(item, target, depth + 1)
            if found:
                return found
    return {}


def _first_text(value: Any, keys: set[str], depth: int = 0) -> str:
    if depth > 10:
        return ""
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _text(candidate)
        for item in value.values():
            found = _first_text(item, keys, depth + 1)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            found = _first_text(item, keys, depth + 1)
            if found:
                return found
    return ""


def _collect_texts(value: Any, keys: set[str], depth: int = 0) -> list[str]:
    if depth > 10:
        return []
    result: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in keys:
                if isinstance(item, str) and item.strip():
                    result.append(_text(item))
                elif isinstance(item, list):
                    result.extend(_text(child) for child in item if isinstance(child, str) and child.strip())
            result.extend(_collect_texts(item, keys, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            result.extend(_collect_texts(item, keys, depth + 1))
    return list(dict.fromkeys(result))


def _is_clarification(result: dict[str, Any], assistant_text: str) -> bool:
    """Use only authoritative turn-level fields for pending clarification.

    A failed child task may contain ``need_clarification=true`` even when the
    final completion decision is ``replan_readonly``. Recursively scanning the
    whole agent result therefore creates stale pending questions.
    """
    if not result:
        return False

    authoritative: list[dict[str, Any]] = [result]
    for key in ("decomposition", "completion", "conversation_state"):
        value = result.get(key)
        if isinstance(value, dict):
            authoritative.append(value)

    final_result = result.get("result")
    if isinstance(final_result, dict):
        authoritative.append(final_result)
        data = final_result.get("data")
        if isinstance(data, dict):
            authoritative.append(data)

    # A final non-user recovery decision explicitly closes nested child-task
    # clarification signals.
    final_actions = {
        str(node.get("next_action") or "").strip().lower()
        for node in authoritative
    }
    if final_actions & {
        "replan",
        "replan_readonly",
        "replan_target_design",
        "finish",
        "report",
        "report_limitation",
    }:
        return False

    for node in authoritative:
        next_action = str(node.get("next_action") or "").strip().lower()
        if next_action in {"ask_user", "clarify", "request_information"}:
            return True
        status = str(node.get("status") or node.get("run_status") or "").strip().lower()
        if status in {
            "waiting_for_clarification",
            "needs_clarification",
            "clarification_required",
        }:
            return True
        if bool(node.get("need_clarification")):
            return True
        intent = str(node.get("intent") or node.get("execution_route") or "").strip().lower()
        if intent in {"clarification_required", "clarification"}:
            return True

    lowered_text = _text(assistant_text).lower()
    return ("?" in lowered_text or "？" in lowered_text) and any(
        marker.lower() in lowered_text for marker in _CLARIFY
    )


def _previous_user(messages: list[ConversationMessage], assistant_index: int) -> ConversationMessage | None:
    for index in range(assistant_index - 1, -1, -1):
        if messages[index].role == "user":
            return messages[index]
    return None


def _pending(messages: list[ConversationMessage]) -> dict[str, Any]:
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if item.role != "assistant":
            continue
        if not _is_clarification(item.agent_result, item.content):
            return {}
        previous = _previous_user(messages, index)
        if previous is None:
            return {}
        goal = _first_mapping(item.agent_result, "user_goal") or {
            "raw_message": previous.content,
            "resolved_message": previous.content,
            "action": "resume_pending_goal",
            "objects": [],
            "constraints": [],
            "expected_outputs": [],
            "source": "conversation_state_reconstruction",
        }
        return {
            "active": True,
            "original_query": previous.content,
            "clarification_question": _first_text(
                item.agent_result, {"clarification_question", "question", "block_message"}
            ) or item.content,
            "missing_information": _collect_texts(
                item.agent_result,
                {"missing_information", "missing_user_required_parameters", "missing_parameters", "required_information"},
            ),
            "previous_user_goal": goal,
            "source_user_message_id": previous.message_id,
            "source_assistant_message_id": item.message_id,
            "source_run_id": item.run_id,
        }
    return {}


def _entity_allowed(value: Any) -> bool:
    if value in _EMPTY:
        return False
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return 0 < len(_text(value)) <= 200
    if isinstance(value, list):
        return 0 < len(value) <= 20 and all(isinstance(item, (str, int, float, bool)) for item in value)
    return False


def _canonical_entity_name(key: str) -> str:
    lowered = str(key or "").strip().lower()
    for canonical, aliases in _ACTIVE_ENTITY_ALIASES.items():
        if lowered == canonical or lowered in aliases:
            return canonical
    return ""


def _normalise_entity_value(name: str, value: Any) -> Any:
    if name == "stock_code":
        # The value already comes from a structured business field. Preserve it
        # verbatim and let the LLM/planner resolve its market semantics.
        return _text(value)[:64]
    if name == "stock_codes":
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value[:20]:
            text = _text(item)[:64]
            if text and text not in result:
                result.append(text)
        return result
    if isinstance(value, str):
        return _text(value)
    return value


def _entities(
    value: Any,
    depth: int = 0,
    *,
    visited: set[int] | None = None,
) -> dict[str, Any]:
    """Extract a small, auditable set of business entities.

    The previous recursive name-pattern scan also inherited runtime counters,
    hashes, tool names and the first code from bulk ranking rows. This version
    only accepts explicit aliases and never descends into bulk result lists.
    """

    if depth > 8 or value is None:
        return {}
    if visited is None:
        visited = set()
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in visited:
            return {}
        visited.add(marker)

    result: dict[str, Any] = {}
    if not isinstance(value, dict):
        return result

    # Direct singular fields and structured parameter containers have priority.
    for raw_key, item in value.items():
        key = str(raw_key)
        canonical = _canonical_entity_name(key)
        if canonical and _entity_allowed(item):
            normalised = _normalise_entity_value(canonical, item)
            if normalised not in _EMPTY:
                result.setdefault(canonical, normalised)

    for raw_key, item in value.items():
        key = str(raw_key).lower()
        if key in _BULK_ENTITY_KEYS:
            continue
        if key not in _ENTITY_WRAPPER_KEYS:
            continue
        if not isinstance(item, dict):
            continue
        for nested_key, nested_value in _entities(
            item,
            depth + 1,
            visited=visited,
        ).items():
            result.setdefault(nested_key, nested_value)

    return result


def _extract_stock_codes(text: str) -> list[str]:
    """Extract explicit A-share security codes without treating amounts as codes."""

    raw = str(text or "")
    result: list[str] = []
    for match in _STOCK_CODE.finditer(raw):
        suffix = raw[match.end(): match.end() + 3]
        if _STOCK_AMOUNT_SUFFIX.match(suffix):
            continue
        code = str(match.group(1) or "")
        if code and code not in result:
            result.append(code)
    return result


def _explicit(text: str) -> dict[str, Any]:
    """Extract only high-confidence identifiers needed for deterministic continuity.

    All other business parameters remain LLM-planned.  This keeps the resolver
    from guessing dates, amounts or weights while still making an explicit
    stock target authoritative from the first turn.
    """

    codes = _extract_stock_codes(text)
    if len(codes) == 1:
        return {"stock_code": codes[0], "stock_codes": codes}
    if codes:
        return {"stock_codes": codes}
    return {}


def _artifact_ref(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        artifact_id = value.strip()
        return {"artifact_id": artifact_id} if artifact_id else {}
    if not isinstance(value, dict):
        return {}
    artifact_id = str(value.get("artifact_id") or "").strip()
    if not artifact_id:
        return {}
    result = {"artifact_id": artifact_id}
    for key in (
        "artifact_type", "schema_version", "producer_type", "producer_id",
        "tool_name", "stock_code", "stock_name", "created_at", "expires_at",
    ):
        item = value.get(key)
        if item not in _EMPTY:
            result[key] = item
    return result


def _extract_artifact_refs(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 12:
        return []
    refs: list[dict[str, Any]] = []
    if isinstance(value, dict):
        direct = _artifact_ref(value)
        if direct:
            refs.append(direct)
        for key, item in value.items():
            name = str(key).lower()
            if name in {"artifact_refs", "reference_artifact_refs", "evidence_refs"} and isinstance(item, list):
                for ref in item:
                    parsed = _artifact_ref(ref)
                    if parsed:
                        refs.append(parsed)
            elif name == "artifact_ref":
                parsed = _artifact_ref(item)
                if parsed:
                    refs.append(parsed)
            refs.extend(_extract_artifact_refs(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            refs.extend(_extract_artifact_refs(item, depth + 1))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        artifact_id = str(ref.get("artifact_id") or "")
        if not artifact_id or artifact_id in seen:
            continue
        seen.add(artifact_id)
        deduped.append(ref)
    return deduped


_INTERNAL_SUMMARIES = {
    "deterministic_registered_capability_fallback",
    "llm completion observer unavailable; deterministic output contract is incomplete.",
    "deterministic output contract is incomplete.",
}


def _summary_candidate(value: Any) -> str:
    if isinstance(value, str):
        text = _text(value)
        if text and text.lower() not in _INTERNAL_SUMMARIES:
            return text
    if isinstance(value, dict):
        for key in ("answer", "final_answer", "report", "message"):
            candidate = _summary_candidate(value.get(key))
            if candidate:
                return candidate
        data = value.get("data") if isinstance(value.get("data"), dict) else {}
        for key in ("summary", "analysis_conclusion", "result_summary"):
            candidate = _summary_candidate(data.get(key))
            if candidate:
                return candidate
        for key in ("summary", "result_summary", "reference_summary", "latest_result_summary", "content"):
            candidate = _summary_candidate(value.get(key))
            if candidate:
                return candidate
        for key in ("result", "produced_outputs", "conversation_state_after_run"):
            candidate = _summary_candidate(value.get(key))
            if candidate:
                return candidate
    return ""


def _meaningful_result_summary(agent_result: dict[str, Any], assistant_text: str) -> str:
    candidate = _summary_candidate(agent_result)
    if candidate:
        return candidate
    text = _text(assistant_text)
    return "" if text.lower() in _INTERNAL_SUMMARIES else text


def _last_state(
    messages: list[ConversationMessage],
) -> tuple[dict[str, Any], str, dict[str, Any], list[str], list[dict[str, Any]]]:
    for index in range(len(messages) - 1, -1, -1):
        item = messages[index]
        if item.role != "assistant" or not item.agent_result:
            continue
        goal = _first_mapping(item.agent_result, "user_goal")
        after_run = _first_mapping(item.agent_result, "conversation_state_after_run")
        if not goal and isinstance(after_run.get("active_goal"), dict):
            goal = dict(after_run.get("active_goal") or {})
        summary = _meaningful_result_summary(item.agent_result, item.content)
        artifact_refs = _extract_artifact_refs(item.agent_result)
        refs = [value for value in (item.message_id, item.run_id) if value]
        previous = _previous_user(messages, index)
        entities: dict[str, Any] = {}
        if previous is not None:
            entities.update(_explicit(previous.content))
            refs.insert(0, previous.message_id)
            if not goal:
                goal = {
                    "raw_message": previous.content,
                    "resolved_message": previous.content,
                    "action": "continue_previous_goal",
                    "objects": [],
                    "constraints": [],
                    "expected_outputs": [],
                    "source": "conversation_state_reconstruction",
                }
        if isinstance(after_run.get("active_entities"), dict):
            entities.update(after_run.get("active_entities") or {})
        for entity_key, entity_value in _entities(item.agent_result).items():
            entities.setdefault(entity_key, entity_value)
        if len(entities.get("stock_codes") or []) == 1 and not entities.get("stock_code"):
            entities["stock_code"] = list(entities["stock_codes"])[0]
        return (
            goal,
            summary,
            entities,
            list(dict.fromkeys(refs)),
            artifact_refs,
        )
    return {}, "", {}, [], []


def _marker_text(text: str) -> str:
    return _text(text).lower().strip("。！？!?，,；;：:")


def _contains(text: str, markers) -> bool:
    lowered = _text(text).lower()
    return any(marker.lower() in lowered for marker in markers)


def _ngrams(text: str, size: int = 2) -> set[str]:
    cleaned = re.sub(r"[\W_]+", "", _text(text).lower(), flags=re.UNICODE)
    if len(cleaned) < size:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + size] for i in range(len(cleaned) - size + 1)}


def _overlap(left: str, right: str) -> float:
    a, b = _ngrams(left), _ngrams(right)
    return len(a & b) / max(1, len(a | b)) if a and b else 0.0


def _select_relevant_history(
    history: list[ConversationMessage],
    raw: str,
    *,
    relation: str,
    candidate_top_n: int = 12,
    relevance_threshold: float = 0.12,
    token_budget_chars: int = 3200,
) -> list[dict[str, Any]]:
    """Select conversational history by relevance, not a fixed recent TopK.

    The last ``candidate_top_n`` messages form a broad candidate pool.  New
    goals receive no inherited history.  Follow-ups are filtered by text
    overlap, reference markers and recency, then packed into a small character
    budget before entering planner context.
    """

    if relation in {RELATION_NEW_GOAL, RELATION_TOPIC_SWITCH, RELATION_RETRY_GOAL}:
        return []
    candidates = [item for item in history[-max(1, candidate_top_n):] if item.content]
    scored: list[tuple[float, int, ConversationMessage]] = []
    has_reference = _contains(raw, _REFERENCE)
    total = max(1, len(candidates))
    for index, item in enumerate(candidates):
        overlap = _overlap(raw, item.content)
        recency = (index + 1) / total
        role_bonus = 0.08 if item.role == "assistant" else 0.04
        reference_bonus = 0.18 if has_reference else 0.0
        score = 0.62 * overlap + 0.20 * recency + role_bonus + reference_bonus
        if score >= relevance_threshold:
            scored.append((score, index, item))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected: list[ConversationMessage] = []
    used_chars = 0
    for _score, _index, item in scored:
        size = len(item.content)
        if used_chars + size > token_budget_chars and selected:
            continue
        selected.append(item)
        used_chars += min(size, token_budget_chars)
        if used_chars >= token_budget_chars:
            break
    selected.sort(key=lambda item: history.index(item))
    return [item.safe_dict() for item in selected]


def _looks_follow_up(raw: str, goal: dict[str, Any], summary: str, active: dict[str, Any], explicit: dict[str, Any]) -> tuple[bool, float]:
    if not raw or not goal:
        return False, 0.0
    if _contains(raw, _REFERENCE):
        return True, 0.92
    if explicit:
        return False, 0.2
    previous = " ".join(str(value or "") for value in (
        goal.get("raw_message"), goal.get("resolved_message"), goal.get("goal_summary"), summary
    ))
    score = _overlap(raw, previous)
    if active and _contains(raw, _QUESTION) and len(raw) <= 48:
        return True, max(0.76, score)
    if len(raw) <= 48 and score >= 0.08:
        return True, min(0.88, 0.65 + score)
    return False, score


def _is_retry_of_pending(raw: str, pending: dict[str, Any]) -> bool:
    original = _text(pending.get("original_query"))
    current = _text(raw)
    if not original or not current:
        return False
    if _marker_text(original) == _marker_text(current):
        return True
    retry_markers = ("重新", "再来一次", "重试", "还是", "again", "retry")
    return _contains(current, retry_markers) and _overlap(current, original) >= 0.35


def _is_retry_of_goal(raw: str, goal: dict[str, Any]) -> bool:
    original = _text(
        goal.get("raw_message")
        or goal.get("resolved_message")
        or ""
    )
    current = _text(raw)
    return bool(
        original
        and current
        and _marker_text(original) == _marker_text(current)
    )


def _active_subject(active: dict[str, Any], goal: dict[str, Any]) -> str:
    stock_name = str(active.get("stock_name") or goal.get("stock_name") or "").strip()
    stock_code = str(active.get("stock_code") or goal.get("stock_code") or "").strip()
    if not stock_code:
        codes = active.get("stock_codes") or goal.get("stock_codes") or []
        if isinstance(codes, list) and len(codes) == 1:
            stock_code = str(codes[0] or "").strip()
    if stock_name and stock_code:
        return f"{stock_name}（{stock_code}）"
    return stock_name or stock_code or "上一轮对象"


def _resolved_message(
    *,
    relation: str,
    raw: str,
    goal: dict[str, Any],
    summary: str,
    pending: dict[str, Any],
    active: dict[str, Any],
    explicit: dict[str, Any],
    inherited: dict[str, Any],
) -> str:
    del summary, explicit, inherited
    if relation in {
        RELATION_NEW_GOAL,
        RELATION_RETRY_GOAL,
        RELATION_CONFIRMATION,
        RELATION_CANCELLATION,
        RELATION_TOPIC_SWITCH,
    }:
        return raw
    subject = _active_subject(active, goal)
    if relation == RELATION_CONTINUATION:
        if "详细" in raw:
            return f"对{subject}进行更详细的分析。"
        return f"继续上一轮关于{subject}的目标：{raw}"
    if relation == RELATION_FOLLOW_UP:
        return f"基于上一轮关于{subject}的结果回答：{raw}"
    if relation == RELATION_CORRECTION:
        return f"继续上一轮关于{subject}的目标，并按本轮纠正处理：{raw}"
    if relation == RELATION_CLARIFICATION_ANSWER:
        original = _text(pending.get("original_query")) or _text(goal.get("raw_message"))
        prefix = f"继续完成原始目标“{original}”。" if original else "继续完成上一轮目标。"
        return f"{prefix}用户补充：{raw}"
    return raw


def _reference_mode(relation: str, raw: str, artifact_refs: list[dict[str, Any]]) -> str:
    if _contains(raw, _FRESHNESS):
        return "current_state"
    if relation in {RELATION_FOLLOW_UP, RELATION_CONTINUATION, RELATION_CORRECTION}:
        return "previous_result" if artifact_refs else "conversation_state"
    if relation == RELATION_CLARIFICATION_ANSWER:
        return "conversation_state"
    return "current_state"


def resolve_turn_from_messages(raw_message: str, *, conversation_id: str, messages: list[ConversationMessage], warnings: list[str] | None = None) -> ResolvedTurn:
    raw = _text(raw_message)
    history = _drop_current_echo(list(messages), raw)
    pending = _pending(history)
    goal, summary, active, refs, artifact_refs = _last_state(history)
    explicit = _explicit(raw)

    if pending:
        goal = dict(pending.get("previous_user_goal") or {}) or goal
        refs = list(dict.fromkeys([
            pending.get("source_user_message_id"), pending.get("source_assistant_message_id"),
            pending.get("source_run_id"), *refs,
        ]))
        refs = [item for item in refs if item]

    normalised = _marker_text(raw)
    retry_pending_goal = bool(pending and _is_retry_of_pending(raw, pending))
    if normalised in _CONFIRM:
        relation, confidence = RELATION_CONFIRMATION, 0.99
    elif normalised in _CANCEL:
        relation, confidence = RELATION_CANCELLATION, 0.99
    elif _contains(raw, _TOPIC_SWITCH):
        relation, confidence = RELATION_TOPIC_SWITCH, 0.96
    elif retry_pending_goal:
        # A repeated complete request is a retry, not an answer to the old
        # clarification question. Clear the stale pending state.
        relation, confidence = RELATION_RETRY_GOAL, 0.99
        pending = {}
    elif pending and (_contains(raw, _CONTINUATION) or (_contains(raw, _QUESTION) and not explicit)):
        # A new question or continuation is not a value for the pending slot.
        relation, confidence = (RELATION_CONTINUATION, 0.96) if _contains(raw, _CONTINUATION) else (RELATION_FOLLOW_UP, 0.9)
        pending = {}
    elif pending:
        relation, confidence = RELATION_CLARIFICATION_ANSWER, 0.98
    elif _is_retry_of_goal(raw, goal):
        relation, confidence = RELATION_RETRY_GOAL, 0.97
    elif _contains(raw, _CORRECTION):
        relation, confidence = RELATION_CORRECTION, 0.92
    elif _contains(raw, _CONTINUATION) and (goal or active or artifact_refs):
        relation, confidence = RELATION_CONTINUATION, 0.96
    else:
        follows, score = _looks_follow_up(raw, goal, summary, active, explicit)
        relation = RELATION_FOLLOW_UP if follows else RELATION_NEW_GOAL
        confidence = score if follows else max(0.72, 1.0 - score)

    if relation in {RELATION_NEW_GOAL, RELATION_TOPIC_SWITCH}:
        # A new goal must not inherit stale objectives, results, entities or
        # references.  Relevant long-term memory is retrieved separately by
        # MemoryManager using the new query.
        goal = {}
        summary = ""
        active = {}
        refs = []
        artifact_refs = []
        pending = {}

    if explicit:
        active = {**active, **explicit}

    inherited: dict[str, Any] = {}
    if relation in {RELATION_FOLLOW_UP, RELATION_CLARIFICATION_ANSWER, RELATION_CORRECTION, RELATION_CONTINUATION}:
        inherited.update(active)
    for key in explicit:
        inherited.pop(key, None)
    if relation == RELATION_CLARIFICATION_ANSWER:
        inherited.update(explicit)

    reference_mode = _reference_mode(relation, raw, artifact_refs)

    resolved = _resolved_message(
        relation=relation, raw=raw, goal=goal, summary=summary, pending=pending,
        active=active, explicit=explicit, inherited=inherited,
    )
    return ResolvedTurn(
        raw_message=raw,
        resolved_message=resolved,
        relation_type=relation,
        conversation_id=str(conversation_id or ""),
        previous_user_goal=goal,
        previous_result_summary=summary[:2400],
        pending_clarification=pending,
        explicit_parameters=explicit,
        inherited_parameters=inherited,
        active_entities=active,
        reference_turn_ids=refs[:20],
        reference_artifact_refs=artifact_refs[:20],
        reference_mode=reference_mode,
        recent_messages=_select_relevant_history(
            history,
            raw,
            relation=relation,
            candidate_top_n=12,
            relevance_threshold=0.12,
            token_budget_chars=3200,
        ),
        confidence=round(float(confidence), 4),
        warnings=list(warnings or []),
    )


def resolve_conversation_turn(raw_message: str, *, user_id: str, conversation_id: str, db_path: str | None = None, max_messages: int = 30) -> ResolvedTurn:
    messages, warnings = _load_messages(
        user_id=str(user_id or ""), conversation_id=str(conversation_id or ""), db_path=db_path, limit=max_messages
    )
    return resolve_turn_from_messages(
        raw_message, conversation_id=conversation_id, messages=messages, warnings=warnings
    )


def merge_planner_context(base_context: dict[str, Any] | None, turn: ResolvedTurn) -> dict[str, Any]:
    result = dict(base_context or {})
    incoming = turn.planner_context()
    for key in ("current_message", "resolved_message", "conversation_id", "previous_result_summary"):
        if result.get(key) in _EMPTY and incoming.get(key) not in _EMPTY:
            result[key] = incoming[key]
    for key in (
        "previous_user_goal", "pending_clarification", "explicit_parameters", "active_entities",
        "turn_resolution", "follow_up",
    ):
        current = result.get(key)
        value = incoming.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = {**value, **current}
        elif current in _EMPTY and value not in _EMPTY:
            result[key] = value
    inherited = dict(turn.inherited_parameters)
    if isinstance(result.get("inherited_parameters"), dict):
        inherited.update(result["inherited_parameters"])
    result["inherited_parameters"] = inherited
    if not isinstance(result.get("recent_messages"), list):
        result["recent_messages"] = incoming["recent_messages"]
    result["conversation_state"] = {
        "relation_type": turn.relation_type,
        "active_entities": dict(turn.active_entities),
        "pending_clarification": dict(turn.pending_clarification),
        "reference_turn_ids": list(turn.reference_turn_ids),
        "reference_artifact_refs": list(turn.reference_artifact_refs),
        "reference_mode": turn.reference_mode,
        "confidence": turn.confidence,
        "warnings": list(turn.warnings),
    }
    return result
