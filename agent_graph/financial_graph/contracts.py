from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable
from uuid import uuid4


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_graph_id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {str(k): _plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


def _clean_text(value: Any, *, limit: int = 4000) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _clean_str_list(value: Any, *, limit: int = 100) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in list(value)[:limit]:
        text = _clean_text(item, limit=1000)
        if text and text not in result:
            result.append(text)
    return result


class GraphNodeKind(str, Enum):
    OBJECT = "object"
    IDENTITY = "identity"
    TERM = "term"
    ASSERTION = "assertion"
    EVIDENCE = "evidence"

    @classmethod
    def from_value(cls, value: Any) -> "GraphNodeKind":
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        raise ValueError(f"unsupported_graph_node_kind:{text}")


class GraphAuthority(str, Enum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    CANONICAL = "canonical"
    REJECTED = "rejected"

    @classmethod
    def from_value(cls, value: Any) -> "GraphAuthority":
        if isinstance(value, cls):
            return value
        text = str(value or "candidate").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        return cls.CANDIDATE


@dataclass(frozen=True)
class GraphRef:
    """The only public reference used by Agents and public Tools.

    Provider codes, stock names and database primary keys stay behind graph-boundary
    services. A GraphRef is stable across Agents and is safe to place in memory,
    task contracts and artifacts.
    """

    graph_id: str
    node_id: str
    node_kind: GraphNodeKind | str
    role: str
    as_of_time: str = ""
    snapshot_id: str = ""
    source: str = ""
    confidence: float = 1.0
    locked: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", _clean_text(self.graph_id or "financial_graph", limit=120))
        object.__setattr__(self, "node_id", _clean_text(self.node_id, limit=400))
        object.__setattr__(self, "node_kind", GraphNodeKind.from_value(self.node_kind))
        object.__setattr__(self, "role", _clean_text(self.role or "context", limit=120))
        object.__setattr__(self, "as_of_time", _clean_text(self.as_of_time, limit=80))
        object.__setattr__(self, "snapshot_id", _clean_text(self.snapshot_id, limit=400))
        object.__setattr__(self, "source", _clean_text(self.source, limit=200))
        try:
            confidence = max(0.0, min(1.0, float(self.confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "locked", bool(self.locked))
        if not self.node_id:
            raise ValueError("graph_ref_node_id_required")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GraphRef":
        return cls(**dict(value or {}))


@dataclass(frozen=True)
class GraphObjectRecord:
    object_id: str
    name: str
    object_class: str = "unknown"
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    authority: GraphAuthority | str = GraphAuthority.CANDIDATE
    confidence: float = 0.5
    source_refs: list[str] = field(default_factory=list)
    valid_from: str = ""
    valid_to: str = ""
    observed_at: str = field(default_factory=utc_now_text)

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_id", _clean_text(self.object_id, limit=400))
        object.__setattr__(self, "name", _clean_text(self.name, limit=500))
        object.__setattr__(self, "object_class", _clean_text(self.object_class or "unknown", limit=200))
        object.__setattr__(self, "aliases", _clean_str_list(self.aliases, limit=100))
        object.__setattr__(self, "properties", dict(self.properties or {}))
        object.__setattr__(self, "authority", GraphAuthority.from_value(self.authority))
        object.__setattr__(self, "source_refs", _clean_str_list(self.source_refs, limit=100))
        object.__setattr__(self, "valid_from", _clean_text(self.valid_from, limit=80))
        object.__setattr__(self, "valid_to", _clean_text(self.valid_to, limit=80))
        object.__setattr__(self, "observed_at", _clean_text(self.observed_at or utc_now_text(), limit=80))
        try:
            object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        except (TypeError, ValueError):
            object.__setattr__(self, "confidence", 0.5)
        if not self.object_id or not self.name:
            raise ValueError("graph_object_id_and_name_required")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class GraphIdentityRecord:
    identity_id: str
    namespace: str
    value: str
    object_id: str
    identity_type: str = "identifier"
    is_primary: bool = False
    confidence: float = 1.0
    source_refs: list[str] = field(default_factory=list)
    valid_from: str = ""
    valid_to: str = ""

    def __post_init__(self) -> None:
        for key in ("identity_id", "namespace", "value", "object_id", "identity_type"):
            object.__setattr__(self, key, _clean_text(getattr(self, key), limit=400))
        object.__setattr__(self, "namespace", self.namespace.lower())
        object.__setattr__(self, "source_refs", _clean_str_list(self.source_refs, limit=100))
        try:
            object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        except (TypeError, ValueError):
            object.__setattr__(self, "confidence", 0.0)
        if not self.identity_id or not self.namespace or not self.value or not self.object_id:
            raise ValueError("graph_identity_required_fields_missing")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class GraphTermRecord:
    term_id: str
    name: str
    term_kind: str
    description: str = ""
    parent_term_ids: list[str] = field(default_factory=list)
    status: str = "active"
    source_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for key in ("term_id", "name", "term_kind", "description", "status"):
            object.__setattr__(self, key, _clean_text(getattr(self, key), limit=1000))
        object.__setattr__(self, "parent_term_ids", _clean_str_list(self.parent_term_ids, limit=50))
        object.__setattr__(self, "source_refs", _clean_str_list(self.source_refs, limit=100))
        if not self.term_id or not self.name or not self.term_kind:
            raise ValueError("graph_term_required_fields_missing")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class GraphEvidenceRecord:
    evidence_id: str
    evidence_class: str
    title: str
    source: str
    content_ref: str
    content_hash: str = ""
    published_at: str = ""
    observed_at: str = field(default_factory=utc_now_text)
    reliability: float = 0.5
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for key in (
            "evidence_id", "evidence_class", "title", "source", "content_ref",
            "content_hash", "published_at", "observed_at",
        ):
            object.__setattr__(self, key, _clean_text(getattr(self, key), limit=4000))
        object.__setattr__(self, "properties", dict(self.properties or {}))
        try:
            object.__setattr__(self, "reliability", max(0.0, min(1.0, float(self.reliability))))
        except (TypeError, ValueError):
            object.__setattr__(self, "reliability", 0.5)
        if not self.evidence_id or not self.evidence_class or not self.content_ref:
            raise ValueError("graph_evidence_required_fields_missing")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class GraphAssertionRecord:
    assertion_id: str
    subject_id: str
    predicate_term_id: str
    object_id: str = ""
    literal_value: Any = None
    assertion_class: str = "claim"
    authority: GraphAuthority | str = GraphAuthority.CANDIDATE
    polarity: str = "positive"
    modality: str = "asserted"
    confidence: float = 0.5
    properties: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    valid_from: str = ""
    valid_to: str = ""
    observed_at: str = field(default_factory=utc_now_text)

    def __post_init__(self) -> None:
        for key in (
            "assertion_id", "subject_id", "predicate_term_id", "object_id",
            "assertion_class", "polarity", "modality", "valid_from", "valid_to", "observed_at",
        ):
            object.__setattr__(self, key, _clean_text(getattr(self, key), limit=500))
        object.__setattr__(self, "authority", GraphAuthority.from_value(self.authority))
        object.__setattr__(self, "properties", dict(self.properties or {}))
        object.__setattr__(self, "evidence_ids", _clean_str_list(self.evidence_ids, limit=100))
        object.__setattr__(self, "contradicting_evidence_ids", _clean_str_list(self.contradicting_evidence_ids, limit=100))
        object.__setattr__(self, "source_refs", _clean_str_list(self.source_refs, limit=100))
        try:
            object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        except (TypeError, ValueError):
            object.__setattr__(self, "confidence", 0.5)
        if not self.assertion_id or not self.subject_id or not self.predicate_term_id:
            raise ValueError("graph_assertion_required_fields_missing")
        if not self.object_id and self.literal_value is None:
            raise ValueError("graph_assertion_object_or_literal_required")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass
class GraphPatch:
    patch_id: str = field(default_factory=lambda: new_graph_id("patch"))
    source_task_id: str = ""
    source_agent_id: str = ""
    source_refs: list[str] = field(default_factory=list)
    objects: list[GraphObjectRecord] = field(default_factory=list)
    identities: list[GraphIdentityRecord] = field(default_factory=list)
    terms: list[GraphTermRecord] = field(default_factory=list)
    assertions: list[GraphAssertionRecord] = field(default_factory=list)
    evidence: list[GraphEvidenceRecord] = field(default_factory=list)
    invalidation_ids: list[str] = field(default_factory=list)
    validation_status: str = "pending"
    created_at: str = field(default_factory=utc_now_text)

    def __post_init__(self) -> None:
        self.patch_id = _clean_text(self.patch_id or new_graph_id("patch"), limit=400)
        self.source_task_id = _clean_text(self.source_task_id, limit=300)
        self.source_agent_id = _clean_text(self.source_agent_id, limit=200).upper()
        self.source_refs = _clean_str_list(self.source_refs, limit=200)
        self.invalidation_ids = _clean_str_list(self.invalidation_ids, limit=200)
        self.validation_status = _clean_text(self.validation_status or "pending", limit=80)
        self.created_at = _clean_text(self.created_at or utc_now_text(), limit=80)
        self.objects = [item if isinstance(item, GraphObjectRecord) else GraphObjectRecord(**item) for item in self.objects]
        self.identities = [item if isinstance(item, GraphIdentityRecord) else GraphIdentityRecord(**item) for item in self.identities]
        self.terms = [item if isinstance(item, GraphTermRecord) else GraphTermRecord(**item) for item in self.terms]
        self.assertions = [item if isinstance(item, GraphAssertionRecord) else GraphAssertionRecord(**item) for item in self.assertions]
        self.evidence = [item if isinstance(item, GraphEvidenceRecord) else GraphEvidenceRecord(**item) for item in self.evidence]

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GraphPatch":
        return cls(**dict(value or {}))


@dataclass(frozen=True)
class GraphPathRef:
    path_id: str
    start_ref: GraphRef
    end_ref: GraphRef
    assertion_ids: list[str] = field(default_factory=list)
    object_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    path_type: str = "semantic"
    confidence: float = 0.0
    explanation: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path_id", _clean_text(self.path_id, limit=400))
        object.__setattr__(self, "path_type", _clean_text(self.path_type or "semantic", limit=160))
        object.__setattr__(self, "assertion_ids", _clean_str_list(self.assertion_ids, limit=100))
        object.__setattr__(self, "object_ids", _clean_str_list(self.object_ids, limit=100))
        object.__setattr__(self, "evidence_ids", _clean_str_list(self.evidence_ids, limit=100))
        object.__setattr__(self, "explanation", _clean_text(self.explanation, limit=2000))
        try:
            object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        except (TypeError, ValueError):
            object.__setattr__(self, "confidence", 0.0)
        if not self.path_id:
            raise ValueError("graph_path_id_required")

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass
class TaskGraphView:
    view_id: str
    anchor_refs: list[GraphRef]
    nodes: list[dict[str, Any]] = field(default_factory=list)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    path_refs: list[GraphPathRef] = field(default_factory=list)
    query_policy: dict[str, Any] = field(default_factory=dict)
    as_of_time: str = ""
    created_at: str = field(default_factory=utc_now_text)

    def __post_init__(self) -> None:
        self.view_id = _clean_text(self.view_id, limit=400)
        self.anchor_refs = [item if isinstance(item, GraphRef) else GraphRef.from_dict(item) for item in self.anchor_refs]
        self.nodes = [dict(item) for item in self.nodes if isinstance(item, dict)]
        self.assertions = [dict(item) for item in self.assertions if isinstance(item, dict)]
        self.evidence = [dict(item) for item in self.evidence if isinstance(item, dict)]
        self.path_refs = [item if isinstance(item, GraphPathRef) else GraphPathRef(**item) for item in self.path_refs]
        self.query_policy = dict(self.query_policy or {})
        self.as_of_time = _clean_text(self.as_of_time, limit=80)
        self.created_at = _clean_text(self.created_at or utc_now_text(), limit=80)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class ResolutionCandidate:
    graph_ref: GraphRef
    matched_text: str
    matched_by: str
    display_name: str = ""
    identities: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass
class GraphResolution:
    candidates: list[ResolutionCandidate] = field(default_factory=list)
    unresolved_mentions: list[str] = field(default_factory=list)
    ambiguous_mentions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def refs(self) -> list[GraphRef]:
        return [item.graph_ref for item in self.candidates]

    @property
    def need_context(self) -> bool:
        return bool(self.ambiguous_mentions)

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


def refs_from(value: Any) -> list[GraphRef]:
    if value is None:
        return []
    rows: Iterable[Any]
    if isinstance(value, (GraphRef, dict)):
        rows = [value]
    elif isinstance(value, (list, tuple, set)):
        rows = value
    else:
        return []
    result: list[GraphRef] = []
    for row in rows:
        try:
            ref = row if isinstance(row, GraphRef) else GraphRef.from_dict(dict(row))
        except Exception:
            continue
        if not any(existing.node_id == ref.node_id and existing.role == ref.role for existing in result):
            result.append(ref)
    return result
