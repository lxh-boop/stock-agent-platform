from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.collaboration.planner import CoordinatorPlanner, CoordinatorPlanningError
from agent.collaboration.worker_directory import CapabilityWorkerDirectory


class _Tools:
    def semantic_output_slots(self, worker_role, *, tool_names=None):
        del tool_names
        if worker_role == "EVIDENCE_COLLECTOR":
            return ["entity_external_evidence"]
        return []


class _LLM:
    # 合并规划伪 LLM：固定返回预置 payload，记录 stage
    def __init__(self, payload):
        self.payload = payload
        self.stages = []

    def generate_json(self, **kwargs):
        self.stages.append(kwargs["stage"])
        kwargs["validator"](self.payload)
        return self.payload


def _worker_need(description="查询外部证据"):
    return {
        "description": description,
        "required": True,
        "requirements": [
            {"semantic_key": "external_evidence", "direction": "output", "required": True},
        ],
        "binding": {"worker_binding": {
            "worker_id": "W01",
            "objective": "查询外部数据",
            "desired_output_data_names": ["evidence"],
        }},
    }


def _reuse_need(turn_id="turn_01", derive_args=None):
    need = {
        "description": "形成目标证券分析",
        "required": True,
        "requirements": [
            {"semantic_key": "entity_analysis", "direction": "output", "required": True},
        ],
        "binding": {"reuse_binding": {
            "turn_id": turn_id,
            "derive_args": dict(derive_args or {}),
        }},
    }
    return need


def _candidates():
    return [{
        "turn_id": "turn_01",
        "trade_date": "2026-08-25",
        "answer_excerpt": "今日涨幅前10为：600519 贵州茅台 ...",
    }]


def _plan(payload, *, context_binding=None, reuse_candidates=None):
    llm = _LLM(payload)
    planner = CoordinatorPlanner(CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_Tools())
    tasks, meta = planner.plan(
        query="分析目标股票",
        effect_limit="read",
        session_id="s",
        run_id="r",
        user_id="u",
        focus_refs=[SimpleNamespace(role="focus")],
        context_refs=[],
        memory_summary="",
        request_id="R01",
        task_id_prefix="R01-",
        context_binding=dict(context_binding or {}),
        reuse_candidates=reuse_candidates,
    )
    return llm, tasks, meta


def test_mixed_worker_and_reuse_bindings_compile_partial_dag() -> None:
    # 一个 Need 走计算、一个 Need 走复用：只有计算路线进 Worker DAG
    llm, tasks, meta = _plan(
        {"needs": [_worker_need(), _reuse_need()], "selection_reason": "分析方向与上轮一致，复用。"},
        context_binding={"freshness_expectation": "reusable", "reuse_reference": ["turn_01"]},
        reuse_candidates=_candidates(),
    )
    assert llm.stages == ["upfront_merged_need_worker_planning"]
    assert [task.worker_id for task in tasks] == ["W01"]
    assert meta["reuse_decisions"] == [{"need_id": "N02", "turn_id": "turn_01", "derive_args": {}}]
    full_contract = meta["request_need_contract"]
    assert len(full_contract["needs"]) == 2
    assert full_contract["needs"][1]["binding"]["type"] == "reuse"
    assert full_contract["needs"][0]["binding"]["type"] == "worker"


def test_reuse_binding_must_reference_injected_candidate() -> None:
    # 复用引用不在注入候选内 = 幻觉引用，直接拒
    with pytest.raises(CoordinatorPlanningError, match="reuse_binding_unknown_turn"):
        _plan(
            {"needs": [_reuse_need(turn_id="turn_99")]},
            context_binding={"freshness_expectation": "reusable", "reuse_reference": ["turn_01"]},
            reuse_candidates=_candidates(),
        )


def test_need_binding_exactly_one_required() -> None:
    both = _worker_need()
    both["binding"]["reuse_binding"] = {"turn_id": "turn_01", "derive_args": {}}
    with pytest.raises(CoordinatorPlanningError, match="need_binding_exactly_one_required"):
        _plan(
            {"needs": [both]},
            context_binding={"freshness_expectation": "reusable"},
            reuse_candidates=_candidates(),
        )
    neither = _worker_need()
    neither["binding"] = {}
    with pytest.raises(CoordinatorPlanningError, match="need_binding_exactly_one_required"):
        _plan({"needs": [neither]})


def test_latest_intent_forbids_reuse_binding() -> None:
    with pytest.raises(CoordinatorPlanningError, match="reuse_binding_forbidden_for_latest"):
        _plan(
            {"needs": [_reuse_need()]},
            context_binding={"freshness_expectation": "latest", "reuse_reference": ["turn_01"]},
            reuse_candidates=_candidates(),
        )


def test_all_needs_reuse_satisfied_skips_worker_dag() -> None:
    # 全复用零 Worker：无 DAG 可编译，返回空任务集，复用决策完整保留
    _, tasks, meta = _plan(
        {"needs": [_reuse_need(derive_args={"top_n": 5})], "selection_reason": "上轮top10可裁剪为前5。"},
        context_binding={"freshness_expectation": "reusable", "reuse_reference": ["turn_01"]},
        reuse_candidates=_candidates(),
    )
    assert tasks == []
    assert meta["planning_mode"] == "merged_need_binding_all_reuse"
    assert meta["reuse_decisions"] == [{"need_id": "N01", "turn_id": "turn_01", "derive_args": {"top_n": 5}}]
    assert meta["task_count"] == 0


def test_unknown_worker_in_binding_rejected() -> None:
    bad = _worker_need()
    bad["binding"]["worker_binding"]["worker_id"] = "W99"
    with pytest.raises(CoordinatorPlanningError, match="unknown_worker_call"):
        _plan({"needs": [bad]})
