from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from core.llm import LLMService

from agent.console_trace import flow_event, runtime_debug_event, trace_exception
from agent.artifacts import ArtifactStore
from agent.capabilities.data_names import LEGACY_OUTPUT_NAME_MAP, normalize_data_name
from agent.context.context_hydrator import ContextHydrator, ContextRequirement
from agent.context.context_types import ContextBundle
from agent.context.context_sufficiency_gate import ContextAndEntitySufficiencyGate
from agent.runtime_state import (
    LLMConcurrencyGate, RequestCheckpoint, RunCheckpoint, RunCheckpointStore, RuntimeResourceBudget,
)

from agent.graph.contracts import GraphNodeKind, GraphRef, refs_from
from agent.graph.errors import GraphConfigurationError, GraphUnavailableError
from agent.graph.identity import GraphEntityIdentityService
from agent.graph.settings import Neo4jSettings
from agent.graph.store import Neo4jFinancialGraphStore
from agent.graph.patch_validator import GraphPatchValidator
from agent.graph.evidence_ingestion import EvidenceIngestionService
from agent.graph.portfolio_graph import PortfolioGraphService
from agent.graph.provider_adapter import GraphProviderAdapter
from agent.graph.impact_service import GraphImpactService

from .completion import evaluate_need_completion, flow_decision, non_success_completion_report, validate_completion_report
from .worker_directory import CapabilityWorkerDirectory, ENTITY_ANALYST, REPORT_WRITER
from .context_binding import ContextBinding
from .turn_inventory import build_conversation_inventory, load_candidate_answers
from .models import GraphAgentTask, GraphWorkerResult, MissingContextItem, ResultStatus
from .planner import CoordinatorPlanner
from .presentation_policy import PresentationPolicy, PresentationPolicyResolver, PresentationValidator
from .request_bundle import (
    RequestBundle, RequestCategory, RequestDecomposer, RequestItem, RequestStatus, RequestType,
)
from .request_parallel import BatchSessionMutationCommitter, SessionMutationProposal, SharedRunContext
from .runtime_services import CollaborationRuntimeServices
from .session_state import SessionStateStore
from .specialist_runtime import SpecialistRuntime
from .write_runtime import WriteRequestExecutor
from .workers.common import execution_safe_value


def _canonical_business_name(value: str) -> str:
    raw = str(value or "").strip()
    mapped = LEGACY_OUTPUT_NAME_MAP.get(raw, raw)
    try:
        return normalize_data_name(mapped)
    except Exception:
        return mapped


def _artifact_business_values(artifact: dict[str, Any], wanted_outputs: set[str]) -> dict[str, Any]:
    """Extract canonical reusable BusinessData values from one stored Tool artifact."""
    content = dict(artifact.get("content") or {}) if isinstance(artifact, dict) else {}
    result = dict(content.get("result") or {}) if isinstance(content.get("result"), dict) else {}
    data = dict(result.get("data") or {}) if isinstance(result.get("data"), dict) else {}
    business_data = dict(data.get("business_data") or {}) if isinstance(data.get("business_data"), dict) else {}
    slots = dict(data.get("slots") or {}) if isinstance(data.get("slots"), dict) else {}
    produced_outputs = [
        _canonical_business_name(value)
        for value in content.get("produced_outputs") or []
        if str(value).strip()
    ]
    values: dict[str, Any] = {}
    for wanted in sorted(wanted_outputs):
        canonical = _canonical_business_name(wanted)
        if canonical in business_data:
            values[canonical] = business_data[canonical]
            continue
        raw_matches = [
            key for key in list(business_data) + list(slots) + list(data)
            if _canonical_business_name(key) == canonical
        ]
        if raw_matches:
            key = raw_matches[0]
            if key in business_data:
                values[canonical] = business_data[key]
            elif key in slots:
                values[canonical] = slots[key]
            else:
                values[canonical] = data[key]
            continue
        if canonical in produced_outputs and len(produced_outputs) == 1 and data:
            # Tool contracts often expose one semantic output slot whose value is the
            # whole structured data payload rather than a nested key.
            values[canonical] = data
    return values


def _dedupe_refs(refs: list[GraphRef]) -> list[GraphRef]:
    result: list[GraphRef] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        key = (ref.node_id, ref.role)
        if key in seen:
            continue
        seen.add(key)
        result.append(ref)
    return result


def _graph_ref_semantic_type(ref: GraphRef) -> str:
    node_id = str(getattr(ref, "node_id", "") or "").lower()
    role = str(getattr(ref, "role", "") or "").lower()
    if node_id.startswith("cn:security:"):
        return "security"
    if "portfolio" in node_id or role in {"portfolio", "holding"}:
        return "portfolio"
    if ref.node_kind in {GraphNodeKind.EVIDENCE, GraphNodeKind.ASSERTION} or role in {"event", "cause"}:
        return "event"
    return "unknown"


def _refs_for_semantic_type(refs: list[GraphRef], semantic_type: str) -> list[GraphRef]:
    wanted = str(semantic_type or "").strip().lower()
    if wanted in {"", "none", "unknown"}:
        return list(refs)
    return [ref for ref in refs if _graph_ref_semantic_type(ref) == wanted]


