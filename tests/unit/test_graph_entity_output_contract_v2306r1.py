from __future__ import annotations

import json

import pytest

from agent.collaboration.request_bundle import (
    RequestBundleError,
    RequestDecomposer,
)


class _CaptureLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.kwargs = None

    def generate_json(self, **kwargs):
        self.kwargs = kwargs
        kwargs["validator"](self.payload)
        return self.payload


def _binding(**overrides):
    binding = {
        "entity_scope": "none",
        "inherit_previous_focus": False,
        "reference_entity_type": "none",
        "reason": "test",
    }
    binding.update(overrides)
    return binding


def _business(mentions=None, binding=None):
    return {
        "source_index": 1,
        "category": "business",
        "objective": "分析目标股票",
        "request_type": "read",
        "proposal_required": False,
        "target": {},
        "constraints": [],
        "depends_on": [],
        "scope": "current_turn",
        "status": "pending",
        "reason": "",
        "action_type": "",
        "mentions": list(mentions) if mentions is not None else [],
        "presentation": {},
        "context_binding": binding or _binding(),
    }


def _decompose(llm: _CaptureLLM, reuse_candidates=None):
    return RequestDecomposer(llm_service=llm).decompose(
        query="分析贵州茅台",
        memory_summary="",
        execution_context={},
        language="zh",
        run_id="r",
        reuse_candidates=reuse_candidates,
    )


def test_mentions_extracted_inside_decompose_and_attached_to_request_item() -> None:
    llm = _CaptureLLM({
        "requests": [_business(mentions=[{"text": "贵州茅台", "role": "focus"}])],
    })
    bundle = _decompose(llm)
    assert bundle.requests[0].mentions == [{"text": "贵州茅台", "role": "focus"}]
    call = llm.kwargs
    # 实体提取已并入拆请求：只有这一个 stage，不再有独立的实体提取调用
    assert call["stage"] == "request_bundle_decomposition"
    system_prompt = call["messages"][0]["content"]
    assert "focus、comparison、cause、impact_target、context、event" in system_prompt
    assert "mentions" in system_prompt


def test_portfolio_scope_request_allows_empty_mentions() -> None:
    llm = _CaptureLLM({
        "requests": [_business(mentions=[], binding=_binding(entity_scope="portfolio", reference_entity_type="portfolio"))],
    })
    bundle = _decompose(llm)
    assert bundle.requests[0].mentions == []


def test_entity_mention_validator_rejects_non_list_mentions() -> None:
    llm = _CaptureLLM({
        "requests": [_business(mentions={"text": "贵州茅台", "role": "focus"})],
    })
    # mentions 非数组时构造阶段直接报错（validate_payload 第一道闸门）
    with pytest.raises((RequestBundleError, TypeError)):
        _decompose(llm)


def test_entity_mention_validator_rejects_invalid_role() -> None:
    llm = _CaptureLLM({
        "requests": [_business(mentions=[{"text": "贵州茅台", "role": "owner"}])],
    })
    with pytest.raises(RequestBundleError, match="invalid_entity_role"):
        _decompose(llm)


def _reuse_candidates():
    return {
        "items": [
            {
                "turn_id": "turn_01",
                "user_summary": "今天涨幅前10",
                "assistant_summary": "已给出top10评分",
                "freshness": "current_trade_day",
            }
        ],
        "guards": {"turn_01": {"turn_id": "turn_real_abc", "trade_date": "2026-08-25"}},
    }


def test_reuse_reference_must_come_from_injected_candidates() -> None:
    llm = _CaptureLLM({
        "requests": [_business(binding=_binding(
            freshness_expectation="reusable",
            reuse_reference=["turn_99"],
        ))],
    })
    with pytest.raises(RequestBundleError, match="reuse_reference_unknown_turn"):
        _decompose(llm, reuse_candidates=_reuse_candidates())


def test_latest_intent_forbids_reuse_reference() -> None:
    llm = _CaptureLLM({
        "requests": [_business(binding=_binding(
            freshness_expectation="latest",
            reuse_reference=["turn_01"],
        ))],
    })
    with pytest.raises(RequestBundleError, match="reuse_reference_forbidden_for_freshness"):
        _decompose(llm, reuse_candidates=_reuse_candidates())


def test_unspecified_intent_forbids_reuse_reference() -> None:
    llm = _CaptureLLM({
        "requests": [_business(binding=_binding(
            freshness_expectation="unspecified",
            reuse_reference=["turn_01"],
        ))],
    })
    with pytest.raises(RequestBundleError, match="reuse_reference_forbidden_for_freshness"):
        _decompose(llm, reuse_candidates=_reuse_candidates())


def test_valid_reuse_reference_flows_into_context_binding_and_guards_attached() -> None:
    llm = _CaptureLLM({
        "requests": [_business(binding=_binding(
            freshness_expectation="reference",
            reuse_reference=["turn_01"],
        ))],
    })
    bundle = _decompose(llm, reuse_candidates=_reuse_candidates())
    binding = bundle.requests[0].context_binding
    assert binding.freshness_expectation.value == "reference"
    assert list(binding.reuse_reference) == ["turn_01"]
    # 校验保护面随 bundle 传递（仅服务端持有）
    assert bundle.reuse_guards["turn_01"]["turn_id"] == "turn_real_abc"
    # 候选清单进入了 prompt
    user_payload = json.loads(llm.kwargs["messages"][1]["content"])
    assert any("turn_01" in line for line in user_payload["reuse_candidates"])


def test_write_request_cannot_carry_reuse_reference() -> None:
    row = _business(binding=_binding(
        freshness_expectation="reusable",
        reuse_reference=["turn_01"],
    ))
    row["request_type"] = "write"
    row["action_type"] = "confirm_execute"
    llm = _CaptureLLM({"requests": [row]})
    with pytest.raises(RequestBundleError, match="write_request_cannot_reuse"):
        _decompose(llm, reuse_candidates=_reuse_candidates())
