from agent.collaboration.request_bundle import (
    RequestCategory,
    RequestDecomposer,
)


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None

    def generate_json(self, **kwargs):
        self.kwargs = kwargs
        return self.payload


def test_short_greeting_uses_deterministic_general_route_without_llm():
    class _UnexpectedLLM:
        def generate_json(self, **kwargs):
            raise AssertionError("general greeting should not call planner LLM")

    bundle = RequestDecomposer(llm_service=_UnexpectedLLM()).decompose(
        query="你好",
        memory_summary="",
        execution_context={},
        language="zh",
        run_id="run_general",
    )

    assert bundle.decomposition_source == "deterministic_general_route"
    assert [item.category for item in bundle.requests] == [RequestCategory.GENERAL]
    assert bundle.requests[0].objective == "你好"
    assert all(not item.request_type == "write" for item in bundle.requests)


def test_llm_can_classify_general_request_without_worker_or_tool_fields():
    fake = _FakeLLM({
        "requests": [{
            "source_index": 1,
            "category": "general",
            "objective": "请介绍一下这个系统的用途",
            "request_type": "",
            "semantic_target": {"status": "not_required"},
            "semantic_entities": [],
            "constraints": [],
            "scope": "current_turn",
            "status": "pending",
        }]
    })

    bundle = RequestDecomposer(llm_service=fake).decompose(
        query="请介绍一下这个系统的用途",
        memory_summary="",
        execution_context={},
        language="zh",
        run_id="run_general",
    )

    assert [item.category for item in bundle.requests] == [RequestCategory.GENERAL]
    assert fake.kwargs is not None
    shape = fake.kwargs["messages"][1]["content"]
    assert "general" in shape
    assert "general|business|presentation" in shape
