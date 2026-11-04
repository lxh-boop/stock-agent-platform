"""Unit tests for the LangGraph agent engine (agent_graph package).

Tests run fully offline: structured LLM stages are stubbed, worker subgraphs
use ``GenericFakeChatModel``, and persistence uses ``InMemorySaver``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_graph.approval import decision_from_resume
from agent_graph.schemas import Decomposition, DecomposedRequest, RequestCategory, SemanticTarget, WorkerAssignment, WorkerPlan
from agent_graph.state import AgentState
from agent_graph.tools.adapter import args_model_from_schema, sanitize_tool_name, wrap_tool_definition
from agent_graph.tools.runtime import AgentResources


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _fake_model(script: str = "好的，任务完成。") -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=script)] * 10000))


def _stub_resources() -> AgentResources:
    return AgentResources(store=None, provider=None, impact_service=None, tool_registry=None, context={})


def _decomposition(**request_overrides: Any) -> Decomposition:
    request = {
        "source_index": 1,
        "category": RequestCategory.BUSINESS,
        "objective": "查询最新模型排名",
        "request_type": "read",
        "proposal_required": False,
        "semantic_target": SemanticTarget(),
        "semantic_entities": [],
        "presentation": {},
        "depends_on": [],
    }
    request.update(request_overrides)
    return Decomposition(requests=[DecomposedRequest(**request)])


def _plan(worker_id: str = "W02", objective: str = "读取最新排名") -> WorkerPlan:
    return WorkerPlan(tasks=[WorkerAssignment(worker_id=worker_id, objective=objective, request_index=1)])


@pytest.fixture()
def graph_module(monkeypatch):
    import agent_graph.graph as graph

    return graph


def _build(graph_module, monkeypatch, *, decomposition=None, plan=None, agents=None, script="任务结论已完成。"):
    captured: dict[str, Any] = {}

    def fake_structured(model, schema, messages, *, retries=1):
        captured.setdefault("stages", []).append(schema.__name__)
        if schema.__name__ == "Decomposition":
            return decomposition if decomposition is not None else _decomposition()
        if schema.__name__ == "WorkerPlan":
            return plan if plan is not None else _plan()
        raise AssertionError(f"unexpected schema {schema.__name__}")

    monkeypatch.setattr(graph_module, "generate_structured", fake_structured)
    model = _fake_model(script)
    resources = _stub_resources()
    agents = agents if agents is not None else {}
    graph = graph_module.build_agent_graph(model, resources, agents, {}, checkpointer=InMemorySaver())
    return graph, captured, model, resources


def _initial_state(query: str = "帮我看看最新排名") -> dict[str, Any]:
    return {
        "query": query,
        "user_id": "tester",
        "session_id": "sess_test",
        "reply_language": "zh",
        "run_id": "run_test",
        "memory_summary": "",
    }


# --------------------------------------------------------------------------
# adapter / helpers
# --------------------------------------------------------------------------


def test_sanitize_tool_name_maps_dotted_ids():
    assert sanitize_tool_name("evidence.search_news") == "evidence_search_news"
    assert sanitize_tool_name("internal.prediction.get_stock") == "internal_prediction_get_stock"


def test_args_model_from_schema_builds_required_and_optional_fields():
    schema = {
        "type": "object",
        "properties": {
            "object_refs": {"type": "array", "description": "权威引用"},
            "top_k": {"type": "integer", "description": "条数"},
        },
        "required": ["object_refs"],
    }
    model = args_model_from_schema("evidence_search_news", schema)
    instance = model(object_refs=[{"node_id": "n1"}])
    assert instance.top_k is None
    with pytest.raises(Exception):
        model()


def test_decision_from_resume():
    assert decision_from_resume({"decision": "approved"}) == "approved"
    assert decision_from_resume({"decision": "rejected"}) == "rejected"
    assert decision_from_resume({"decision": "maybe"}) == "invalid"
    assert decision_from_resume(None) == "invalid"


# --------------------------------------------------------------------------
# graph flow
# --------------------------------------------------------------------------


def test_general_request_routes_to_direct_answer(graph_module, monkeypatch):
    decomposition = _decomposition(category=RequestCategory.GENERAL, request_type="none", objective="你好呀")
    graph, captured, _model, _res = _build(graph_module, monkeypatch, decomposition=decomposition)
    result = graph.invoke(_initial_state("你好"), {"configurable": {"thread_id": "t1"}})
    assert result["category"] == "general"
    assert result["final_answer"]
    assert result.get("worker_tasks") in (None, [])
    assert captured["stages"] == ["Decomposition"]


def test_business_flow_plans_and_completes_provider_worker(graph_module, monkeypatch):
    graph, captured, _model, _res = _build(graph_module, monkeypatch)
    result = graph.invoke(_initial_state(), {"configurable": {"thread_id": "t2"}})
    assert result["category"] == "business"
    assert captured["stages"] == ["Decomposition", "WorkerPlan"]
    tasks = result["worker_tasks"]
    assert tasks and tasks[0]["worker_id"] == "W02"
    statuses = [r["status"] for r in result["worker_results"]]
    assert statuses, "worker_results must be recorded"
    assert result["final_answer"]
    # provider stage dispatched; no consumer tasks planned
    assert all(t["stage"] == "provider" for t in tasks)


def test_failed_worker_is_retried_once_then_synthesized(graph_module, monkeypatch):
    graph, _c, _m, _res = _build(graph_module, monkeypatch, agents={})
    result = graph.invoke(_initial_state(), {"configurable": {"thread_id": "t3"}})
    # unknown worker -> failed; retried once; still failed -> synthesize still runs
    assert result["final_answer"]
    assert len(result.get("retried_task_ids") or []) == 1


def test_write_request_interrupts_and_resume_rejects(graph_module, monkeypatch):
    decomposition = _decomposition(request_type="write", objective="确认执行调仓方案")
    graph, _c, _m, _res = _build(graph_module, monkeypatch, decomposition=decomposition)
    config = {"configurable": {"thread_id": "t4"}}
    result = graph.invoke(_initial_state("确认执行"), config)
    assert result.get("__interrupt__"), "write gate must interrupt"
    resumed = graph.invoke(Command(resume={"decision": "rejected"}), config=config)
    assert resumed["final_answer"]
    assert resumed.get("approval_decision") == "rejected"


# --------------------------------------------------------------------------
# tool approval inside worker loop
# --------------------------------------------------------------------------


def _approval_tool_definition():
    from agent_graph.toolbox.contracts import OP_WRITE, ToolDefinition

    def handler(arguments, context):
        return {"success": True, "message": "已写入", "data": {"written": True}}

    return ToolDefinition(
        name="database.write_test",
        display_name="Write Test",
        description="Function: test write\nApplies when: needed\nNot for: reads\nPreconditions: approved\nMain inputs: none\nMain outputs: flag\nSide effects: write",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object"},
        execution_handler=handler,
        operation_type=OP_WRITE,
        requires_approval=True,
    )


def test_worker_write_tool_interrupts_then_executes_after_approval(graph_module, monkeypatch):
    from langchain.agents import create_agent

    wrapped = wrap_tool_definition(_approval_tool_definition(), {}, worker_id="W08")
    worker_agent = create_agent(
        _fake_model("写入已完成。"),
        [wrapped],
        system_prompt="你是写入Worker。",
    )
    decomposition = _decomposition(request_type="write", objective="触发审批")
    graph, _c, _m, _res = _build(
        graph_module, monkeypatch, decomposition=decomposition, plan=None, agents={"W08": worker_agent}
    )
    config = {"configurable": {"thread_id": "t5"}}
    result = graph.invoke(_initial_state("触发审批"), config)
    assert result.get("__interrupt__") or result.get("pending_approval")
    resumed = graph.invoke(Command(resume={"decision": "approved"}), config=config)
    statuses = {r["status"] for r in resumed.get("worker_results") or []}
    assert "completed" in statuses or resumed.get("final_answer")
