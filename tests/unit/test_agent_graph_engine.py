"""Unit tests for the single-agent (agent-as-tool) engine.

Offline: structured specialist loops use ``GenericFakeChatModel``, approval
uses ``InMemorySaver``, and the graph registry is a minimal real ToolRegistry.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import agent_graph.graph as graph_module
from agent_graph.approval import decision_from_resume
from agent_graph.messages_utils import summarize_messages
from agent_graph.specialists import build_specialist_tools
from agent_graph.tools.main_tools import _trim_tool_result
from agent_graph.tools.runtime import AgentResources


class _ToolAwareFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel without bind_tools; create_agent requires it."""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003 - test stub
        return self


def _fake_model(script: str = "好的，任务完成。") -> GenericFakeChatModel:
    return _ToolAwareFakeChatModel(messages=iter([AIMessage(content=script)] * 10000))


def _tool_loop_model(messages: list[AIMessage]) -> GenericFakeChatModel:
    # No cycling: replaying the same tool_call id would trip create_agent's
    # synthetic-tool-message branch (returns 'model', not a legal destination).
    return _ToolAwareFakeChatModel(messages=iter(list(messages)))


def _resources(registry=None, provider=None) -> AgentResources:
    return AgentResources(
        store=None, provider=provider, impact_service=None, tool_registry=registry, context={ "user_id": "tester"}
    )


def _initial_state(query: str = "帮我看看最新排名") -> dict[str, Any]:
    return {
        "query": query,
        "user_id": "tester",
        "session_id": "sess_test",
        "reply_language": "zh",
        "run_id": "run_test",
        "memory_summary": "",
    }


def _read_tool_definition(name: str, payload: dict[str, Any]):
    from agent_graph.toolbox.contracts import ToolDefinition
    from agent_graph.toolbox.validation import result_schema, schema

    def handler(arguments, context):
        return {"success": True, "message": "ok", "data": payload}

    return ToolDefinition(
        name=name,
        display_name=name,
        description="Function: test read\nApplies when: needed\nNot for: writes\nPreconditions: none\nMain inputs: none\nMain outputs: payload\nSide effects: none",
        input_schema=schema({"top_k": {"type": "integer"}}),
        output_schema=result_schema(["payload"]),
        execution_handler=handler,
    )


def _gated_write_tool_definition(name: str):
    from agent_graph.toolbox.contracts import OP_WRITE, ToolDefinition
    from agent_graph.toolbox.validation import result_schema, schema

    def handler(arguments, context):
        return {"success": True, "message": "已写入", "data": {"written": True}}

    return ToolDefinition(
        name=name,
        display_name=name,
        description="Function: test write\nApplies when: needed\nNot for: reads\nPreconditions: approved\nMain inputs: none\nMain outputs: flag\nSide effects: write",
        input_schema=schema({}),
        output_schema=result_schema(["written"]),
        execution_handler=handler,
        operation_type=OP_WRITE,
        requires_approval=True,
    )


# --------------------------------------------------------------------------
# tool layer
# --------------------------------------------------------------------------


def test_specialist_tools_built_from_registry():
    from agent_graph.toolbox.registry import ToolRegistry

    registry = ToolRegistry([_read_tool_definition("evidence.search_news", {"records": [1]})])
    tools = build_specialist_tools(_fake_model(), registry, {"user_id": "tester"})
    names = {tool.name for tool in tools}
    assert "evidence_collector" in names
    assert "internal_data_provider" not in names  # its tools are not in the registry


def test_trim_tool_result_strips_secrets():
    trimmed = _trim_tool_result(
        {"success": True, "message": "ok", "confirmation_token": "secret", "data": {"plan_id": "p1", "confirmation_token": "x"}}
    )
    assert "secret" not in trimmed
    assert "p1" in trimmed


def test_decision_from_resume_values():
    assert decision_from_resume({"decision": "approved"}) == "approved"
    assert decision_from_resume({"decision": "maybe"}) == "invalid"


# --------------------------------------------------------------------------
# graph flow
# --------------------------------------------------------------------------


def test_general_question_answered_without_tools():
    resources = _resources()
    graph = graph_module.build_agent_graph(_fake_model("你好！有什么可以帮你？"), resources, {}, checkpointer=InMemorySaver())
    result = graph.invoke(_initial_state("你好"), {"configurable": {"thread_id": "t1"}})
    answer, _calls, _refs = summarize_messages(result.get("messages") or [])
    assert answer
    assert result.get("pending_approval") is None


def test_specialist_tool_invocation_inside_agent_loop():
    from agent_graph.toolbox.registry import ToolRegistry

    registry = ToolRegistry([_read_tool_definition("evidence.search_news", {"records": [{"news_id": "n1"}]})])
    model = _tool_loop_model(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "evidence_collector", "args": {"objective": "查最新新闻"}, "id": "c1"}],
            ),
            AIMessage(content="根据证据：有 1 条新闻。"),
        ]
    )
    graph = graph_module.build_agent_graph(
        model,
        _resources(registry),
        {},
        checkpointer=InMemorySaver(),
        specialist_model=_fake_model("专项结论：检索到 1 条新闻。"),
    )
    result = graph.invoke(_initial_state(), {"configurable": {"thread_id": "t2"}})
    answer, calls, _refs = summarize_messages(result.get("messages") or [])
    assert answer
    assert any(call["tool"] == "evidence_collector" for call in calls)


def test_write_tool_interrupts_then_executes_after_approval():
    from agent_graph.toolbox.registry import ToolRegistry

    write_name = "database.write_portfolio_graph_context"
    registry = ToolRegistry([_gated_write_tool_definition(write_name)])
    model = _tool_loop_model(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "database_write_portfolio_graph_context", "args": {}, "id": "c1"}],
            ),
            AIMessage(content="写入完成。"),
        ]
    )
    graph = graph_module.build_agent_graph(model, _resources(registry), {}, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t3"}}
    result = graph.invoke(_initial_state("写入组合上下文"), config)
    assert result.get("__interrupt__") or result.get("pending_approval")
    resumed = graph.invoke(Command(resume={"decision": "approved"}), config=config)
    answer, calls, _refs = summarize_messages(resumed.get("messages") or [])
    assert answer
    assert any(call["tool"] == "database_write_portfolio_graph_context" for call in calls)


def test_write_tool_rejection_ends_run_with_cancel_answer():
    from agent_graph.toolbox.registry import ToolRegistry

    write_name = "database.write_evidence_graph_context"
    registry = ToolRegistry([_gated_write_tool_definition(write_name)])
    model = _tool_loop_model(
        [
            AIMessage(
                content="",
                tool_calls=[{"name": "database_write_evidence_graph_context", "args": {}, "id": "c1"}],
            ),
            AIMessage(content="已按用户要求取消该操作。"),
        ]
    )
    graph = graph_module.build_agent_graph(model, _resources(registry), {}, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t4"}}
    graph.invoke(_initial_state("写入证据上下文"), config)
    resumed = graph.invoke(Command(resume={"decision": "rejected"}), config=config)
    answer, _calls, _refs = summarize_messages(resumed.get("messages") or [])
    # rejection flows back into the tool, which returns the cancelled marker
    assert "未获批准" in answer or "取消" in answer or "rejected" in answer