def _walk_graph_refs(value: Any, *, depth: int = 0) -> list[GraphRef]:
    if depth > 8:
        return []
    refs: list[GraphRef] = []
    if isinstance(value, GraphRef):
        return [value]
    if isinstance(value, dict):
        if value.get("node_id") and value.get("node_kind"):
            try:
                refs.append(GraphRef.from_dict(value))
            except Exception:
                pass
        for key, item in value.items():
            if str(key).lower() in {"api_key", "password", "secret", "confirmation_token", "raw_payload"}:
                continue
            refs.extend(_walk_graph_refs(item, depth=depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in list(value)[:200]:
            refs.extend(_walk_graph_refs(item, depth=depth + 1))
    return _dedupe_refs(refs)


def _clarification_question(items: list[MissingContextItem], language: str) -> str:
    descriptions = [item.description for item in items if item.description]
    if language == "en":
        return "Please provide or select: " + "; ".join(descriptions[:4])
    return "请补充或选择：" + "；".join(descriptions[:4])


def _authoritative_entity_catalog(
    resolution_audit: dict[str, Any],
    focus_refs: list[GraphRef],
) -> list[dict[str, Any]]:
    """Preserve labels confirmed by GraphRef resolution for downstream reporting."""

    selected_ids = {ref.node_id for ref in focus_refs}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list((resolution_audit or {}).get("items") or []):
        if not isinstance(item, dict):
            continue
        resolution = item.get("resolution") if isinstance(item.get("resolution"), dict) else {}
        for candidate in list(resolution.get("candidates") or []):
            if not isinstance(candidate, dict):
                continue
            graph_ref = candidate.get("graph_ref") if isinstance(candidate.get("graph_ref"), dict) else {}
            node_id = str(graph_ref.get("node_id") or "")
            if not node_id or node_id not in selected_ids or node_id in seen:
                continue
            display_label = str(candidate.get("display_name") or "").strip()
            public_code = node_id.rsplit(":", 1)[-1]
            if not (len(public_code) == 6 and public_code.isdigit()):
                public_code = ""
            rows.append(
                {
                    "entity_ref": dict(graph_ref),
                    "node_id": node_id,
                    "public_code": public_code,
                    "display_label": display_label,
                    "identity_source": str(candidate.get("matched_by") or graph_ref.get("source") or ""),
                    "identity_locked": bool(graph_ref.get("locked")),
                }
            )
            seen.add(node_id)
    return rows


def _bind_authoritative_task_context(
    tasks: list[GraphAgentTask],
    *,
    entity_catalog: list[dict[str, Any]],
) -> None:
    for task in tasks:
        task.metadata["authoritative_entity_catalog"] = [dict(item) for item in entity_catalog]


def _bind_task_completion_contracts(
    tasks: list[GraphAgentTask],
    *,
    directory: CapabilityWorkerDirectory,
) -> None:
    # Capability contracts are already carried by each task.  Runtime no longer
    # compiles a task-type completion contract.
    del tasks, directory


def _planning_memory_summary(
    *,
    session_summary: str,
    long_term_memory_summary: str,
    inherit_previous_focus: bool,
) -> str:
    """Build the memory view that may influence business-intent planning.

    RequestBundle ContextBinding owns whether the current request inherits the previous
    conversation focus.  When it explicitly rejects that inheritance, the
    canonical-intent planner must not be allowed to re-introduce a previous
    named financial entity through ``session_summary``.  Long-term memory stays
    available for non-focus user preferences/constraints; authoritative entity
    identity still comes exclusively from GraphRef resolution.
    """

    items: list[str] = []
    if inherit_previous_focus and str(session_summary or "").strip():
        items.append(str(session_summary).strip())
    if str(long_term_memory_summary or "").strip():
        items.append(str(long_term_memory_summary).strip())
    return "\n".join(items)[:4800]


def _has_forward_replan_context_blocker(observations: list[dict[str, Any]]) -> bool:
    """Return True when a failure belongs to the context/authority layer.

    Worker forward-replan may replace failed business Workers, but it must not
    manufacture user input or authoritative GraphRefs that only the context and
    identity layers are allowed to establish.
    """

    return any(
        str(item.get("failure_kind") or "") in {
            "user_input_required",
            "worker_context_unresolved",
        }
        for item in observations
    )



class AgentCollaborationCoordinator:
    """Existing Main-Agent pattern with a Neo4j/GraphRef-only data boundary."""

    def __init__(
        self,
        *,
        output_dir: str | Path,
        db_path: str | Path | None,
        llm_service: LLMService,
        graph_settings: Neo4jSettings | None = None,
        runtime_services: CollaborationRuntimeServices | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.db_path = db_path
        self.resource_budget = RuntimeResourceBudget()
        self.llm_service = LLMConcurrencyGate(llm_service, self.resource_budget)
        self.runtime_services = runtime_services
        self.session_state = SessionStateStore(output_dir=output_dir)
        self.checkpoints = RunCheckpointStore(output_dir)
        self.context_hydrator = ContextHydrator(
            session_state=self.session_state,
            checkpoint_store=self.checkpoints,
            output_dir=str(output_dir),
        )
        self.sufficiency_gate = ContextAndEntitySufficiencyGate()
        self.directory = CapabilityWorkerDirectory()
        settings = graph_settings or Neo4jSettings.from_env()
        self.store = Neo4jFinancialGraphStore(settings)
        self.store.verify_connectivity()
        self.store.ensure_schema()
        self.identity = GraphEntityIdentityService(self.store)
        validator = GraphPatchValidator(self.store)
        self.write_executor = WriteRequestExecutor(
            output_dir=self.output_dir, db_path=self.db_path, graph_validator=validator
        )
        provider = GraphProviderAdapter(
            identity=self.identity,
            evidence_ingestion=EvidenceIngestionService(validator),
            portfolio_graph=PortfolioGraphService(self.identity, validator),
        )
        self.specialist = SpecialistRuntime(
            llm_service=self.llm_service,
            provider=provider,
            impact_service=GraphImpactService(self.store),
            directory=self.directory,
            resource_budget=self.resource_budget,
        )
        # RequestBundle is the only public semantic entry.
        self.request_decomposer = RequestDecomposer(llm_service=self.llm_service)
        self.presentation_policy_resolver = PresentationPolicyResolver()
        self.presentation_validator = PresentationValidator()
        self.session_mutation_committer = BatchSessionMutationCommitter(self.session_state)
        self.planner = CoordinatorPlanner(
            self.directory,
            llm_service=self.llm_service,
            worker_tool_directory=self.specialist.worker_tool_directory,
        )

    def close(self) -> None:
        self.store.close()

    def _memory_refs(self, session_id: str) -> list[GraphRef]:
        item = self.session_state.get(session_id, "active_graph_refs")
        return refs_from(item.value if item is not None else [])

    def _resolve_request_refs(
        self,
        *,
        query: str,
        inherited_refs: list[GraphRef],
        typed_inherited_refs: list[GraphRef] | None = None,
        context_refs: list[GraphRef],
        as_of_time: str,
        language: str,
        context_binding: dict[str, Any] | None = None,
        semantic_target: dict[str, Any] | None = None,
        semantic_entities: list[dict[str, Any]] | None = None,
    ) -> tuple[list[GraphRef], list[MissingContextItem], dict[str, Any]]:
        """Convert semantic entities into authoritative GraphRefs deterministically.

        LLM output is treated only as natural-language identity evidence.  Node IDs,
        security codes and canonical GraphRefs are produced exclusively by code +
        the authoritative graph/session state.
        """

        target = dict(semantic_target or {})
        entities = [
            dict(item)
            for item in list(semantic_entities or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ][:20]
        target_text = str(target.get("display_text") or "").strip()
        if str(target.get("status") or "") == "identified" and target_text:
            if not any(str(item.get("text") or "").strip() == target_text for item in entities):
                entities.insert(0, {
                    "text": target_text,
                    "entity_type": str(target.get("entity_type") or "unknown"),
                    "role": "focus",
                    "source": str(target.get("source") or "explicit"),
                })

        # 先读取本 Request 的上下文绑定。conversation_context 产生的语义实体只是
        # LLM 的自然语言判断；如果 Session 已存在类型焦点 GraphRef，则类型焦点才是
        # 指代解析的权威来源，LLM 不能覆盖它。
        binding = dict(context_binding or {})
        inherit_previous = bool(binding.get("inherit_previous_focus"))
        reference_type = str(binding.get("reference_entity_type") or "none").strip().lower()
        typed_refs = _refs_for_semantic_type(list(typed_inherited_refs or []), reference_type)
        previous_refs = _refs_for_semantic_type(list(inherited_refs or []), reference_type)

        semantic_resolved: list[GraphRef] = []
        missing: list[MissingContextItem] = []
        audit: list[dict[str, Any]] = []
        for entity in entities:
            text = str(entity.get("text") or "").strip()
            role = str(entity.get("role") or "focus")
            source = str(entity.get("source") or "explicit").strip().lower()
            resolution = self.identity.resolve_request(
                query,
                inherited_refs=[],
                role=role,
                as_of_time=as_of_time,
                explicit_mentions=[text],
            )
            audit_row = {
                "semantic_entity": dict(entity),
                "resolution": resolution.to_dict(),
            }

            # 对“它/刚才那只股票”等会话指代，类型焦点是代码侧权威事实。
            # 语义名称若能解析，必须与该 GraphRef 一致；冲突时阻塞而不是让 LLM 覆盖。
            conversation_focus_authority = (
                source == "conversation_context" and role == "focus" and bool(typed_refs)
            )
            if conversation_focus_authority:
                typed_by_id = {ref.node_id: ref for ref in typed_refs}
                resolved_ids = {ref.node_id for ref in resolution.refs}
                if resolution.refs and resolved_ids.intersection(typed_by_id):
                    matched = [typed_by_id[node_id] for node_id in resolved_ids if node_id in typed_by_id]
                    semantic_resolved.extend(matched)
                    audit_row["authority_decision"] = "semantic_hint_confirmed_by_typed_focus"
                    audit.append(audit_row)
                    continue
                if resolution.refs and not resolved_ids.intersection(typed_by_id):
                    missing.append(MissingContextItem(
                        key="conversation_focus_semantic_conflict",
                        description=(
                            f"会话摘要推断出的“{text}”与当前已确认的{reference_type or '实体'}焦点不一致，需要确认具体对象。"
                            if language != "en" else
                            "The semantic entity inferred from conversation history conflicts with the authoritative session focus."
                        ),
                        expected_format="明确名称、代码或确认沿用当前会话焦点",
                        reason="conversation-context semantic entity conflicts with authoritative typed session GraphRef",
                        searched_sources=["session typed GraphRef", "Neo4j identity", "conversation semantic entity"],
                    ))
                    audit_row["authority_decision"] = "semantic_hint_conflicts_with_typed_focus"
                    audit.append(audit_row)
                    continue
                # 语义名称本身无法落到图实体时，不让 LLM 的字符串阻塞已有的权威类型焦点。
                semantic_resolved.extend(typed_refs)
                audit_row["authority_decision"] = "typed_focus_used_when_semantic_hint_unresolved"
                audit.append(audit_row)
                continue

            audit.append(audit_row)
            if resolution.ambiguous_mentions:
                missing.append(MissingContextItem(
                    key="ambiguous_graph_entity",
                    description=f"“{text}”对应多个金融对象，需要选择具体对象。",
                    expected_format="从候选对象中选择一个",
                    reason="语义实体不能替代权威实体解析。",
                    searched_sources=["Neo4j identity", "Neo4j aliases", "Neo4j fulltext candidates"],
                ))
            elif resolution.unresolved_mentions:
                missing.append(MissingContextItem(
                    key="unresolved_graph_entity",
                    description=f"无法在权威金融图中确认“{text}”。",
                    expected_format="明确名称、交易所代码或已导入的 GraphRef",
                    reason="语义实体存在，但权威实体不存在或证券主数据尚未导入。",
                    searched_sources=["Neo4j identity", "Neo4j aliases"],
                ))
            else:
                semantic_resolved.extend(resolution.refs)
        if semantic_resolved:
            focus = semantic_resolved
        elif missing:
            # 语义实体已经进入权威解析但发生歧义/冲突/未解析时，禁止偷偷回退到
            # 上一轮焦点，否则会把“需要用户确认”误变成旧实体继续执行。
            focus = []
        elif context_refs:
            focus = [
                ref for ref in context_refs
                if ref.role in {"focus", "cause", "impact_target", "comparison", "event"}
            ]
            focus = _refs_for_semantic_type(focus or context_refs, reference_type)
        elif inherit_previous and previous_refs:
            focus = previous_refs
        elif reference_type not in {"", "none", "unknown"} and typed_refs:
            focus = typed_refs
            audit.append({
                "semantic_entity": {
                    "text": "<typed_conversation_focus>",
                    "entity_type": reference_type,
                    "role": "focus",
                    "source": "session_authority",
                },
                "resolution": {
                    "source": f"typed_graph_focus:{reference_type}",
                    "ref_count": len(typed_refs),
                },
            })
        else:
            focus = []

        entity_scope = str(binding.get("entity_scope") or "none").strip().lower()
        target_status = str(target.get("status") or "not_required").strip().lower()
        if not focus and not missing and target_status == "missing":
            missing.append(MissingContextItem(
                key=f"missing_semantic_target:{str(target.get('entity_type') or reference_type or 'unknown')}",
                description=(
                    "当前业务需要具体目标对象，但从本轮问题和会话摘要中仍无法确定。"
                    if language != "en" else
                    "This request needs a concrete target, but it cannot be identified from the current message and conversation summary."
                ),
                expected_format="明确目标名称、代码或可唯一识别的对象描述",
                reason="semantic_target.status=missing; authority resolution must not invent a target",
                searched_sources=["current user message", "session summary", f"typed_graph_focus:{reference_type}"],
            ))
        if (
            not focus
            and reference_type not in {"", "none", "unknown"}
            and entity_scope in {"conversation_focus", "explicit_entities"}
            and not missing
        ):
            missing.append(MissingContextItem(
                key=f"unresolved_conversation_{reference_type}",
                description=(
                    "无法确认当前指代的是哪只证券，请明确证券名称或代码。"
                    if reference_type == "security"
                    else f"无法确认当前指代的{reference_type}对象，请明确具体对象。"
                ),
                expected_format="明确名称、代码或已解析GraphRef",
                reason=(
                    "typed entity reference requested but no authoritative current/typed focus is available; "
                    "authority resolution must not invent the referenced entity"
                ),
                searched_sources=[
                    "semantic entity GraphRef resolution",
                    "runtime context GraphRefs",
                    "previous active GraphRefs",
                    f"typed_graph_focus:{reference_type}",
                ],
            ))
        return _dedupe_refs(focus), missing, {
            "semantic_target": target,
            "semantic_entities": entities,
            "items": audit,
            "context_binding": binding,
            "typed_focus_source_count": len(typed_refs),
        }

    @staticmethod
    def _request_dependency_usable(status: str) -> bool:
        return str(status or "") in {
            RequestStatus.COMPLETED.value,
            RequestStatus.PARTIALLY_COMPLETED.value,
            RequestStatus.WAITING_APPROVAL.value,
            RequestStatus.PRESENTATION_APPLIED.value,
        }

    @staticmethod
    def _classify_request_result(result: dict[str, Any]) -> RequestStatus:
        worker_results = [
            dict(item) for item in dict(result.get("worker_results") or {}).values()
            if isinstance(item, dict)
        ]
        # Proposal creation completes a READ Request. Proposal lifecycle stays
        # independently PENDING_APPROVAL in the canonical ProposalStore.
        if any(str(item.get("status") or "") == ResultStatus.PROPOSAL_READY.value for item in worker_results):
            return RequestStatus.COMPLETED
        failure_kinds = {
            str((item.get("completion") or {}).get("failure_kind") or "")
            for item in worker_results
            if isinstance(item.get("completion"), dict)
        }
        failure_kinds.update(
            str((item.get("error") or {}).get("error_id") or (item.get("error") or {}).get("code") or "")
            for item in worker_results
            if isinstance(item.get("error"), dict)
        )
        if "user_input_required" in failure_kinds:
            return RequestStatus.WAITING_USER_INPUT
        if str(result.get("execution_status") or "") == "waiting_context":
            return RequestStatus.WAITING_CONTEXT
        # A failed provider must not erase useful downstream analysis. If at least
        # one branch completed and the request is explicitly partial, preserve the
        # partial result instead of collapsing the whole Request into TOOL_FAILED.
        if str(result.get("execution_status") or "") == "partially_completed":
            return RequestStatus.PARTIALLY_COMPLETED
        if any(kind in {"tool_execution_failure", "worker_execution_failure", "structured_output_failure"} or "tool" in kind for kind in failure_kinds if kind):
            return RequestStatus.TOOL_FAILED
        business_statuses = {
            str((item.get("completion") or {}).get("business_status") or "")
            for item in worker_results
            if isinstance(item.get("completion"), dict)
        }
        if business_statuses and business_statuses <= {"empty", "business_empty"}:
            return RequestStatus.BUSINESS_EMPTY
        if bool(result.get("success")):
            return RequestStatus.COMPLETED
        return RequestStatus.FAILED

    def _materialize_reuse_decisions(
        self,
        *,
        reuse_decisions: list[dict[str, Any]],
        reuse_candidates: list[dict[str, Any]],
        request_need_contract: dict[str, Any],
        user_id: str,
        session_id: str,
        run_id: str,
        focus_refs: list[GraphRef],
    ) -> dict[str, Any]:
        """Publish verified historical Artifact values into this RunContextStore.

        Reuse is Need-level. A failed parent Request does not invalidate a successful
        Tool Artifact; only artifacts whose declared produced_outputs cover the reused
        Need are materialized.
        """
        bundle = self.specialist.context_bundle
        if bundle is None or not reuse_decisions:
            return {"materialized": [], "rejected": [], "count": 0}

        candidate_by_turn = {
            str(item.get("turn_id") or ""): dict(item)
            for item in reuse_candidates or []
            if str(item.get("turn_id") or "")
        }
        need_by_id = {
            str(item.get("need_id") or ""): dict(item)
            for item in request_need_contract.get("needs") or []
            if isinstance(item, dict) and str(item.get("need_id") or "")
        }
        store = ArtifactStore(db_path=self.db_path, output_dir=self.output_dir)
        entity_ref = focus_refs[0].to_dict() if focus_refs else None
        materialized: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        for decision in reuse_decisions:
            need_id = str(decision.get("need_id") or "")
            turn_id = str(decision.get("turn_id") or "")
            candidate = dict(candidate_by_turn.get(turn_id) or {})
            need = dict(need_by_id.get(need_id) or {})
            wanted = {
                str(req.get("data_name") or "")
                for req in need.get("requirements") or []
                if req.get("direction") == "output"
                and req.get("kind") == "data"
                and req.get("necessity") == "required"
                and str(req.get("data_name") or "")
            }
            if not candidate:
                rejected.append({"need_id": need_id, "turn_id": turn_id, "reason": "candidate_not_loaded"})
                continue
            remaining = set(wanted)
            for artifact_ref in candidate.get("artifact_refs") or []:
                if not isinstance(artifact_ref, dict) or not remaining:
                    continue
                declared = {
                    _canonical_business_name(name)
                    for name in artifact_ref.get("produced_outputs") or []
                    if str(name).strip()
                }
                relevant = remaining.intersection(declared)
                if not relevant:
                    continue
                artifact_id = str(artifact_ref.get("artifact_id") or "")
                artifact = store.read(
                    artifact_id,
                    user_id=user_id,
                    conversation_id=session_id,
                ) if artifact_id else None
                if not artifact:
                    rejected.append({
                        "need_id": need_id, "turn_id": turn_id,
                        "artifact_id": artifact_id, "reason": "artifact_unreadable",
                    })
                    continue
                values = _artifact_business_values(artifact, relevant)
                for name, value in values.items():
                    target_ref = None if name == "ranking" else entity_ref
                    bundle.put_business_data(
                        entity_ref=target_ref,
                        name=name,
                        value=value,
                        data_time=str(artifact_ref.get("trade_date") or candidate.get("trade_date") or ""),
                        contract=str(artifact.get("contract") or name),
                        version=str(artifact.get("version") or "1.0"),
                        schema_id=str(artifact.get("schema_id") or ""),
                        provenance={
                            "producer_type": "historical_artifact",
                            "artifact_id": artifact_id,
                            "source_turn_id": turn_id,
                            "source_run_id": str(candidate.get("source_run_id") or ""),
                            "reused_for_need_id": need_id,
                            "reused_into_run_id": run_id,
                        },
                    )
                    remaining.discard(name)
                    materialized.append({
                        "need_id": need_id,
                        "turn_id": turn_id,
                        "artifact_id": artifact_id,
                        "data_name": name,
                    })
            if remaining:
                rejected.append({
                    "need_id": need_id,
                    "turn_id": turn_id,
                    "reason": "required_reuse_output_not_materialized",
                    "missing_data_names": sorted(remaining),
                })

        payload = {
            "materialized": materialized,
            "rejected": rejected,
            "count": len(materialized),
        }
        runtime_debug_event(
            "REUSE_BUSINESS_DATA_MATERIALIZED",
            payload,
            run_id=run_id,
            level="WARNING" if rejected else "DEBUG",
        )
        return payload

    def _materialize_request_payload(
        self,
        *,
        request: RequestItem,
        result: dict[str, Any],
        run_id: str,
    ) -> dict[str, Any]:
        """Build the verified Request payload without a point-to-point data bus."""
        del run_id
        business_data: dict[str, list[Any]] = {}
        for row in dict(result.get("worker_results") or {}).values():
            if not isinstance(row, dict):
                continue
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            values = data.get("business_data") if isinstance(data.get("business_data"), dict) else {}
            for name, value in values.items():
                business_data.setdefault(str(name), []).append(value)
        proposal_meta: dict[str, Any] = {}
        for row in dict(result.get("worker_results") or {}).values():
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            data = row.get("data") if isinstance(row.get("data"), dict) else {}
            candidate = {**data, **metadata}
            if str(candidate.get("proposal_id") or "").startswith("proposal_"):
                proposal_meta = {
                    "proposal_id": str(candidate.get("proposal_id") or ""),
                    "proposal_version": int(candidate.get("proposal_version") or 0),
                    "proposal_status": str(candidate.get("proposal_status") or "pending_approval"),
                    "payload_hash": str(candidate.get("payload_hash") or ""),
                }
                break
        return {
            "request_id": request.request_id,
            "source_index": request.source_index,
            "category": request.category.value,
            "request_type": request.request_type.value if request.category == RequestCategory.BUSINESS else "",
            "proposal_required": bool(request.proposal_required),
            "objective": request.objective,
            "status": request.status.value,
            "status_reason": request.status_reason,
            "depends_on": list(request.depends_on),
            "need_completion": dict(result.get("need_completion") or {}),
            "context_sufficiency": dict(result.get("context_sufficiency") or {}),
            "business_effect_applied": False,
            **proposal_meta,
            "business_data": business_data,
            "warnings": list(result.get("warnings") or []),
            "errors": list(result.get("errors") or []),
        }

    @staticmethod
    def _request_coverage(bundle: RequestBundle, request_results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for request in bundle.requests:
            result = dict(request_results.get(request.request_id) or {})
            status = str(result.get("status") or request.status.value)
            counts[status] = counts.get(status, 0) + 1
            rows.append({
                "request_id": request.request_id,
                "source_index": request.source_index,
                "category": request.category.value,
                "objective": request.objective,
                "request_type": request.request_type.value if request.category == RequestCategory.BUSINESS else "",
                "proposal_required": bool(request.proposal_required),
                "depends_on": list(request.depends_on),
                "status": status,
                "reason": str(result.get("reason") or request.status_reason or ""),
            })
        unresolved = [
            row for row in rows
            if row["status"] not in {
                RequestStatus.COMPLETED.value,
                RequestStatus.PARTIALLY_COMPLETED.value,
                RequestStatus.WAITING_APPROVAL.value,
                RequestStatus.UNSUPPORTED.value,
                RequestStatus.PRESENTATION_APPLIED.value,
                RequestStatus.BUSINESS_EMPTY.value,
                RequestStatus.TOOL_FAILED.value,
                RequestStatus.FAILED.value,
                RequestStatus.BLOCKED.value,
            }
        ]
        return {
            "schema_version": "request_coverage.v2",
            "request_count": len(rows),
            "counts_by_status": counts,
            "requests": rows,
            "all_terminal": not unresolved,
            "unresolved_request_ids": [row["request_id"] for row in unresolved],
        }

    def _build_shared_run_context(
        self, *, user_id: str, session_id: str, run_id: str,
        session_preference: dict[str, Any], execution_context: dict[str, Any],
    ) -> SharedRunContext:
        profile_item = self.session_state.get(session_id, "user_profile_state")
        user_profile = (
            dict(profile_item.value or {})
            if profile_item is not None and isinstance(profile_item.value, dict) else {}
        )
        cards = self.directory.list() if hasattr(self.directory, "list") else []
        worker_catalog = [
            {
                "worker_id": card.worker_id, "agent_id": card.agent_id, "role": card.role,
                "short_description": card.short_description,
                "supported_boundary_ids": list(card.supported_boundary_ids),
                "capability_tags": list(card.capability_tags),
                "can_mutate": bool(card.can_mutate),
                "execution_stage": card.execution_stage,
                "working_memory_mode": card.working_memory_mode,
                "execution_mode": card.execution_mode,
            }
            for card in cards
        ]
        registry = getattr(getattr(self, "planner", None), "registry", None)
        capability_snapshot = {
            "semantic_requirements": registry.semantic_requirement_catalog() if registry is not None else [],
            "business_boundaries": registry.public_catalog(effect_limit="read") if registry is not None else [],
        }
        market_context = dict(
            execution_context.get("global_market_context")
            or execution_context.get("market_context")
            or {}
        ) if isinstance(
            execution_context.get("global_market_context")
            or execution_context.get("market_context")
            or {}, dict
        ) else {}
        shared = SharedRunContext(
            user_id=user_id, session_id=session_id, run_id=run_id,
            user_profile_snapshot=user_profile,
            session_preference_snapshot=dict(session_preference or {}),
            global_market_context=market_context,
            worker_public_catalog=worker_catalog,
            capability_registry_snapshot=capability_snapshot,
            runtime_configuration=(self.resource_budget.snapshot() if hasattr(self, "resource_budget") else {}),
            shared_context_refs=[
                dict(item) for item in execution_context.get("shared_context_refs") or []
                if isinstance(item, dict)
            ],
        )
        return shared

    def _commit_request_batch_mutations(
        self, *, run_id: str, batch_index: int, proposals: list[SessionMutationProposal],
    ) -> dict[str, Any]:
        flow_event(
            "REQUEST_BATCH_BARRIER_ENTERED",
            {"batch_index": batch_index, "mutation_proposal_count": len(proposals)},
            run_id=run_id,
        )
        commit = self.session_mutation_committer.commit(proposals) if proposals else {
            "schema_version": "request_batch_session_commit.v1",
            "proposal_count": 0, "operation_count": 0, "committed": [], "conflicts": [],
        }
        if commit.get("conflicts"):
            flow_event(
                "REQUEST_BATCH_CONFLICT_DETECTED",
                {"batch_index": batch_index, "conflicts": commit.get("conflicts")},
                run_id=run_id, level="WARNING",
            )
        flow_event(
            "REQUEST_BATCH_COMMITTED",
            {"batch_index": batch_index, **commit},
            run_id=run_id,
        )
        return commit

    def _build_bundle_report_task(
        self, *, run_id: str, session_id: str, user_id: str, objective: str,
        include_validation_feedback: bool = False,
    ) -> GraphAgentTask:
        card = self.directory.get("W06")
        task_id = "BUNDLE-FINAL-T02" if include_validation_feedback else "BUNDLE-FINAL-T01"
        return GraphAgentTask(
            task_id=task_id, run_id=run_id, session_id=session_id, assigned_agent=card.agent_id,
            worker_id="W06", objective=objective, user_id=user_id, boundary_id=card.role,
            contracts=[{
                "contract_id": "BUNDLE-FINAL-C01",
                "description": "根据Request Coverage与PresentationPolicy生成最终用户回答",
                "required_data": [], "required_parameters": [],
                "promised_data": [
                    {"name": "report", "required_paths": []},
                    {"name": "result.user_facing", "required_paths": []},
                ],
                "acceptance_rule_ids": ["schema_valid", "no_new_business_claims"],
                "forbidden_data_names": [], "criticality": "required",
                "mutation_allowed": False,
                "allowed_terminal_states": ["completed", "business_empty", "business_insufficient"],
            }],
            business_parameters={"report_goal": objective}, dependency_task_ids=[],
            expected_data_names=["report", "result.user_facing"], effect_limit="read",
            execution_mode=card.execution_mode, focus_refs=[], context_refs=[], priority=1,
            metadata={"request_id": "BUNDLE_FINAL", "bundle_report": True,
                      "presentation_policy_required": True},
        )

    @staticmethod
    def _report_content(result: GraphWorkerResult | None) -> str:
        """Return W06 content only after the existing FinalReport contract completed.

        W06 validity is decided once, here in Coordinator. Web/API layers must not
        reinterpret ``success`` or rescan Worker results to decide whether the answer
        exists.
        """
        if result is None or result.status != ResultStatus.COMPLETED:
            return ""
        if str(result.output_type or "") != "FinalReport" or not isinstance(result.data, dict):
            return ""
        completion = dict(result.completion or {})
        if not (
            completion.get("expected_task_completed")
            and str(completion.get("completion_status") or "") == "completed"
        ):
            return ""
        content = str(result.data.get("content") or "").strip()
        if content:
            return content
        data = result.data.get("business_data") if isinstance(result.data.get("business_data"), dict) else {}
        return str(data.get("report") or data.get("result.user_facing") or "").strip()

    def execute(
        self,
        *,
        query: str,
        decomposition: dict[str, Any],
        user_id: str,
        default_top_k: int,
        session_id: str,
        run_id: str,
        language: str,
        execution_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """RequestBundle-only entry with Ready-Batch BUSINESS parallelism.

        One user message may contain multiple Requests. READ business requests are
        independently planned into Need lists; WRITE requests use the deterministic
        WriteRequestExecutor fast path; PRESENTATION resolves into one authoritative
        PresentationPolicy. Worker DAGs remain deterministic Runtime compilations.
        """

        if not hasattr(self, "resource_budget"):
            self.resource_budget = RuntimeResourceBudget(max_parallel_requests=3, max_parallel_workers=4, max_parallel_tools=4, max_parallel_llm=4)
        if not hasattr(self, "session_mutation_committer"):
            self.session_mutation_committer = BatchSessionMutationCommitter(self.session_state)
        if self.runtime_services is not None:
            self.runtime_services.validate_identity(run_id=run_id, user_id=user_id, session_id=session_id)
        context = dict(execution_context or {})
        run_context_bundle = ContextBundle(user_id=user_id, conversation_id=session_id, run_id=run_id, locale=language)
        if hasattr(self, "specialist"):
            self.specialist.bind_context_bundle(run_context_bundle)
        context["context_bundle_id"] = run_context_bundle.context_id
        memory_summary = self.session_state.build_summary(session_id, limit=40)
        flow_event(
            "REQUEST_DECOMPOSITION_STARTED",
            {
                "request": str(query or ""),
                "memory_summary_chars": len(memory_summary),
                "execution_context_keys": sorted(context.keys()),
            },
            run_id=run_id,
        )
        # 构建复用候选清单（ConversationInventory 唯一入口）；清单不可用时按空候选降级，
        # 不影响主链路（无候选 = 无复用，一切照常计算）
        try:
            reuse_inventory = build_conversation_inventory(self.db_path, user_id=user_id, debug_run_id=run_id)
        except Exception:
            reuse_inventory = {"items": [], "guards": {}}
        bundle = self.request_decomposer.decompose(
            query=query,
            memory_summary=memory_summary,
            execution_context=context,
            language=language,
            run_id=run_id,
            reuse_candidates=reuse_inventory,
        )
        flow_event("REQUEST_BUNDLE_CREATED", bundle.to_dict(), run_id=run_id)
        for item in bundle.requests:
            flow_event(
                "REQUEST_CLASSIFIED",
                {
                    "request_id": item.request_id,
                    "source_index": item.source_index,
                    "category": item.category.value,
                    "request_type": item.request_type.value if item.category == RequestCategory.BUSINESS else "",
                    "proposal_required": bool(item.proposal_required),
                    "status": item.status.value,
                    "depends_on": list(item.depends_on),
                    "scope": item.scope,
                },
                run_id=run_id,
                level="WARNING" if item.status == RequestStatus.UNSUPPORTED else "INFO",
            )

        session_pref_item = self.session_state.get(session_id, PresentationPolicyResolver.SESSION_KEY)
        session_pref = dict(session_pref_item.value or {}) if session_pref_item is not None and isinstance(session_pref_item.value, dict) else {}
        presentation_policy = self.presentation_policy_resolver.resolve(
            bundle=bundle,
            session_preference=session_pref,
            system_language=language,
        )
        flow_event(
            "PRESENTATION_POLICY_RESOLVED",
            presentation_policy.to_dict(),
            run_id=run_id,
        )
        bundle_session_mutations = SessionMutationProposal(
            request_id="BUNDLE", source_index=0
        )
        if presentation_policy.session_update:
            persisted = dict(session_pref)
            persisted.update(presentation_policy.session_update)
            bundle_session_mutations.add_put(
                session_id=session_id,
                key=PresentationPolicyResolver.SESSION_KEY,
                value={
                    name: str(persisted.get(name) or "")
                    for name in ("language", "style", "length", "format")
                },
                value_type="presentation_preference",
                summary="会话级回答呈现偏好。",
                source_type="request_bundle",
                source_ref=run_id,
                confirmed=True,
                confidence=1.0,
            )

        # Store the original user turn once. Individual Business Request
        # execution must not overwrite the user's source message boundary.
        self.session_state.put(
            session_id=session_id,
            key=f"turn:{run_id}:user_message",
            value={"user_id": user_id, "message": str(query or "")},
            value_type="conversation_turn",
            summary=str(query or "")[:500],
            source_type="user_message",
            source_ref=run_id,
            confirmed=True,
            confidence=1.0,
        )

        context["request_bundle"] = bundle.to_dict()
        context["presentation_policy"] = presentation_policy.to_dict()
        context["language"] = presentation_policy.language or language

        shared_run_context = self._build_shared_run_context(
            user_id=user_id, session_id=session_id, run_id=run_id,
            session_preference=session_pref, execution_context=context,
        )
        context["shared_run_context"] = shared_run_context.to_dict()
        flow_event(
            "SHARED_RUN_CONTEXT_CREATED",
            {
                "schema_version": "shared_run_context.v1",
                "read_only": True,
                "worker_catalog_count": len(shared_run_context.worker_public_catalog),
                "semantic_requirement_count": len(
                    shared_run_context.capability_registry_snapshot.get("semantic_requirements") or []
                ),
                "resource_budget": self.resource_budget.snapshot(),
            },
            run_id=run_id,
        )

        request_results: dict[str, dict[str, Any]] = {}
        materialized_results: dict[str, dict[str, Any]] = {}
        pending = {item.request_id: item for item in bundle.requests}
        request_batches: list[dict[str, Any]] = []
        request_batch_index = 0
        batch_commit_audit: list[dict[str, Any]] = []

        flow_event(
            "REQUEST_DEPENDENCIES_RESOLVED",
            {
                "dependencies": {item.request_id: list(item.depends_on) for item in bundle.requests},
                "acyclic": True,
            },
            run_id=run_id,
        )

        def save_request_checkpoint(item: RequestItem, *, phase: str, status: str | None = None, result: dict[str, Any] | None = None) -> None:
            payload = dict(result or {})
            graph_runtime = dict(payload.get("graph_runtime") or {})
            focus_refs = list(graph_runtime.get("focus_refs") or [])
            tasks = list(dict(graph_runtime.get("planner") or {}).get("capability_plan", {}).get("tasks") or [])
            if not hasattr(self.checkpoints, "save_request"):
                return
            self.checkpoints.save_request(RequestCheckpoint(
                run_id=run_id,
                request_id=item.request_id,
                status=str(status or item.status.value),
                current_phase=phase,
                resolved_graph_refs=[dict(row) for row in focus_refs if isinstance(row, dict)],
                worker_tasks=[dict(row) for row in tasks if isinstance(row, dict)],
                missing_parameters=[
                    str(row.get("key") or "") for row in payload.get("missing_context") or []
                    if isinstance(row, dict) and "parameter" in str(row.get("reason") or "").lower()
                ],
                missing_context=[
                    str(row.get("key") or "") for row in payload.get("missing_context") or []
                    if isinstance(row, dict)
                ],
                replan_count=int(payload.get("replan_count") or 0),
            ))

        def business_call(item: RequestItem) -> dict[str, Any]:
            """Execute one Request branch without mutating parent-run state."""
            with self.resource_budget.request_slot():
                dependency_payload = {
                    dep: materialized_results[dep]
                    for dep in item.depends_on
                    if dep in materialized_results
                }
                request_context = dict(context)
                request_context["shared_run_context"] = shared_run_context.for_request()
                request_context["current_user_request"] = item.objective
                request_context["request_item"] = item.to_dict()
                request_context["reuse_guards"] = dict(bundle.reuse_guards or {})
                request_context["dependency_request_ids"] = sorted(dependency_payload)
                request_context["request_presentation_policy"] = presentation_policy.for_request(item.request_id)
                flow_event(
                    "BUSINESS_REQUEST_PLANNING_STARTED",
                    {
                        "request_id": item.request_id,
                        "objective": item.objective,
                        "request_type": item.request_type.value,
                        "proposal_required": bool(item.proposal_required),
                        "depends_on": list(item.depends_on),
                        "dependency_result_ids": sorted(dependency_payload),
                        "shared_context_read_only": True,
                    },
                    run_id=run_id,
                )
                save_request_checkpoint(item, phase="business_planning", status=RequestStatus.RUNNING.value)
                try:
                    if item.request_type != RequestType.READ:
                        raise RuntimeError("write_request_entered_read_planning_path")
                    business_result = self._execute_read_request(
                        query=item.objective,
                        decomposition={},
                        user_id=user_id,
                        default_top_k=default_top_k,
                        session_id=session_id,
                        run_id=run_id,
                        language=(presentation_policy.for_request(item.request_id).get("language") or presentation_policy.language or language),
                        execution_context=request_context,
                        proposal_required=bool(item.proposal_required),
                        context_binding=item.context_binding,
                        request_id=item.request_id,
                        task_id_prefix=f"{item.request_id}-",
                        persist_checkpoint=False,
                        persist_user_turn=False,
                        defer_session_mutations=True,
                        request_source_index=item.source_index,
                        semantic_target=item.semantic_target.to_dict(),
                        semantic_entities=[entity.to_dict() for entity in item.semantic_entities],
                    )
                    status = self._classify_request_result(business_result)
                    business_result["status"] = status.value
                    materialized = self._materialize_request_payload(
                        request=item,
                        result=business_result,
                        run_id=run_id,
                    )
                    mutation_payload = dict(business_result.get("session_mutation_proposal") or {})
                    proposal = SessionMutationProposal(
                        request_id=item.request_id,
                        source_index=item.source_index,
                        operations=[dict(row) for row in mutation_payload.get("operations") or [] if isinstance(row, dict)],
                    )
                    return {
                        "status": status,
                        "result": business_result,
                        "materialized": materialized,
                        "mutation_proposal": proposal,
                        "reason": str(business_result.get("clarification_question") or business_result.get("answer") or "")[:500],
                    }
                except Exception as exc:
                    detail = str(exc)
                    status = (
                        RequestStatus.UNSUPPORTED
                        if "unresolvable" in detail or "no_consumer_worker" in detail or "no_owner_worker" in detail
                        else RequestStatus.FAILED
                    )
                    return {
                        "status": status,
                        "result": {
                            "request_id": item.request_id,
                            "status": status.value,
                            "reason": f"{type(exc).__name__}:{detail}"[:500],
                            "exception_type": type(exc).__name__,
                        },
                        "materialized": {},
                        "mutation_proposal": SessionMutationProposal(item.request_id, item.source_index),
                        "reason": f"{type(exc).__name__}:{detail}"[:500],
                    }

        while pending:
            progressed = False
            for request_id, item in list(pending.items()):
                blocked_by = [
                    dep for dep in item.depends_on
                    if dep in request_results and not self._request_dependency_usable(str(request_results[dep].get("status") or ""))
                ]
                if not blocked_by:
                    continue
                item.status = RequestStatus.BLOCKED
                item.status_reason = f"blocked_by_request:{','.join(blocked_by)}"
                request_results[request_id] = {
                    "request_id": request_id,
                    "status": item.status.value,
                    "reason": item.status_reason,
                    "blocked_by": blocked_by,
                }
                pending.pop(request_id, None)
                save_request_checkpoint(item, phase="blocked", result=request_results[request_id])
                progressed = True
                flow_event("REQUEST_COMPLETION_UPDATED", request_results[request_id], run_id=run_id, level="WARNING")

            ready = sorted([
                item for item in pending.values()
                if all(dep in request_results and self._request_dependency_usable(str(request_results[dep].get("status") or "")) for dep in item.depends_on)
            ], key=lambda row: (row.source_index, row.request_id))
            if not ready:
                if progressed:
                    continue
                for item in list(pending.values()):
                    item.status = RequestStatus.BLOCKED
                    item.status_reason = "unresolved_request_dependency"
                    request_results[item.request_id] = {
                        "request_id": item.request_id, "status": item.status.value, "reason": item.status_reason,
                    }
                    save_request_checkpoint(item, phase="blocked", result=request_results[item.request_id])
                    pending.pop(item.request_id, None)
                break

            # WRITE is deterministic and serialized. It never enters MainAgent or
            # the READ Worker pool. Confirmation creates a new WRITE Request.
            writes = [
                item for item in ready
                if item.category == RequestCategory.BUSINESS and item.request_type == RequestType.WRITE
            ]
            if writes:
                item = writes[0]
                pending.pop(item.request_id, None)
                item.status = RequestStatus.RUNNING
                request_batch_index += 1
                batch_meta = {
                    "batch_index": request_batch_index,
                    "request_ids": [item.request_id],
                    "parallelizable": False,
                    "execution_mode": "deterministic_write_fast_path",
                    "action_type": item.action_type,
                }
                request_batches.append(batch_meta)
                flow_event("REQUEST_BATCH_STARTED", batch_meta, run_id=run_id)
                save_request_checkpoint(item, phase="write_fast_path", status=RequestStatus.RUNNING.value)
                write_result = self.write_executor.execute(
                    action_type=item.action_type,
                    query=item.objective or query,
                    user_id=user_id,
                    session_id=session_id,
                    run_id=run_id,
                    context=context,
                )
                write_status = str(write_result.get("status") or "failed")
                if write_status == "completed":
                    item.status = RequestStatus.COMPLETED
                elif write_status == "waiting_user_input":
                    item.status = RequestStatus.WAITING_USER_INPUT
                else:
                    item.status = RequestStatus.FAILED
                item.status_reason = str(write_result.get("outcome") or "")[:500]
                write_result.update({
                    "request_id": item.request_id,
                    "source_index": item.source_index,
                    "category": "business",
                    "request_type": "write",
                    "objective": item.objective,
                    "status": item.status.value,
                })
                request_results[item.request_id] = write_result
                materialized_results[item.request_id] = {
                    "request_id": item.request_id,
                    "source_index": item.source_index,
                    "category": "business",
                    "request_type": "write",
                    "proposal_required": False,
                    "objective": item.objective,
                    "status": item.status.value,
                    "status_reason": item.status_reason,
                    "depends_on": list(item.depends_on),
                    "business_effect_applied": bool(write_result.get("business_effect_applied")),
                    "outcome": str(write_result.get("outcome") or ""),
                    "proposal_id": str(write_result.get("proposal_id") or ""),
                    "proposal_version": int(write_result.get("proposal_version") or 0),
                    "proposal_status": str(write_result.get("proposal_status") or ""),
                    "mutation_status": str(write_result.get("mutation_status") or ""),
                    "warnings": list(write_result.get("warnings") or []),
                    "errors": list(write_result.get("errors") or []),
                }
                save_request_checkpoint(item, phase="write_completed", result=write_result)
                flow_event("WRITE_REQUEST_COMPLETED", write_result, run_id=run_id,
                           level="INFO" if item.status == RequestStatus.COMPLETED else "WARNING")
                continue

            # PRESENTATION and pre-classified UNSUPPORTED do not enter the parallel business pool.
            for item in list(ready):
                if item.status == RequestStatus.UNSUPPORTED:
                    pending.pop(item.request_id, None)
                    request_results[item.request_id] = {
                        "request_id": item.request_id, "status": RequestStatus.UNSUPPORTED.value,
                        "reason": item.status_reason or "request_marked_unsupported",
                    }
                    save_request_checkpoint(item, phase="unsupported", result=request_results[item.request_id])
                    flow_event("REQUEST_COMPLETION_UPDATED", request_results[item.request_id], run_id=run_id, level="WARNING")
                elif item.category == RequestCategory.PRESENTATION:
                    pending.pop(item.request_id, None)
                    item.status = RequestStatus.PRESENTATION_APPLIED
                    request_results[item.request_id] = {
                        "request_id": item.request_id, "status": item.status.value,
                        "presentation_policy": presentation_policy.to_dict(),
                    }
                    save_request_checkpoint(item, phase="presentation_applied", result=request_results[item.request_id])
                    flow_event("REQUEST_COMPLETION_UPDATED", request_results[item.request_id], run_id=run_id)

            ready_business = [
                item for item in ready
                if item.request_id in pending
                and item.category == RequestCategory.BUSINESS
                and item.request_type == RequestType.READ
            ]
            executable_ready = list(ready_business)
            if executable_ready:
                request_batch_index += 1
                batch_meta = {
                    "batch_index": request_batch_index,
                    "request_ids": [item.request_id for item in executable_ready],
                    "parallelizable": len(executable_ready) > 1,
                    "execution_mode": "ready_batch_parallel_business",
                    "max_parallel_requests": self.resource_budget.max_parallel_requests,
                }
                request_batches.append(batch_meta)
                flow_event("REQUEST_BATCH_STARTED", batch_meta, run_id=run_id)
                flow_event(
                    "REQUEST_PARALLEL_EXECUTION_STARTED",
                    {**batch_meta, "resource_budget": self.resource_budget.snapshot()},
                    run_id=run_id,
                )
                for item in executable_ready:
                    pending.pop(item.request_id, None)
                    item.status = RequestStatus.RUNNING
                outcomes: dict[str, dict[str, Any]] = {}
                max_workers = min(self.resource_budget.max_parallel_requests, len(executable_ready))
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = {pool.submit(business_call, item): item for item in executable_ready}
                    for future in as_completed(futures):
                        item = futures[future]
                        outcomes[item.request_id] = future.result()
                proposals: list[SessionMutationProposal] = []
                # Merge in source order, never future completion order.
                for item in executable_ready:
                    outcome = outcomes[item.request_id]
                    item.status = outcome["status"]
                    item.status_reason = outcome["reason"]
                    request_results[item.request_id] = outcome["result"]
                    if outcome["materialized"]:
                        materialized_results[item.request_id] = outcome["materialized"]
                    proposals.append(outcome["mutation_proposal"])
                    save_request_checkpoint(item, phase="business_completed", result=outcome["result"])
                    flow_event(
                        "BUSINESS_REQUEST_PLANNING_COMPLETED",
                        {"request_id": item.request_id, "status": item.status.value,
                         "need_completion": dict(outcome["result"].get("need_completion") or {})},
                        run_id=run_id,
                        level="INFO" if self._request_dependency_usable(item.status.value) else "WARNING",
                    )
                    flow_event(
                        "REQUEST_COMPLETION_UPDATED",
                        {"request_id": item.request_id, "status": item.status.value, "category": "business"},
                        run_id=run_id,
                        level="INFO" if self._request_dependency_usable(item.status.value) else "WARNING",
                    )
                flow_event(
                    "REQUEST_PARALLEL_EXECUTION_FINISHED",
                    {**batch_meta, "statuses": {item.request_id: item.status.value for item in executable_ready},
                     "resource_budget": self.resource_budget.snapshot()},
                    run_id=run_id,
                )
                commit = self._commit_request_batch_mutations(
                    run_id=run_id, batch_index=request_batch_index, proposals=proposals
                )
                batch_commit_audit.append(commit)
                continue

            # Only non-executable bookkeeping requests remained in this ready set.
            if not progressed and not any(item.request_id in pending for item in ready):
                continue

        # Persist bundle-level presentation preference only after Request execution.
        if bundle_session_mutations.operations:
            batch_commit_audit.append(self._commit_request_batch_mutations(
                run_id=run_id, batch_index=request_batch_index + 1,
                proposals=[bundle_session_mutations],
            ))

        coverage = self._request_coverage(bundle, request_results)
        runtime_debug_event(
            "REQUEST_STATE_MACHINE_SNAPSHOT",
            {
                "coverage": coverage,
                "request_results": {
                    request_id: {
                        "status": str(result.get("status") or ""),
                        "reason": str(result.get("reason") or ""),
                    }
                    for request_id, result in request_results.items()
                },
                "terminal_statuses": [
                    RequestStatus.COMPLETED.value,
                    RequestStatus.PARTIALLY_COMPLETED.value,
                    RequestStatus.WAITING_APPROVAL.value,
                    RequestStatus.UNSUPPORTED.value,
                    RequestStatus.PRESENTATION_APPLIED.value,
                    RequestStatus.BUSINESS_EMPTY.value,
                    RequestStatus.TOOL_FAILED.value,
                    RequestStatus.FAILED.value,
                    RequestStatus.BLOCKED.value,
                ],
            },
            run_id=run_id,
        )
        flow_event("REQUEST_COVERAGE_COMPLETED", coverage, run_id=run_id)

        bundle_results_payload = {
            "schema_version": "request_bundle_results.v2",
            "request_bundle": bundle.to_dict(),
            "request_coverage": coverage,
            "request_results": {
                item.request_id: (
                    materialized_results.get(item.request_id)
                    or {
                        "request_id": item.request_id,
                        "category": item.category.value,
                        "request_type": item.request_type.value if item.category == RequestCategory.BUSINESS else "",
                        "proposal_required": bool(item.proposal_required),
                        "objective": item.objective,
                        "status": str((request_results.get(item.request_id) or {}).get("status") or item.status.value),
                        "reason": str((request_results.get(item.request_id) or {}).get("reason") or ""),
                    }
                )
                for item in bundle.requests
            },
        }
        context["request_bundle_results"] = bundle_results_payload
        context["presentation_policy"] = presentation_policy.to_dict()

        final_task = self._build_bundle_report_task(
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            objective="按照Request原始顺序汇总本轮已验证结果，并明确未完成、待参数、待审批和不支持的Request。",
        )
        if self.runtime_services is not None:
            self.runtime_services.register_tasks([final_task])
        final_results, final_batches, final_timeline = self._run_dag(
            [final_task],
            query=query,
            output_dir=self.output_dir,
            db_path=self.db_path,
            default_top_k=default_top_k,
            language=presentation_policy.language or language,
            execution_context=context,
        )
        final_result = final_results.get(final_task.task_id)
        answer = self._report_content(final_result)
        validation = self.presentation_validator.validate(answer, presentation_policy)
        if not validation.valid:
            flow_event(
                "PRESENTATION_VALIDATION_FAILED",
                validation.to_dict(),
                run_id=run_id,
                level="WARNING",
            )
            context["presentation_validation_feedback"] = validation.to_dict()
            retry_task = self._build_bundle_report_task(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                objective="只修正上一版最终回答的呈现格式以满足PresentationPolicy；不得增加、删除或改变业务事实与结论。",
                include_validation_feedback=True,
            )
            if self.runtime_services is not None:
                self.runtime_services.register_tasks([retry_task])
            retry_results, retry_batches, retry_timeline = self._run_dag(
                [retry_task],
                query=query,
                output_dir=self.output_dir,
                db_path=self.db_path,
                default_top_k=default_top_k,
                language=presentation_policy.language or language,
                execution_context=context,
            )
            retry_result = retry_results.get(retry_task.task_id)
            retry_answer = self._report_content(retry_result)
            retry_validation = self.presentation_validator.validate(retry_answer, presentation_policy)
            if retry_answer:
                answer = retry_answer
                validation = retry_validation
                final_results.update(retry_results)
                final_batches.extend(retry_batches)
                final_timeline.extend(retry_timeline)

        if not answer:
            answer = (
                "本轮Request已完成结构化处理，但最终呈现Worker未生成有效回答。"
                if (presentation_policy.language or language) != "en"
                else "The requests were processed structurally, but the final presentation Worker did not produce a valid answer."
            )

        # Parent Run state is Request-level, not one Business sub-plan's state.
        waiting_user = [
            row["request_id"] for row in coverage["requests"]
            if row["status"] == RequestStatus.WAITING_USER_INPUT.value
        ]
        waiting_context = [
            row["request_id"] for row in coverage["requests"]
            if row["status"] in {RequestStatus.WAITING_CONTEXT.value, RequestStatus.BLOCKED.value}
        ]
        failed_requests = [
            row["request_id"] for row in coverage["requests"]
            if row["status"] in {RequestStatus.FAILED.value, RequestStatus.TOOL_FAILED.value}
        ]
        supported_terminal = [
            row for row in coverage["requests"]
            if row["status"] not in {RequestStatus.UNSUPPORTED.value, RequestStatus.PRESENTATION_APPLIED.value}
        ]
        completed_any = any(
            row["status"] in {
                RequestStatus.COMPLETED.value, RequestStatus.PARTIALLY_COMPLETED.value,
                RequestStatus.WAITING_APPROVAL.value, RequestStatus.BUSINESS_EMPTY.value,
            }
            for row in supported_terminal
        )
        execution_status = (
            "waiting_context" if waiting_user or waiting_context else
            "partially_completed" if failed_requests and completed_any else
            "failed" if failed_requests and not completed_any else
            "completed"
        )
        success = bool(completed_any and not waiting_user and not waiting_context and not failed_requests and validation.valid)
        self.checkpoints.save(RunCheckpoint(
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            status=(
                "waiting_user_input" if waiting_user else
                "waiting_context" if waiting_context else
                "completed" if execution_status == "completed" else "failed"
            ),
            current_node_id="request_bundle_final_response",
            capability_plan={
                "request_bundle": bundle.to_dict(),
                "request_coverage": coverage,
                "presentation_policy": presentation_policy.to_dict(),
            },
            task_states={
                task_id: str((payload or {}).get("status") or "")
                for request_result in request_results.values()
                for task_id, payload in dict(request_result.get("worker_results") or {}).items()
                if isinstance(payload, dict)
            },
            data_refs=[],
            working_memory_snapshot=run_context_bundle.snapshot_working_memory(),
            missing_parameters=waiting_user,
            missing_context=waiting_context,
            replan_count=sum(int(result.get("replan_count") or 0) for result in request_results.values()),
        ))

        all_worker_results: dict[str, Any] = {}
        all_timeline: list[dict[str, Any]] = []
        all_batches: list[dict[str, Any]] = []
        for request_result in request_results.values():
            all_worker_results.update(dict(request_result.get("worker_results") or {}))
            all_timeline.extend(list(request_result.get("agent_timeline") or []))
            all_batches.extend(list(request_result.get("execution_batches") or []))
        all_worker_results.update({task_id: result.handoff_view() for task_id, result in final_results.items()})
        all_timeline.extend(final_timeline)
        all_batches.extend(final_batches)

        return {
            "success": success,
            "answer": answer,
            "execution_status": execution_status,
            "request_bundle": bundle.to_dict(),
            "request_coverage": coverage,
            "request_results": request_results,
            "presentation_policy": presentation_policy.to_dict(),
            "presentation_validation": validation.to_dict(),
            "worker_results": all_worker_results,
            "business_outputs": run_context_bundle.business_data_context(),
            "tool_calls": [],
            "internal_tool_call_count": len([row for row in all_timeline if row.get("status") != "not_executed"]),
            "execution_order": [str(row.get("task_id") or "") for row in all_timeline if str(row.get("task_id") or "")],
            "execution_batches": all_batches,
            "request_execution_batches": request_batches,
            "request_parallel_metrics": {
                "request_parallelism": self.resource_budget.snapshot()["request"]["peak"],
                "max_parallel_requests": self.resource_budget.max_parallel_requests,
                "resource_budget": self.resource_budget.snapshot(),
                "batch_count": len(request_batches),
                "batch_commit_count": len(batch_commit_audit),
                "shared_context_reuse_count": len(bundle.business_requests()),
            },
            "warnings": [
                warning
                for result in request_results.values()
                for warning in result.get("warnings") or []
            ] + ([] if validation.valid else list(validation.violations)),
            "errors": [
                str(result.get("reason") or "")
                for result in request_results.values()
                if str(result.get("status") or "") in {RequestStatus.FAILED.value, RequestStatus.TOOL_FAILED.value}
            ],
            "need_clarification": bool(waiting_user),
            "clarification_question": (
                f"以下Request仍需要用户补充参数：{', '.join(waiting_user)}"
                if waiting_user and (presentation_policy.language or language) != "en" else
                f"User input is still required for: {', '.join(waiting_user)}" if waiting_user else ""
            ),
            "missing_context": waiting_context,
            "observations": coverage["requests"],
            "need_completion": {
                request_id: dict(result.get("need_completion") or {})
                for request_id, result in request_results.items()
            },
            "replan_audit": [
                item
                for result in request_results.values()
                for item in result.get("replan_audit") or []
            ],
            "replan_count": sum(int(result.get("replan_count") or 0) for result in request_results.values()),
            "invalid_replan_block_count": sum(int(result.get("invalid_replan_block_count") or 0) for result in request_results.values()),
            "replan_limits": {"request_scoped": True, "worker_forward_replan_preserved": True},
            "agent_timeline": all_timeline,
            "handoff": {
                "handoff_available": bool(all_worker_results),
                "handoff_count": len(all_worker_results),
                "handoff_refs": [f"worker_result:{task_id}" for task_id in all_worker_results],
                "safety": {
                    "worker_private_tools": True,
                    "coordinator_tool_visibility": "none",
                    "control_gateway_separate": True,
                },
            },
            "graph_runtime": {
                "contract_version": "request_bundle_capability_runtime.v1",
                "graph_id": self.store.graph_id,
                "request_bundle": bundle.to_dict(),
                "request_coverage": coverage,
                "presentation_policy": presentation_policy.to_dict(),
                "worker_dag_build_owner": "runtime_deterministic_compiler",
                "request_dependency_owner": "request_bundle_ready_batch_runtime",
                "worker_private_planning_owner": "specialist_worker",
                "request_parallel_execution": {
                    "executable_ready_batch_enabled": True,
                    "proposal_parallel_enabled": False,
                    "control_parallel_enabled": False,
                    "shared_run_context_read_only": True,
                    "cross_request_channel": "request_order_plus_context_bundle",
                    "resource_budget": self.resource_budget.snapshot(),
                    "batch_session_commits": batch_commit_audit,
                },
                "runtime_persistence": {
                    "agent_steps_connected": self.runtime_services is not None,
                    "runtime_layer": "request_dag+capability_dag+worker_tool_dag",
                },
            },
        }

    def _execute_read_request(
        self,
        *,
        query: str,
        decomposition: dict[str, Any],
        user_id: str,
        default_top_k: int,
        session_id: str,
        run_id: str,
        language: str,
        execution_context: dict[str, Any] | None,
        proposal_required: bool,
        context_binding: ContextBinding,
        request_id: str = "",
        task_id_prefix: str = "",
        persist_checkpoint: bool = True,
        persist_user_turn: bool = True,
        defer_session_mutations: bool = False,
        request_source_index: int = 0,
        semantic_target: dict[str, Any] | None = None,
        semantic_entities: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        del decomposition
        if self.runtime_services is not None:
            self.runtime_services.validate_identity(
                run_id=run_id,
                user_id=user_id,
                session_id=session_id,
            )
        context = dict(execution_context or {})
        if self.specialist.context_bundle is None or self.specialist.context_bundle.run_id != run_id:
            self.specialist.bind_context_bundle(ContextBundle(user_id=user_id, conversation_id=session_id, run_id=run_id, locale=language))
        session_mutation_proposal = SessionMutationProposal(
            request_id=str(request_id or "REQUEST"),
            source_index=int(request_source_index or 0),
        )
        memory_summary = self.session_state.build_summary(session_id, limit=40)
        proposal_required = bool(proposal_required)
        # ``read_goal`` is an internal planning-output hint only. It is not a
        # Request permission. The current Business Request remains READ.
        read_goal = "proposal" if proposal_required else "read"
        flow_event(
            "BUSINESS_REQUEST_CLASSIFIED",
            {
                "request_id": str(request_id or ""),
                "category": "business",
                "request_type": "read",
                "proposal_required": proposal_required,
                "context_binding": context_binding.to_dict(),
                "semantic_authority": "request_bundle",
            },
            run_id=run_id,
        )

        requirements = [
            ContextRequirement(
                context_key="session_summary",
                required=False,
                source_preferences=["session_state"],
            ),
            ContextRequirement(
                context_key="long_term_memory",
                required=False,
                source_preferences=["sqlite_memory_store"],
            ),
            ContextRequirement(
                context_key="pending_runs",
                required=False,
                source_preferences=["run_checkpoint"],
            ),
        ]
        if context_binding.inherit_previous_focus:
            requirements.append(ContextRequirement(
                context_key="previous_focus_entities",
                required=True,
                source_preferences=["session_state", "run_checkpoint"],
                allow_session_inheritance=True,
            ))
        reference_entity_type = context_binding.reference_entity_type.value
        if reference_entity_type not in {"none", "unknown"}:
            requirements.append(ContextRequirement(
                context_key=f"typed_focus:{reference_entity_type}",
                required=False,
                source_preferences=["session_state"],
                allow_session_inheritance=True,
            ))
        hydrated = self.context_hydrator.hydrate(
            user_id=user_id,
            session_id=session_id,
            requirements=requirements,
            query=query,
            run_id=run_id,
            execution_context=context,
        )
        context.setdefault("available_parameters", dict(hydrated.available_parameters))
        context.setdefault("permission_context", dict(hydrated.permission_context))
        planning_memory_summary = _planning_memory_summary(
            session_summary=hydrated.session_summary,
            long_term_memory_summary=hydrated.long_term_memory_summary,
            inherit_previous_focus=context_binding.inherit_previous_focus,
        )
        # Keep the full conversation summary in runtime context for audit and
        # non-business conversational handling.  The MainAgent business planner
        # receives the authority-filtered view below.
        context["memory_summary"] = memory_summary
        context["long_term_memory_refs"] = list(hydrated.long_term_memory_refs)
        flow_event(
            "CONTEXT_HYDRATED",
            {
                "source_audit": hydrated.source_audit,
                "previous_focus_ref_count": len(hydrated.previous_focus_refs),
                "typed_focus_ref_counts": {
                    key: len(refs) for key, refs in hydrated.typed_focus_refs.items()
                },
                "pending_run_ids": hydrated.pending_run_ids,
                "available_parameter_keys": sorted(hydrated.available_parameters),
                "long_term_memory_ref_count": len(hydrated.long_term_memory_refs),
            },
            run_id=run_id,
        )
        context_refs = _walk_graph_refs(context)
        inherited_refs = hydrated.previous_focus_refs
        typed_inherited_refs = list(hydrated.typed_focus_refs.get(reference_entity_type, []))
        explicit_as_of = str(context.get("as_of_time") or context.get("as_of_date") or "")
        flow_event(
            "GRAPH_REF_RESOLUTION_STARTED",
            {
                "context_ref_count": len(context_refs),
                "inherited_ref_count": len(inherited_refs),
                "typed_inherited_ref_count": len(typed_inherited_refs),
                "reference_entity_type": reference_entity_type,
                "as_of_time": explicit_as_of,
                "context_binding": context_binding.to_dict(),
            },
            run_id=run_id,
        )
        focus_refs, resolution_missing, resolution_audit = self._resolve_request_refs(
            query=query,
            inherited_refs=inherited_refs,
            typed_inherited_refs=typed_inherited_refs,
            context_refs=context_refs,
            as_of_time=explicit_as_of,
            language=language,
            context_binding=context_binding.to_dict(),
            semantic_target=semantic_target,
            semantic_entities=semantic_entities,
        )
        flow_event(
            "GRAPH_REF_RESOLUTION_COMPLETED",
            {
                "focus_ref_count": len(focus_refs),
                "focus_refs": [ref.to_dict() for ref in focus_refs],
                "missing_context": [item.to_dict() for item in resolution_missing],
                "resolution_audit": resolution_audit,
            },
            run_id=run_id,
            level="WARNING" if resolution_missing else "INFO",
        )
        if resolution_missing:
            sufficiency = self.sufficiency_gate.evaluate(
                missing_items=resolution_missing,
                available_parameters=hydrated.available_parameters,
            )
            if persist_checkpoint:
                self.checkpoints.save(RunCheckpoint(
                    run_id=run_id,
                    session_id=session_id,
                    user_id=user_id,
                    status=(
                        "waiting_user_input"
                        if sufficiency.missing_parameters or sufficiency.unresolved_entities
                        else "waiting_context"
                    ),
                    current_node_id=f"request:{request_id}:entity_resolution" if request_id else "entity_resolution",
                    blocked_task_id="",
                    resolved_entity_refs=[ref.to_dict() for ref in focus_refs],
                    missing_parameters=list(sufficiency.missing_parameters),
                    missing_context=[*sufficiency.missing_context, *sufficiency.unresolved_entities],
                ))
            flow_event("CONTEXT_SUFFICIENCY_BLOCKED", sufficiency.to_dict(), run_id=run_id, level="WARNING")
            question = _clarification_question(resolution_missing, language)
            return {
                **self._empty_result(answer=question, success=False, status="waiting_context"),
                "need_clarification": True,
                "clarification_question": question,
                "missing_context": [item.to_dict() for item in resolution_missing],
                "graph_runtime": {
                    "contract_version": "capability_contract_runtime.v1",
                    "graph_id": self.store.graph_id,
                    "resolution_audit": resolution_audit,
                },
            }
        if persist_user_turn:
            self.session_state.put(
                session_id=session_id,
                key=f"turn:{run_id}:user_message",
                value={"user_id": user_id, "message": str(query or "")},
                value_type="conversation_turn",
                summary=str(query or "")[:500],
                source_type="user_message",
                source_ref=run_id,
                confirmed=True,
                confidence=1.0,
            )
        if focus_refs:
            focus_put = {
                "session_id": session_id, "key": "active_graph_refs",
                "value": [ref.to_dict() for ref in focus_refs],
                "value_type": "graph_ref_list", "summary": "当前对话已确认的金融图对象引用。",
                "source_type": "graph_entity_resolution", "source_ref": run_id,
                "confirmed": True, "confidence": 1.0,
            }
            if defer_session_mutations:
                session_mutation_proposal.add_put(**focus_put)
            else:
                self.session_state.put(**focus_put)
            typed_groups: dict[str, list[GraphRef]] = {}
            for ref in focus_refs:
                focus_type = _graph_ref_semantic_type(ref)
                if focus_type in {"security", "portfolio", "event"}:
                    typed_groups.setdefault(focus_type, []).append(ref)
            for focus_type, refs in typed_groups.items():
                typed_put = {
                    "session_id": session_id, "key": f"typed_graph_focus:{focus_type}",
                    "value": [ref.to_dict() for ref in _dedupe_refs(refs)],
                    "value_type": "graph_ref_list",
                    "summary": f"最近一次已确认的{focus_type}类型金融图对象引用。",
                    "source_type": "graph_entity_resolution", "source_ref": run_id,
                    "confirmed": True, "confidence": 1.0,
                }
                if defer_session_mutations:
                    session_mutation_proposal.add_put(**typed_put)
                else:
                    self.session_state.put(**typed_put)

        if persist_checkpoint:
            self.checkpoints.save(RunCheckpoint(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                status="running",
                current_node_id=f"request:{request_id}:capability_planning" if request_id else "capability_planning",
                resolved_entity_refs=[ref.to_dict() for ref in focus_refs],
            ))
        flow_event(
            "WORKER_PLANNING_STARTED",
            {
                "request_id": str(request_id or ""),
                "request_type": "read",
                "proposal_required": proposal_required,
                "focus_ref_count": len(focus_refs),
                "context_ref_count": len(context_refs),
                "worker_selection_owner": "main_agent",
                "planning_mode": "business_request_need_then_worker_assignment_then_runtime_dag_compile",
                "worker_loading": "all_public_descriptions_upfront",
                "runtime_assignment_role": "validate_only",
                "raw_request_semantic_owner": "request_bundle.objective",
                "planning_memory_policy": {
                    "previous_focus_inheritance": bool(context_binding.inherit_previous_focus),
                    "session_summary_included": bool(
                        context_binding.inherit_previous_focus
                        and str(hydrated.session_summary or "").strip()
                    ),
                    "long_term_memory_included": bool(str(hydrated.long_term_memory_summary or "").strip()),
                },
            },
            run_id=run_id,
        )
        try:
            # 规则层 1：对 decompose 粗筛标记的复用候选做精筛（去重截断取完整回答裁剪版）。
            # 候选加载失败按无候选降级，不影响正常计算路线。
            reuse_candidates: list[dict[str, Any]] = []
            reuse_guards = dict(context.get("reuse_guards") or {})
            marked_turn_ids = [
                str(turn_id) for turn_id in (context_binding.reuse_reference or ())
                if str(turn_id) in reuse_guards
            ]
            if marked_turn_ids:
                try:
                    reuse_candidates = load_candidate_answers(
                        self.db_path,
                        guards=reuse_guards,
                        display_ids=marked_turn_ids,
                        debug_run_id=run_id,
                    )
                except Exception as exc:
                    runtime_debug_event(
                        "REUSE_CANDIDATE_LOAD_FAILED",
                        {
                            "marked_turn_ids": marked_turn_ids,
                            "exception_type": type(exc).__name__,
                            "message": str(exc),
                        },
                        run_id=run_id,
                        level="ERROR",
                    )
                    reuse_candidates = []
            runtime_debug_event(
                "REUSE_CANDIDATE_SELECTION_FOR_PLANNER",
                {
                    "rough_reuse_reference": list(context_binding.reuse_reference or ()),
                    "guard_turn_ids": sorted(reuse_guards),
                    "marked_turn_ids": marked_turn_ids,
                    "loaded_candidates": [
                        {
                            "turn_id": row.get("turn_id"),
                            "freshness": row.get("freshness"),
                            "reusable_outputs": row.get("reusable_outputs"),
                            "artifact_count": len(row.get("artifact_refs") or []),
                        }
                        for row in reuse_candidates
                    ],
                },
                run_id=run_id,
            )
            tasks, plan_meta = self.planner.plan(
                query=query,
                effect_limit=read_goal,
                session_id=session_id,
                run_id=run_id,
                user_id=user_id,
                focus_refs=focus_refs,
                context_refs=context_refs,
                memory_summary=planning_memory_summary,
                language=language,
                as_of_time=explicit_as_of,
                context_binding=context_binding.to_dict(),
                request_id=str(request_id or ""),
                task_id_prefix=str(task_id_prefix or ""),
                authoritative_target={
                    "semantic_target": dict(dict(context.get("request_item") or {}).get("semantic_target") or {}),
                    "graph_refs": [ref.to_dict() for ref in focus_refs],
                },
                request_constraints=[
                    str(item) for item in dict(context.get("request_item") or {}).get("constraints") or []
                    if str(item)
                ],
                reuse_candidates=reuse_candidates,
            )
            reuse_materialization = self._materialize_reuse_decisions(
                reuse_decisions=list(plan_meta.get("reuse_decisions") or []),
                reuse_candidates=reuse_candidates,
                request_need_contract=dict(plan_meta.get("request_need_contract") or {}),
                user_id=user_id,
                session_id=session_id,
                run_id=run_id,
                focus_refs=focus_refs,
            )
            plan_meta["reuse_materialization"] = reuse_materialization
        except Exception as exc:
            flow_event(
                "WORKER_PLANNING_FAILED",
                {
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                    "diagnostics": getattr(exc, "diagnostics", {}),
                },
                run_id=run_id,
                level="ERROR",
            )
            trace_exception(
                "coordinator.worker_planning.failed",
                exc,
                run_id=run_id,
            )
            raise
        entity_catalog = _authoritative_entity_catalog(resolution_audit, focus_refs)
        _bind_authoritative_task_context(tasks, entity_catalog=entity_catalog)
        _bind_task_completion_contracts(tasks, directory=self.directory)
        flow_event(
            "WORKER_PLANNING_COMPLETED",
            {
                "task_count": len(tasks),
                "planner": plan_meta,
                "tasks": [task.safe_for_coordinator() for task in tasks],
            },
            run_id=run_id,
        )
        if self.runtime_services is not None:
            self.runtime_services.register_tasks(tasks)
        flow_event(
            "WORKER_DAG_REGISTERED",
            {
                "task_count": len(tasks),
                "runtime_persistence": self.runtime_services is not None,
                "tasks": [task.safe_for_coordinator() for task in tasks],
            },
            run_id=run_id,
        )
        flow_event(
            "WORKER_EXECUTION_STARTED",
            {
                "task_count": len(tasks),
                "parallel_execution_enabled": True,
            },
            run_id=run_id,
        )
        results, batches, timeline = self._run_dag(
            tasks,
            query=query,
            output_dir=self.output_dir,
            db_path=self.db_path,
            default_top_k=default_top_k,
            language=language,
            execution_context=context,
        )
        flow_event(
            "WORKER_INITIAL_EXECUTION_COMPLETED",
            {
                "result_count": len(results),
                "execution_batches": batches,
                "timeline": timeline,
            },
            run_id=run_id,
        )

        # Execution-time forward replanning. Successful non-report WorkerResults
        # are frozen and reused. Only partial/failed/insufficient tasks are
        # superseded, and every patch remains inside the original GoalContract
        # and side-effect boundary.
        active_tasks = list(tasks)
        replan_audit: list[dict[str, Any]] = []
        recovery_error_history: list[dict[str, Any]] = []
        invalid_replan_block_count = 0
        max_replan_rounds = 2
        for replan_round in range(1, max_replan_rounds + 1):
            observations = self._build_task_observations(active_tasks, results)
            flow_event(
                "WORKER_RESULT_OBSERVATION_COMPLETED",
                {
                    "replan_round": replan_round - 1,
                    "observations": observations,
                },
                run_id=run_id,
                level=(
                    "WARNING"
                    if any(not item.get("semantic_satisfied") for item in observations)
                    else "INFO"
                ),
            )
            blocking_context = _has_forward_replan_context_blocker(observations)
            replan_candidates = [
                item
                for item in observations
                if item.get("replan_recommended")
                and item.get("failure_kind") not in {
                    "worker_output_contract_failure",
                    "worker_output_contract_violation",
                    "completion_report_invalid",
                    "completion_report_missing",
                }
            ]
            if blocking_context or not replan_candidates:
                break
            before_unsatisfied = sum(
                1 for item in observations if not item.get("semantic_satisfied")
            )
            try:
                full_tasks, new_tasks, replan_meta = self.planner.replan_forward(
                    query=query,
                    effect_limit=read_goal,
                    session_id=session_id,
                    run_id=run_id,
                    user_id=user_id,
                    focus_refs=focus_refs,
                    context_refs=context_refs,
                    memory_summary=planning_memory_summary,
                    language=language,
                    as_of_time=explicit_as_of,
                    current_tasks=active_tasks,
                    current_results=results,
                    observations=observations,
                    replan_round=replan_round,
                    error_history=recovery_error_history,
                )
            except Exception as exc:
                invalid_replan_block_count += 1
                audit = {
                    "round": replan_round,
                    "status": "blocked",
                    "reason": str(exc),
                    "exception_type": type(exc).__name__,
                    "observations": replan_candidates,
                }
                replan_audit.append(audit)
                flow_event(
                    "WORKER_FORWARD_REPLAN_BLOCKED",
                    audit,
                    run_id=run_id,
                    level="ERROR",
                )
                break
            recovery_error_history = [
                dict(item)
                for item in replan_meta.get("recovery_error_history") or recovery_error_history
                if isinstance(item, dict)
            ]
            if not new_tasks:
                replan_audit.append(
                    {
                        "round": replan_round,
                        "status": "no_patch",
                        "reason": "forward_replan_returned_no_new_tasks",
                        "meta": replan_meta,
                    }
                )
                break
            _bind_authoritative_task_context(full_tasks, entity_catalog=entity_catalog)
            _bind_authoritative_task_context(new_tasks, entity_catalog=entity_catalog)
            _bind_task_completion_contracts(full_tasks, directory=self.directory)
            _bind_task_completion_contracts(new_tasks, directory=self.directory)
            if self.runtime_services is not None:
                self.runtime_services.register_tasks(new_tasks)
            combined_results, new_batches, new_timeline = self._run_dag(
                new_tasks,
                query=query,
                output_dir=self.output_dir,
                db_path=self.db_path,
                default_top_k=default_top_k,
                language=language,
                execution_context=context,
                existing_results=results,
            )
            batch_offset = len(batches)
            for batch in new_batches:
                batch["batch_index"] = int(batch.get("batch_index") or 0) + batch_offset
                batch["replan_round"] = replan_round
            for row in new_timeline:
                row["replan_round"] = replan_round
            active_ids = {task.task_id for task in full_tasks}
            superseded_ids = sorted(set(results) - active_ids)
            results = {
                task_id: result
                for task_id, result in combined_results.items()
                if task_id in active_ids
            }
            active_tasks = list(full_tasks)
            batches.extend(new_batches)
            timeline.extend(new_timeline)
            after_observations = self._build_task_observations(active_tasks, results)
            after_unsatisfied = sum(
                1 for item in after_observations if not item.get("semantic_satisfied")
            )
            execution_progress = after_unsatisfied < before_unsatisfied

            # Structural progress describes whether the PlanPatch changed the
            # repair graph in a potentially useful way; execution progress says
            # whether that change actually satisfied more contracts after run.
            # Keep the two concepts separate so an executable repair is not
            # mislabeled merely because its newly added Worker later fails.
            prior_missing_data = {
                str(name)
                for item in replan_candidates
                for name in item.get("missing_data_names") or []
                if str(name)
            }
            new_produced_data = {
                str(name)
                for task in new_tasks
                for name in task.expected_data_names
                if str(name)
            }
            structural_progress = bool(
                new_tasks
                and (prior_missing_data.intersection(new_produced_data) or superseded_ids)
            )
            audit = {
                "round": replan_round,
                "status": "executed",
                "reused_task_ids": replan_meta.get("reused_task_ids") or [],
                "new_task_ids": replan_meta.get("new_task_ids") or [],
                "superseded_task_ids": superseded_ids,
                "before_unsatisfied": before_unsatisfied,
                "after_unsatisfied": after_unsatisfied,
                "structural_progress": structural_progress,
                "execution_progress": execution_progress,
                "progress": execution_progress,
                "meta": replan_meta,
            }
            replan_audit.append(audit)
            flow_event(
                "WORKER_FORWARD_REPLAN_EXECUTED",
                audit,
                run_id=run_id,
                level="INFO" if execution_progress else "WARNING",
            )
            # Do not stop solely because the first Recovery round made no
            # execution progress. The next LLM Recovery round receives the new
            # error result plus prior error history and may choose a different
            # Worker route. max_replan_rounds=2 is the hard bound.

        tasks = active_tasks
        final_observations = self._build_task_observations(tasks, results)
        need_completion = evaluate_need_completion(
            dict(plan_meta.get("request_need_contract") or {}),
            final_observations,
        )
        flow_event(
            "WORKER_EXECUTION_COMPLETED",
            {
                "result_count": len(results),
                "execution_batches": batches,
                "timeline": timeline,
                "task_observations": final_observations,
                "need_completion": need_completion,
                "replan_count": len([item for item in replan_audit if item.get("status") == "executed"]),
                "replan_audit": replan_audit,
                "recovery_error_history_count": len(recovery_error_history),
            },
            run_id=run_id,
        )
        for result in results.values():
            for update in result.memory_updates:
                memory_put = {
                    "session_id": session_id, "key": update.key, "value": update.value,
                    "value_type": update.value_type, "summary": update.summary,
                    "source_type": update.source_type,
                    "source_ref": update.source_ref or result.task_id,
                    "confirmed": update.confirmed, "confidence": update.confidence,
                }
                if defer_session_mutations:
                    session_mutation_proposal.add_put(**memory_put)
                else:
                    self.session_state.put(**memory_put)

        worker_results = {task_id: result.handoff_view() for task_id, result in results.items()}
        report = next(
            (
                results.get(task.task_id)
                for task in reversed(tasks)
                if task.assigned_agent == REPORT_WRITER and task.task_id in results
            ),
            None,
        )
        report_content = ""
        if report is not None and isinstance(report.data, dict):
            report_content = str(report.data.get("content") or "").strip()
        goal_contract = dict(plan_meta.get("goal_contract") or {})
        goal_data_names = {
            str(item) for item in [
                *(goal_contract.get("desired_outputs") or []),
                *(goal_contract.get("required_context_names") or []),
            ] if str(item)
        }
        # Every Business Request stops at structured business results. W06 runs
        # exactly once at Bundle level after Request Coverage.
        terminal_report_required = False
        terminal_report_missing = terminal_report_required and not bool(report_content)
        if report_content:
            answer = report_content
        elif terminal_report_required:
            answer = (
                "最终自然语言报告未生成，系统不会用Worker状态摘要冒充业务回答。"
                if language != "en" else
                "The final user-facing report was not generated; Worker status summaries are not used as the business answer."
            )
        else:
            answer = ""
        statuses = [result.status for result in results.values()]
        need_context = [item for result in results.values() for item in result.missing_items if item.blocking]
        final_sufficiency = self.sufficiency_gate.evaluate(
            missing_items=need_context,
            available_parameters=hydrated.available_parameters,
        ) if need_context else None
        waiting_user_input = bool(final_sufficiency and final_sufficiency.next_action in {"ask_user", "select_entity"})
        waiting_internal_context = bool(need_context and not waiting_user_input)
        status_failed = sum(status in {ResultStatus.FAILED, ResultStatus.BLOCKED, ResultStatus.NOT_EXECUTED} for status in statuses)
        semantic_failed = sum(
            1
            for item in final_observations
            if not item.get("semantic_satisfied")
            and item.get("failure_kind") != "context_missing"
        ) + (1 if terminal_report_missing else 0)
        if terminal_report_missing:
            final_observations.append({
                "task_id": "FINAL",
                "worker_id": "",
                "boundary_id": "result.composition",
                "status": "failed",
                "contract_valid": False,
                "completion_report_valid": False,
                "semantic_satisfied": False,
                "produced_data_names": [],
                "missing_data_names": ["user_facing_report"],
                "failure_kind": "terminal_user_facing_report_missing",
                "retryable": False,
                "repairable": True,
                "replan_recommended": True,
                "should_freeze": False,
                "reusable": False,
                "freeze_reason": "user_facing_report_required",
                "error": {
                    "code": "terminal_user_facing_report_missing",
                    "message": "Goal requires user_facing_report but no validated report content was produced.",
                    "component": "REPORT_WRITER",
                    "retryable": False,
                },
                "worker_escalation": None,
                "completion": {},
                "replan_triggers": ["required_contract_not_satisfied"],
            })
        failed = max(status_failed, semantic_failed)
        completed = sum(status in {ResultStatus.COMPLETED, ResultStatus.PARTIAL, ResultStatus.PROPOSAL_READY} for status in statuses)
        # Keep the external execution-status contract stable: both user-input
        # waits and internal-context waits remain waiting_context here. The
        # persisted RunCheckpoint distinguishes waiting_user_input from
        # waiting_context for resume/routing semantics.
        execution_status = (
            "waiting_context" if need_context else
            "completed" if failed == 0 else
            "partially_completed" if completed else
            "failed"
        )
        success = completed > 0 and failed == 0 and not need_context
        question = _clarification_question(need_context, language) if waiting_user_input else ""
        context_wait_message = (
            "系统仍缺少内部上下文或上游能力结果，本轮不会把它误判成需要用户提供的参数。"
            if waiting_internal_context and language != "en" else
            "The system is still missing internal context or an upstream capability result; it is not being treated as a user-supplied parameter."
            if waiting_internal_context else ""
        )
        internal_count = len([item for item in timeline if item.get("status") not in {"not_executed"}])
        if persist_checkpoint:
            self.checkpoints.save(RunCheckpoint(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                status="waiting_user_input" if waiting_user_input else "waiting_context" if waiting_internal_context else "completed" if success else "failed",
                current_node_id=f"request:{request_id}:final" if request_id else "final_response",
                capability_plan=dict(plan_meta.get("capability_plan") or {}),
                task_states={task.task_id: (results[task.task_id].status.value if task.task_id in results else "not_executed") for task in tasks},
                resolved_entity_refs=[ref.to_dict() for ref in focus_refs],
                data_refs=[],
                working_memory_snapshot=(
                    self.specialist.context_bundle.snapshot_working_memory()
                    if self.specialist.context_bundle is not None else {}
                ),
                missing_parameters=list(final_sufficiency.missing_parameters) if final_sufficiency else [],
                missing_context=(
                    [*final_sufficiency.missing_context, *final_sufficiency.unresolved_entities]
                    if final_sufficiency else []
                ),
                replan_count=len([item for item in replan_audit if item.get("status") == "executed"]),
            ))
        return {
            "request_id": str(request_id or ""),
            "session_mutation_proposal": session_mutation_proposal.to_dict(),
            "success": success,
            "answer": question if question else context_wait_message if context_wait_message else answer,
            "worker_results": worker_results,
            "business_outputs": (
                self.specialist.context_bundle.business_data_context(
                    entity_refs=[ref.to_dict() for ref in focus_refs]
                )
                if self.specialist.context_bundle is not None else {}
            ),
            "tool_calls": [],
            "internal_tool_call_count": internal_count,
            "execution_order": [item.task_id for item in tasks if item.task_id in results],
            "execution_batches": batches,
            "warnings": [warning for result in results.values() for warning in result.warnings],
            "errors": [],
            "execution_status": execution_status,
            "need_clarification": bool(waiting_user_input),
            "clarification_question": question,
            "context_sufficiency": final_sufficiency.to_dict() if final_sufficiency else {},
            "missing_context": [item.to_dict() for item in need_context],
            "observations": final_observations,
            "need_completion": need_completion,
            "replan_audit": replan_audit,
            "replan_count": len([item for item in replan_audit if item.get("status") == "executed"]),
            "invalid_replan_block_count": invalid_replan_block_count,
            "replan_limits": {"max_rounds": max_replan_rounds, "delegation_preserved": True},
            "recovery_error_history_count": len(recovery_error_history),
            "agent_timeline": timeline,
            "handoff": {
                "handoff_available": bool(worker_results),
                "handoff_count": len(worker_results),
                "handoff_refs": [f"worker_result:{task_id}" for task_id in worker_results],
                "safety": {"worker_private_tools": True, "coordinator_tool_visibility": "none"},
            },
            "graph_runtime": {
                "contract_version": "capability_contract_runtime.v1",
                "graph_id": self.store.graph_id,
                "task_contract": "capability_execution_task.v1",
                "result_contract": "graph_worker_result.v1",
                "focus_refs": [ref.to_dict() for ref in focus_refs],
                "resolution_audit": resolution_audit,
                "planner": plan_meta,
                "worker_dag": {
                    "contract_version": "worker_dag_snapshot.v1",
                    "task_count": len(tasks),
                    "tasks": [task.safe_for_coordinator() for task in tasks],
                    "execution_batches": batches,
                    "execution_order": [
                        item.task_id for item in tasks if item.task_id in results
                    ],
                },
                "runtime_persistence": {
                    "agent_steps_connected": self.runtime_services is not None,
                    "runtime_layer": "capability_dag+worker_tool_dag",
                },
            },
        }

    @staticmethod
    def _task_observation(
        task: GraphAgentTask,
        result: GraphWorkerResult | None,
    ) -> dict[str, Any]:
        expected_data = set(task.expected_data_names)
        if result is None:
            return {
                "task_id": task.task_id,
                "worker_id": task.worker_id,
                "boundary_id": task.boundary_id,
                "status": ResultStatus.NOT_EXECUTED.value,
                "contract_valid": False,
                "semantic_satisfied": False,
                "produced_data_names": [],
                "missing_data_names": sorted(expected_data),
                "failure_kind": "not_executed",
                "retryable": False,
                "repairable": True,
                "replan_recommended": True,
                "should_freeze": False,
                "reusable": False,
                "freeze_reason": "task_not_executed",
                "completion": {},
            }
        completion = dict(result.completion or {})
        completion_valid = False
        completion_error = ""
        if completion:
            try:
                validate_completion_report(completion, task)
                completion_valid = True
            except Exception as exc:
                completion_error = str(exc)
        produced = {
            str(item) for item in completion.get("produced_data_names") or [] if str(item)
        }
        missing = {
            str(item) for item in completion.get("missing_data_names") or [] if str(item)
        } or (expected_data - produced)
        if completion_valid:
            decision = flow_decision(
                result.status,
                completion,
                output_type=result.output_type,
                retryable=bool((result.error or {}).get("retryable")),
            )
            semantic_satisfied = bool(decision.semantic_satisfied)
            failure_kind = decision.failure_kind
            repairable = bool(decision.replan_recommended)
            should_freeze = bool(decision.should_freeze)
            reusable = bool(decision.reusable)
            freeze_reason = decision.freeze_reason
        else:
            semantic_satisfied = False
            failure_kind = "completion_report_invalid" if completion_error else "completion_report_missing"
            repairable = result.status != ResultStatus.NEED_CONTEXT
            should_freeze = False
            reusable = False
            freeze_reason = "capability_contract_completion_required"
        escalation = dict((result.metadata or {}).get("worker_escalation") or {})
        error = escalation or dict(result.error or {})
        if escalation:
            failure_kind = str(escalation.get("error_id") or failure_kind)
            repairable = bool(
                (result.metadata or {}).get("worker_escalation_retryable", repairable)
            )
        if completion_error and not error:
            error = {
                "code": "capability_completion_report_invalid",
                "message": completion_error,
                "component": result.agent_id,
                "retryable": False,
            }
        return {
            "task_id": task.task_id,
            "worker_id": task.worker_id,
            "boundary_id": task.boundary_id,
            "status": result.status.value,
            "contract_valid": completion_valid,
            "completion_report_valid": completion_valid,
            "semantic_satisfied": semantic_satisfied,
            "produced_data_names": sorted(produced),
            "missing_data_names": sorted(missing),
            "missing_context": sorted({
                str(item.key) for item in list(result.missing_items or [])
                if getattr(item, "blocking", True) and str(getattr(item, "key", "") or "")
            }),
            "failure_kind": failure_kind,
            "retryable": bool((result.error or {}).get("retryable")),
            "repairable": repairable,
            "replan_recommended": bool(not semantic_satisfied and repairable),
            "should_freeze": should_freeze,
            "reusable": reusable,
            "freeze_reason": freeze_reason,
            "error": error or None,
            "worker_escalation": escalation or None,
            "completion": completion,
            "replan_triggers": ([] if semantic_satisfied else ["required_contract_not_satisfied"]),
        }

    @classmethod
    def _build_task_observations(
        cls,
        tasks: list[GraphAgentTask],
        results: dict[str, GraphWorkerResult],
    ) -> list[dict[str, Any]]:
        return [
            cls._task_observation(task, results.get(task.task_id))
            for task in tasks
        ]

    @staticmethod
    def _worker_result_usable(result: GraphWorkerResult | None) -> bool:
        """Return whether a dependency may unlock its downstream Worker."""

        if result is None:
            return False
        completion = dict(result.completion or {})
        return bool(
            completion
            and completion.get("expected_task_completed")
            and completion.get("completion_status") == "completed"
            and result.status in {ResultStatus.COMPLETED, ResultStatus.PROPOSAL_READY}
        )

    def _task_input_data_gate(self, task: GraphAgentTask) -> dict[str, Any]:
        """Evaluate data necessity after all order dependencies reach a terminal result."""
        requirements = [
            dict(item)
            for item in task.metadata.get("input_requirements") or []
            if isinstance(item, dict) and item.get("kind") == "data" and str(item.get("data_name") or "")
        ]
        bundle = self.specialist.context_bundle
        focus_entity_ids = [str(ref.node_id or "") for ref in task.focus_refs if str(ref.node_id or "")]

        def available(name: str) -> bool:
            if bundle is None:
                return False
            if bundle.has_business_data(entity_id="__run__", name=name):
                return True
            if not focus_entity_ids:
                return False
            return all(bundle.has_business_data(entity_id=entity_id, name=name) for entity_id in focus_entity_ids)

        missing = {"required": [], "preferred": [], "optional": []}
        available_names: list[str] = []
        for requirement in requirements:
            name = str(requirement.get("data_name") or "")
            necessity = str(requirement.get("necessity") or "required")
            if available(name):
                available_names.append(name)
            else:
                missing.setdefault(necessity, []).append(name)
        return {
            "task_id": task.task_id,
            "worker_id": task.worker_id,
            "dependency_semantics": "completion_order_only",
            "dependency_states": {},
            "requirements": requirements,
            "available_input_data_names": sorted(set(available_names)),
            "missing_required_data_names": sorted(set(missing.get("required") or [])),
            "missing_preferred_data_names": sorted(set(missing.get("preferred") or [])),
            "missing_optional_data_names": sorted(set(missing.get("optional") or [])),
        }

    @staticmethod
    def _worker_results_context_for_consumer(
        task: GraphAgentTask,
        results: dict[str, GraphWorkerResult],
    ) -> dict[str, Any]:
        """Build W09's only business-data input from already completed Worker results.

        This reuses the Coordinator's existing ``results`` dictionary. No new Store
        is introduced. The projection preserves concrete Worker business payloads
        (unlike audit/handoff summaries) while filtering execution-private keys.
        """
        items: dict[str, Any] = {}
        for task_id, result in results.items():
            if str(task_id) == str(task.task_id):
                continue
            items[str(task_id)] = execution_safe_value({
                "task_id": result.task_id,
                "agent_id": result.agent_id,
                "status": result.status.value,
                "output_type": result.output_type,
                "data": result.data,
                "summary": result.summary,
                "completion": result.completion,
                "error": result.error,
                "warnings": result.warnings,
                "focus_refs": [ref.to_dict() for ref in result.focus_refs],
            })
        return {
            "schema_version": "other_worker_results.v1",
            "current_task_id": task.task_id,
            "result_count": len(items),
            "items": items,
        }

    def _run_dag(
        self,
        tasks: list[GraphAgentTask],
        *,
        query: str,
        output_dir: str | Path,
        db_path: str | Path | None,
        default_top_k: int,
        language: str,
        execution_context: dict[str, Any],
        existing_results: dict[str, GraphWorkerResult] | None = None,
    ) -> tuple[dict[str, GraphWorkerResult], list[dict[str, Any]], list[dict[str, Any]]]:
        results: dict[str, GraphWorkerResult] = dict(existing_results or {})
        pending = {task.task_id: task for task in tasks}
        dag_wait_started = time.perf_counter()
        batches: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        batch_index = 0
        while pending:
            # dependency_task_ids are completion-order edges only. A downstream
            # Worker waits for providers to finish, but provider failure alone does
            # not block it. Required/preferred/optional data necessity is checked
            # separately against RunContextStore below.
            runtime_debug_event(
                "WORKER_DAG_PENDING_SNAPSHOT",
                {
                    "pending_task_ids": sorted(pending),
                    "completed_result_ids": sorted(results),
                    "dependency_states": {
                        task_id: {
                            dep: (results[dep].status.value if dep in results else "pending")
                            for dep in task.dependency_task_ids
                        }
                        for task_id, task in pending.items()
                    },
                },
                run_id=str(tasks[0].run_id if tasks else ""),
            )
            if not pending:
                break

            ready = [
                task
                for task in pending.values()
                if all(dependency_id in results for dependency_id in task.dependency_task_ids)
            ]

            # Runtime may enforce objective engineering gates for ordinary Workers,
            # but W09 business-data sufficiency is owned by W09 itself. W09 receives
            # the already completed sibling Worker results and decides whether those
            # results are enough for the requested analysis.
            gated_ready: list[GraphAgentTask] = []
            for task in list(ready):
                if task.assigned_agent == ENTITY_ANALYST:
                    gate = {
                        "task_id": task.task_id,
                        "worker_id": task.worker_id,
                        "dependency_semantics": "completion_order_only",
                        "dependency_states": {
                            dependency_id: results[dependency_id].status.value
                            for dependency_id in task.dependency_task_ids
                            if dependency_id in results
                        },
                        "decision": "worker_judges_sufficiency",
                        "business_sufficiency_owner": "W09",
                    }
                    task.metadata["input_gate"] = gate
                    task.metadata["degraded_execution"] = False
                    task.metadata["missing_preferred_data_names"] = []
                    task.metadata["missing_optional_data_names"] = []
                    runtime_debug_event(
                        "WORKER_EXECUTION_GATE_DECISION", gate,
                        run_id=task.run_id, task_id=task.task_id, level="DEBUG",
                    )
                    gated_ready.append(task)
                    continue
                gate = self._task_input_data_gate(task)
                gate["dependency_states"] = {
                    dependency_id: results[dependency_id].status.value
                    for dependency_id in task.dependency_task_ids
                    if dependency_id in results
                }
                missing_required = list(gate.get("missing_required_data_names") or [])
                missing_preferred = list(gate.get("missing_preferred_data_names") or [])
                missing_optional = list(gate.get("missing_optional_data_names") or [])

                # Even when the MainAgent did not explicitly list a provider output as
                # an input Requirement, a deliberately scheduled provider that terminates
                # without its promised data is still useful limitation information for
                # downstream analytical Workers. Treat these undeclared provider gaps as
                # preferred (never hard-blocking) inputs. Explicit required Requirements
                # above still win and remain blocking.
                dependency_missing_outputs: set[str] = set()
                dependency_details: dict[str, dict[str, Any]] = {}
                declared_input_names = {
                    str(item.get("data_name") or "")
                    for item in gate.get("requirements") or []
                    if str(item.get("data_name") or "")
                }
                for dependency_id in task.dependency_task_ids:
                    dependency_result = results.get(dependency_id)
                    if dependency_result is None:
                        continue
                    completion = dict(dependency_result.completion or {})
                    produced_names = {
                        str(name) for name in completion.get("produced_data_names") or [] if str(name)
                    }
                    missing_names = {
                        str(name) for name in completion.get("missing_data_names") or [] if str(name)
                    }
                    if not missing_names and not bool(completion.get("expected_task_completed")):
                        # Completion report may be minimal on an execution failure.
                        missing_names.update(
                            str(name) for name in (dependency_result.error or {}).get("missing_data_names") or [] if str(name)
                        )
                    dependency_missing_outputs.update(missing_names)
                    dependency_details[dependency_id] = {
                        "status": dependency_result.status.value,
                        "produced_data_names": sorted(produced_names),
                        "missing_data_names": sorted(missing_names),
                        "failure_kind": str(completion.get("failure_kind") or ""),
                    }
                implicit_preferred = sorted(
                    dependency_missing_outputs
                    - set(missing_required)
                    - declared_input_names
                )
                missing_preferred = sorted(set(missing_preferred).union(implicit_preferred))
                gate["dependency_output_observations"] = dependency_details
                gate["implicit_provider_gaps_as_preferred"] = implicit_preferred
                gate["missing_preferred_data_names"] = missing_preferred
                decision = "block" if missing_required else "degraded_run" if (missing_preferred or missing_optional) else "run"
                gate["decision"] = decision
                runtime_debug_event(
                    "WORKER_EXECUTION_GATE_DECISION",
                    gate,
                    run_id=task.run_id,
                    task_id=task.task_id,
                    level="WARNING" if decision != "run" else "DEBUG",
                )
                if missing_required:
                    task.metadata["input_gate"] = gate
                    task.metadata.setdefault(
                        "dependency_wait_ms",
                        round((time.perf_counter() - dag_wait_started) * 1000.0, 3),
                    )
                    result = GraphWorkerResult(
                        task_id=task.task_id,
                        agent_id=task.assigned_agent,
                        status=ResultStatus.BLOCKED,
                        output_type=task.expected_output_type,
                        data=None,
                        error={
                            "code": "required_business_data_missing",
                            "message": "Required business data is unavailable after upstream completion.",
                            "component": "worker_execution_gate",
                            "retryable": True,
                            "missing_data_names": missing_required,
                        },
                        focus_refs=task.focus_refs,
                        summary="必需业务数据缺失，当前 Worker 未执行并交由 MainAgent 决定是否重规划。",
                        warnings=["required_business_data_missing"],
                        missing_items=[
                            MissingContextItem(
                                key=name,
                                description=f"缺少必需业务数据：{name}",
                                expected_format="RunContextStore BusinessData",
                                reason="required_business_data_missing_after_provider_completion",
                                searched_sources=["RunContextStore"],
                                blocking=True,
                            )
                            for name in missing_required
                        ],
                        completion=non_success_completion_report(
                            task,
                            execution_status="blocked",
                            reason="required business data missing after dependency completion",
                            failure_kind="required_business_data_missing",
                        ),
                        metadata={
                            "input_gate": gate,
                            "replan_recommended": True,
                            "degraded_execution": False,
                        },
                    )
                    results[task.task_id] = result
                    if self.runtime_services is not None:
                        self.runtime_services.record_result(task, result)
                    timeline.append({
                        "task_id": task.task_id,
                        "worker_id": task.worker_id,
                        "agent_id": task.assigned_agent,
                        "boundary_id": task.boundary_id,
                        "status": result.status.value,
                        "output_type": result.output_type,
                        "duration_ms": 0.0,
                        "dependency_wait_ms": task.metadata.get("dependency_wait_ms", 0.0),
                        "summary": result.summary[:500],
                        "warning_count": len(result.warnings),
                        "evidence_count": 0,
                        "artifact_count": 0,
                        "error": result.error,
                        "input_gate": gate,
                    })
                    pending.pop(task.task_id, None)
                    continue
                task.metadata["input_gate"] = gate
                task.metadata["degraded_execution"] = decision == "degraded_run"
                task.metadata["missing_preferred_data_names"] = missing_preferred
                task.metadata["missing_optional_data_names"] = missing_optional
                gated_ready.append(task)
            ready = gated_ready
            # W09 consumes already completed sibling Worker results. When provider
            # Workers and a consumer become ready in the same layer, execute providers
            # first so W09 can inspect their real GraphWorkerResult payloads.
            if ready:
                provider_ready = [
                    task for task in ready
                    if str(getattr(self.directory.get(task.worker_id or task.assigned_agent), "working_memory_mode", "none")) == "provider"
                ]
                if provider_ready:
                    deferred_consumers = [
                        task.task_id for task in ready
                        if str(getattr(self.directory.get(task.worker_id or task.assigned_agent), "working_memory_mode", "none")) == "consumer"
                    ]
                    if deferred_consumers:
                        flow_event(
                            "WORKING_MEMORY_CONSUMER_DEFERRED",
                            {
                                "provider_task_ids": [task.task_id for task in provider_ready],
                                "consumer_task_ids": deferred_consumers,
                                "reason": "make_provider_worker_results_available_before_entity_analysis",
                            },
                            run_id=str(tasks[0].run_id if tasks else ""),
                        )
                        ready = [
                            task for task in ready
                            if str(getattr(self.directory.get(task.worker_id or task.assigned_agent), "working_memory_mode", "none")) != "consumer"
                        ]
            if not ready and pending and any(
                all(dependency_id in results for dependency_id in task.dependency_task_ids)
                for task in pending.values()
            ):
                # A gate may have just created a terminal BLOCKED result; allow the
                # next loop to evaluate downstream tasks against actual data necessity.
                continue
            if not ready:
                for task in list(pending.values()):
                    task.metadata.setdefault(
                        "dependency_wait_ms",
                        round((time.perf_counter() - dag_wait_started) * 1000.0, 3),
                    )
                    result = GraphWorkerResult(
                        task_id=task.task_id,
                        agent_id=task.assigned_agent,
                        status=ResultStatus.NOT_EXECUTED,
                        output_type=task.expected_output_type,
                        data=None,
                        error={
                            "code": "unresolved_task_dependency",
                            "message": "任务依赖无法满足。",
                            "component": "worker_dag_executor",
                            "retryable": False,
                        },
                        focus_refs=task.focus_refs,
                        summary="任务依赖无法满足。",
                        warnings=["unresolved_task_dependency"],
                    )
                    results[task.task_id] = result
                    pending.pop(task.task_id, None)
                    if self.runtime_services is not None:
                        self.runtime_services.record_result(task, result)
                break
            ready_at = time.perf_counter()
            for task in ready:
                task.metadata.setdefault(
                    "dependency_wait_ms",
                    round((ready_at - dag_wait_started) * 1000.0, 3),
                )
            batch_index += 1
            batches.append({
                "batch_index": batch_index,
                "task_ids": [task.task_id for task in ready],
                "agents": [task.assigned_agent for task in ready],
                "parallel": len(ready) > 1,
            })
            max_workers = min(4, self.resource_budget.max_parallel_workers, len(ready))
            if self.runtime_services is not None:
                for task in ready:
                    self.runtime_services.mark_ready(task)
                    self.runtime_services.mark_running(task)
            results_snapshot = dict(results)

            def run_specialist(task: GraphAgentTask) -> GraphWorkerResult:
                task_execution_context = dict(execution_context or {})
                if task.assigned_agent == ENTITY_ANALYST:
                    worker_results_context = self._worker_results_context_for_consumer(task, results_snapshot)
                    task_execution_context["worker_results_context"] = worker_results_context
                    runtime_debug_event(
                        "W09_WORKER_RESULTS_INPUT_READY",
                        {
                            "task_id": task.task_id,
                            "worker_id": task.worker_id,
                            "result_count": worker_results_context.get("result_count", 0),
                            "source_task_ids": sorted((worker_results_context.get("items") or {}).keys()),
                        },
                        run_id=task.run_id, task_id=task.task_id,
                    )
                with self.resource_budget.worker_slot():
                    return self.specialist.run(
                        task, current_user_request=query, output_dir=output_dir,
                        db_path=db_path, default_top_k=default_top_k, language=language,
                        execution_context=task_execution_context,
                    )

            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(run_specialist, task): task for task in ready}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = GraphWorkerResult(
                            task_id=task.task_id,
                            agent_id=task.assigned_agent,
                            status=ResultStatus.FAILED,
                            output_type=task.expected_output_type,
                            data=None,
                            error={
                                "code": "worker_execution_failed",
                                "message": str(exc),
                                "component": task.assigned_agent,
                                "retryable": True,
                            },
                            focus_refs=task.focus_refs,
                            summary="Worker 执行失败。",
                            warnings=[f"{type(exc).__name__}:{exc}"],
                        )
                    results[task.task_id] = result
                    if self.runtime_services is not None:
                        self.runtime_services.record_result(task, result)
                    timeline.append({
                        "task_id": task.task_id,
                        "worker_id": task.worker_id,
                        "agent_id": task.assigned_agent,
                        "boundary_id": task.boundary_id,
                        "status": result.status.value,
                        "output_type": result.output_type,
                        "duration_ms": result.metadata.get("duration_ms"),
                        "dependency_wait_ms": result.metadata.get(
                            "dependency_wait_ms", task.metadata.get("dependency_wait_ms", 0.0)
                        ),
                        "llm_execution_timing": result.metadata.get("llm_execution_timing", {}),
                        "tool_execution_timing": result.metadata.get("tool_execution_timing", {}),
                        "unattributed_worker_execution_ms": result.metadata.get(
                            "unattributed_worker_execution_ms", 0.0
                        ),
                        "summary": result.summary[:500],
                        "warning_count": len(result.warnings),
                        "evidence_count": len(result.evidence_refs),
                        "artifact_count": len(result.artifact_refs),
                        "error": result.error,
                    })
                    pending.pop(task.task_id, None)
        return results, batches, timeline

    @staticmethod
    def _empty_result(*, answer: str, success: bool, status: str, warnings: list[str] | None = None) -> dict[str, Any]:
        return {
            "success": success,
            "answer": answer,
            "worker_results": {},
            "tool_calls": [],
            "internal_tool_call_count": 0,
            "execution_order": [],
            "execution_batches": [],
            "warnings": list(warnings or []),
            "errors": [],
            "execution_status": status,
            "need_clarification": False,
            "clarification_question": "",
            "missing_context": [],
            "observations": [],
            "replan_audit": [],
            "replan_count": 0,
            "invalid_replan_block_count": 0,
            "replan_limits": {"max_rounds": 0},
            "agent_timeline": [],
            "handoff": {"handoff_available": False, "handoff_count": 0, "handoff_refs": []},
        }
