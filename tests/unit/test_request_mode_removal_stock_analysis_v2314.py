from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.capabilities import NeedRequirementCompiler
from agent.collaboration.planner import CoordinatorPlanner
from agent.collaboration.request_bundle import RequestDecomposer
from agent.collaboration.worker_contracts import WorkerContractViolation
from agent.collaboration.worker_directory import CapabilityWorkerDirectory


class _QueueLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.stages = []

    def generate_json(self, **kwargs):
        self.stages.append(kwargs.get("stage"))
        payload = self.payloads.pop(0)
        kwargs["validator"](payload)
        return payload


class _Tools:
    def semantic_output_slots(self, worker_role, *, tool_names=None):
        del tool_names
        return {
            "EVIDENCE_COLLECTOR": ["entity_external_evidence"],
            "PORTFOLIO_ANALYST": ["entity_model_signals", "market_ranking_signals"],
        }.get(worker_role, [])


class _StockLLM:
    def __init__(self, ranked=False):
        self.stages = []
        self.ranked = ranked

    def generate_json(self, **kwargs):
        stage = kwargs["stage"]
        self.stages.append(stage)
        if stage == "upfront_merged_need_worker_planning":
            # 合并规划：每个 Need 生成时直接声明 worker_binding
            if self.ranked:
                needs = [
                    {
                        "description": "定位模型排名第一的股票并查询内部数据",
                        "required": True,
                        "requirements": [
                            {"semantic_key": "market_ranking", "direction": "output", "required": True},
                            {"semantic_key": "entity_model_signals", "direction": "output", "required": True},
                        ],
                        "binding": {"worker_binding": {
                            "worker_id": "W02",
                            "objective": "定位排名第一实体并查询内部数据",
                            "desired_output_data_names": ["ranking", "prediction"],
                        }},
                    },
                    {
                        "description": "分析已定位股票",
                        "required": True,
                        "requirements": [
                            {"semantic_key": "entity_analysis", "direction": "output", "required": True},
                        ],
                        "binding": {"worker_binding": {
                            "worker_id": "W09",
                            "objective": "分析已定位股票",
                            "desired_output_data_names": ["analysis"],
                        }},
                    },
                ]
            else:
                needs = [
                    {
                        "description": "查询外部证据",
                        "required": True,
                        "requirements": [
                            {"semantic_key": "external_evidence", "direction": "output", "required": True},
                        ],
                        "binding": {"worker_binding": {
                            "worker_id": "W01",
                            "objective": "查询外部证据",
                            "desired_output_data_names": ["evidence"],
                        }},
                    },
                    {
                        "description": "查询内部预测",
                        "required": True,
                        "requirements": [
                            {"semantic_key": "entity_model_signals", "direction": "output", "required": True},
                        ],
                        "binding": {"worker_binding": {
                            "worker_id": "W02",
                            "objective": "查询内部预测",
                            "desired_output_data_names": ["prediction"],
                        }},
                    },
                    {
                        "description": "分析目标证券",
                        "required": True,
                        "requirements": [
                            {"semantic_key": "entity_analysis", "direction": "output", "required": True},
                            {"semantic_key": "entity_uncertainty", "direction": "output", "required": True},
                        ],
                        "binding": {"worker_binding": {
                            "worker_id": "W09",
                            "objective": "分析目标证券",
                            "desired_output_data_names": ["analysis", "analysis_uncertainty"],
                        }},
                    },
                ]
            payload = {"needs": needs, "selection_reason": "查询Worker写ContextBundle，W09只分析。"}
        else:
            raise AssertionError(stage)
        kwargs["validator"](payload)
        return payload


def _binding(scope="none", inherit=False, ref_type="none"):
    return {
        "entity_scope": scope,
        "inherit_previous_focus": inherit,
        "reference_entity_type": ref_type,
        "reason": "test",
    }


def test_old_request_mode_routing_is_absent_from_production_source() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / "agent/collaboration/entry_decision.py").exists()
    for token in ("RequestMode", "EntryDecision", "MainEntryDecisionPlanner", "request_mode", "allowed_request_modes"):
        for base in (root / "agent", root / "core"):
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                assert token not in path.read_text(encoding="utf-8"), f"{token} remains in {path.relative_to(root)}"


