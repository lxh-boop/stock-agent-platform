from __future__ import annotations

from agent.collaboration.coordinator import (
    _has_forward_replan_context_blocker,
    _planning_memory_summary,
)
from agent.collaboration.worker_directory import CapabilityWorkerDirectory


def test_portfolio_request_without_focus_inheritance_hides_session_entity_from_business_planner() -> None:
    summary = _planning_memory_summary(
        session_summary="上一轮分析了贵州茅台，并形成了实体分析。",
        long_term_memory_summary="",
        inherit_previous_focus=False,
    )
    assert summary == ""


def test_conversation_focus_inheritance_keeps_session_summary_for_business_planner() -> None:
    summary = _planning_memory_summary(
        session_summary="上一轮分析了贵州茅台，并形成了实体分析。",
        long_term_memory_summary="",
        inherit_previous_focus=True,
    )
    assert "贵州茅台" in summary


def test_long_term_memory_remains_available_without_previous_focus_inheritance() -> None:
    summary = _planning_memory_summary(
        session_summary="上一轮分析了贵州茅台。",
        long_term_memory_summary="用户风险偏好为稳健型。",
        inherit_previous_focus=False,
    )
    assert "贵州茅台" not in summary
    assert "稳健型" in summary


def test_worker_context_unresolved_blocks_business_worker_forward_replan() -> None:
    observations = [
        {"task_id": "T01", "failure_kind": "none"},
        {"task_id": "T02", "failure_kind": "worker_context_unresolved"},
        {"task_id": "T03", "failure_kind": "upstream_worker_failed"},
    ]
    assert _has_forward_replan_context_blocker(observations) is True


def test_regular_worker_failure_still_allows_forward_replan() -> None:
    observations = [
        {"task_id": "T01", "failure_kind": "none"},
        {"task_id": "T02", "failure_kind": "tool_execution_failure"},
    ]
    assert _has_forward_replan_context_blocker(observations) is False


def test_w02_ranking_resolution_is_only_for_discovery_not_named_entity_substitution() -> None:
    prompt = CapabilityWorkerDirectory().get("W02").private_worker_prompt
    assert "排名、筛选或发现候选证券" in prompt
    assert "绝不能用排名第一或其他候选证券替代目标" in prompt
    assert "internal.entity.resolve_ranked_security" in prompt


def test_coordinator_does_not_pass_previous_session_entity_to_planner_when_entry_rejects_focus_inheritance(tmp_path) -> None:
    import types
    import pytest

    from agent.collaboration.coordinator import AgentCollaborationCoordinator
    from agent.collaboration.context_binding import ContextBinding, EntityScope
    from agent.context.context_hydrator import HydratedContext

    class FakeSessionState:
        def build_summary(self, session_id, limit=40):
            del session_id, limit
            return "上一轮分析了贵州茅台。"

        def put(self, **kwargs):
            del kwargs

    class FakeHydrator:
        def hydrate(self, **kwargs):
            del kwargs
            return HydratedContext(
                user_id="u",
                session_id="s",
                session_summary="上一轮分析了贵州茅台。",
                previous_focus_refs=[],
                typed_focus_refs={},
                pending_run_ids=[],
                pending_proposal_ids=[],
                permission_context={},
                available_parameters={},
                long_term_memory_summary="",
                long_term_memory_refs=[],
                source_audit=[{"context_key": "session_summary", "source": "session_state"}],
            )

    class FakeCheckpoints:
        def save(self, checkpoint):
            del checkpoint

    class CapturePlanner:
        def __init__(self):
            self.memory_summary = None
            self.context_binding = None

        def plan(self, **kwargs):
            self.memory_summary = kwargs["memory_summary"]
            self.context_binding = kwargs.get("context_binding")
            raise RuntimeError("stop_after_memory_capture")

    coordinator = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    coordinator.output_dir = tmp_path
    coordinator.db_path = None
    coordinator.runtime_services = None
    coordinator.session_state = FakeSessionState()
    coordinator.context_hydrator = FakeHydrator()
    coordinator.checkpoints = FakeCheckpoints()
    coordinator.planner = CapturePlanner()
    coordinator.specialist = types.SimpleNamespace(context_bundle=types.SimpleNamespace(run_id="r"))
    coordinator._resolve_request_refs = types.MethodType(
        lambda self, **kwargs: ([], [], {"semantic_entities": [], "items": [], "context_binding": kwargs.get("context_binding") or {}}),
        coordinator,
    )

    with pytest.raises(RuntimeError, match="stop_after_memory_capture"):
        coordinator._execute_read_request(
            query="你觉得我的持仓应该怎么调整？",
            decomposition={},
            user_id="u",
            default_top_k=10,
            session_id="s",
            run_id="r",
            language="zh",
            execution_context={},
            proposal_required=True,
            context_binding=ContextBinding(
                entity_scope=EntityScope.PORTFOLIO,
                inherit_previous_focus=False,
                reason="完整组合任务不继承上一轮单一证券",
            ),
        )

    assert coordinator.planner.memory_summary == ""
    assert coordinator.planner.context_binding["entity_scope"] == "portfolio"
    assert coordinator.planner.context_binding["inherit_previous_focus"] is False