def test_explicit_request_boundary_does_not_overwrite_normalized_objective() -> None:
    llm = _QueueLLM([{"requests": [
        {
            "source_index": 1, "category": "business",
            "objective": "分析目标股票", "request_type": "read", "proposal_required": False,
            "target": {"stock_code": "600519"}, "constraints": [], "depends_on": [],
            "scope": "current_turn", "status": "pending", "reason": "", "action_type": "",
            "presentation": {}, "context_binding": _binding("explicit_entities", False, "security"),
        },
        {
            "source_index": 2, "category": "business",
            "objective": "分析模型预测排名第一的股票", "request_type": "read", "proposal_required": False,
            "target": {"selector": "model_rank_1"}, "constraints": [], "depends_on": [],
            "scope": "current_turn", "status": "pending", "reason": "", "action_type": "",
            "presentation": {}, "context_binding": _binding(),
        },
        {
            "source_index": 3, "category": "business",
            "objective": "比较前两项分析结果", "request_type": "read", "proposal_required": False,
            "target": {}, "constraints": [], "depends_on": [1, 2],
            "scope": "current_turn", "status": "pending", "reason": "", "action_type": "",
            "presentation": {}, "context_binding": _binding(),
        },
    ]}])
    bundle = RequestDecomposer(llm_service=llm).decompose(
        query="1.帮我瞅瞅600519\n2.分析模型预测排名第一的股票\n3.对比一下",
        memory_summary="", execution_context={}, language="zh", run_id="r",
    )
    assert [x.objective for x in bundle.requests] == [
        "分析目标股票", "分析模型预测排名第一的股票", "比较前两项分析结果"
    ]
    assert bundle.requests[2].depends_on == ["R01", "R02"]
    assert llm.stages == ["request_bundle_decomposition"]


def test_generic_stock_analysis_compiles_providers_then_w09_without_business_input_binding() -> None:
    llm = _StockLLM()
    planner = CoordinatorPlanner(CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_Tools())
    tasks, meta = planner.plan(
        query="分析目标股票", request_target={"stock_code": "600519"}, effect_limit="read",
        session_id="s", run_id="r", user_id="u",
        focus_refs=[SimpleNamespace(role="focus")], context_refs=[], memory_summary="",
        request_id="R01", task_id_prefix="R01-",
    )
    assert llm.stages == ["upfront_merged_need_worker_planning"]
    assert [task.worker_id for task in tasks] == ["W01", "W02", "W09"]
    assert {name for task in tasks for name in task.expected_data_names} == {
        "evidence", "prediction", "analysis", "analysis_uncertainty"
    }
    assert set(tasks[2].dependency_task_ids) == {"R01-T01", "R01-T02"}
    assert tasks[2].contracts[0]["required_data"] == []
    contract = meta["request_need_contract"]
    assert contract["schema_version"] == "request_need_contract.v1"
    assert contract["requirement_contract_version"] == NeedRequirementCompiler.SCHEMA_VERSION
    assert contract["request_objective"] == "分析目标股票"
    assert contract["request_target"] == {"stock_code": "600519"}


def test_request_need_llm_cannot_reintroduce_removed_semantic_fields() -> None:
    llm = _QueueLLM([{
        "constraints": ["不应由Need阶段重新生成"],
        "needs": [{
            "description": "形成分析", "required": True,
            "requirements": [{"semantic_key": "entity_analysis", "direction": "output", "required": True}],
        }],
    }])
    planner = CoordinatorPlanner(CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_Tools())
    with pytest.raises(WorkerContractViolation) as caught:
        planner._plan_request_need_contract(
            query="分析目标股票", effect_limit="read", run_id="r", language="zh",
            initial_context_names={"authoritative_entity_refs"}, memory_summary="",
            context_binding={}, request_id="R01", request_target={"stock_code": "600519"},
        )
    assert caught.value.code == "request_need_unexpected_top_level_field"


def test_ranked_stock_analysis_uses_w02_then_w09_and_no_deep_business_binding() -> None:
    llm = _StockLLM(ranked=True)
    planner = CoordinatorPlanner(CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_Tools())
    tasks, meta = planner.plan(
        query="分析模型预测排名第一的股票", request_target={"selector": "model_rank_1"}, effect_limit="read",
        session_id="s", run_id="r", user_id="u", focus_refs=[], context_refs=[], memory_summary="",
        request_id="R02", task_id_prefix="R02-",
    )
    assert [task.worker_id for task in tasks] == ["W02", "W09"]
    assert set(tasks[0].expected_data_names) == {"ranking", "prediction"}
    assert tasks[1].expected_data_names == ["analysis"]
    assert tasks[1].dependency_task_ids == ["R02-T01"]
    assert tasks[1].contracts[0]["required_data"] == []
    assert meta["request_need_contract"]["request_target"] == {"selector": "model_rank_1"}