def test_conversation_semantic_focus_cannot_override_authoritative_typed_focus():
    from types import SimpleNamespace
    from agent.collaboration.coordinator import AgentCollaborationCoordinator
    from agent.graph.contracts import GraphNodeKind, GraphRef

    typed = GraphRef(
        graph_id="financial_graph",
        node_id="cn:security:szse:000858",
        node_kind=GraphNodeKind.OBJECT,
        role="focus",
        source="session_state",
        confidence=1.0,
        locked=True,
    )
    semantic = GraphRef(
        graph_id="financial_graph",
        node_id="cn:security:sse:600519",
        node_kind=GraphNodeKind.OBJECT,
        role="focus",
        source="neo4j",
        confidence=1.0,
        locked=True,
    )

    class Identity:
        def resolve_request(self, *args, **kwargs):
            del args, kwargs
            return SimpleNamespace(
                refs=[semantic],
                ambiguous_mentions=[],
                unresolved_mentions=[],
                to_dict=lambda: {"refs": [semantic.to_dict()]},
            )

    coordinator = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    coordinator.identity = Identity()
    refs, missing, audit = coordinator._resolve_request_refs(
        query="那它呢？",
        inherited_refs=[typed],
        typed_inherited_refs=[typed],
        context_refs=[],
        as_of_time="",
        language="zh",
        context_binding={
            "entity_scope": "conversation_focus",
            "inherit_previous_focus": True,
            "reference_entity_type": "security",
        },
        semantic_target={
            "entity_type": "security",
            "display_text": "贵州茅台",
            "source": "conversation_context",
            "status": "identified",
        },
        semantic_entities=[{
            "text": "贵州茅台",
            "entity_type": "security",
            "role": "focus",
            "source": "conversation_context",
        }],
    )
    assert refs == []
    assert [item.key for item in missing] == ["conversation_focus_semantic_conflict"]
    assert audit["items"][0]["authority_decision"] == "semantic_hint_conflicts_with_typed_focus"


def test_unresolved_conversation_semantic_hint_falls_back_to_authoritative_typed_focus():
    from types import SimpleNamespace
    from agent.collaboration.coordinator import AgentCollaborationCoordinator
    from agent.graph.contracts import GraphNodeKind, GraphRef

    typed = GraphRef(
        graph_id="financial_graph",
        node_id="cn:security:szse:000858",
        node_kind=GraphNodeKind.OBJECT,
        role="focus",
        source="session_state",
        confidence=1.0,
        locked=True,
    )

    class Identity:
        def resolve_request(self, *args, **kwargs):
            del args, kwargs
            return SimpleNamespace(
                refs=[],
                ambiguous_mentions=[],
                unresolved_mentions=["上一只股票"],
                to_dict=lambda: {"unresolved_mentions": ["上一只股票"]},
            )

    coordinator = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    coordinator.identity = Identity()
    refs, missing, audit = coordinator._resolve_request_refs(
        query="那它呢？",
        inherited_refs=[typed],
        typed_inherited_refs=[typed],
        context_refs=[],
        as_of_time="",
        language="zh",
        context_binding={
            "entity_scope": "conversation_focus",
            "inherit_previous_focus": True,
            "reference_entity_type": "security",
        },
        semantic_target={
            "entity_type": "security",
            "display_text": "上一只股票",
            "source": "conversation_context",
            "status": "identified",
        },
        semantic_entities=[{
            "text": "上一只股票",
            "entity_type": "security",
            "role": "focus",
            "source": "conversation_context",
        }],
    )
    assert [ref.node_id for ref in refs] == [typed.node_id]
    assert missing == []
    assert audit["items"][0]["authority_decision"] == "typed_focus_used_when_semantic_hint_unresolved"
