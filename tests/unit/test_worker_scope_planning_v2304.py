from __future__ import annotations

import json

import pytest

from agent.capabilities import CapabilityRegistry
from agent.capabilities.validator import CapabilityPlanValidator
from agent.collaboration.planner import CoordinatorPlanner
from agent.collaboration.worker_catalog import WorkerDescriptionCatalog
from agent.collaboration.worker_directory import CapabilityWorkerDirectory

from agent.collaboration.worker_directory import PORTFOLIO_ANALYST
from agent.tool_dag.planner import WorkerToolDagPlanner
from agent.tool_dag.validation import ToolDagValidator
from agent.tool_runtime import ToolRegistry
from agent.worker_tools.internal_system import (
    INTERNAL_PORTFOLIO_GET_STATE,
    INTERNAL_USER_PROFILE_GET,
    build_internal_system_tool_definitions,
)
from agent.worker_tools.registry import WorkerToolDirectory


class _ScopeToolDirectory:
    def semantic_output_slots(self, worker_role, *, tool_names=None):
        del tool_names
        if worker_role == "PORTFOLIO_ANALYST":
            return [
                "current_portfolio_state",
                "portfolio_positions",
                "account_financial_state",
                "user_profile_state",
                "user_constraints",
                "entity_model_signals",
                "market_ranking_signals",
                "model_quality_metrics",
                "backtest_summary",
                "selected_strategy_state",
            ]
        if worker_role == "EVIDENCE_COLLECTOR":
            return ["entity_external_evidence", "evidence_source_records"]
        return []


def _contract(*, inputs, outputs, rules, effect="read"):
    del effect
    return {
        "description": "worker-scope contract",
        "required_data": [
            {"name": name, "required": True, "source_policy": "system", "satisfaction_rule": "exists", "required_paths": []}
            for name in inputs
        ],
        "required_parameters": [],
        "promised_data": [
            {"name": name, "required_paths": []}
            for name in outputs
        ],
        "acceptance_rule_ids": list(rules),
        "forbidden_data_names": [],
        "criticality": "required",
        "mutation_allowed": False,
    }


def test_main_agent_catalog_exposes_one_worker_scope_not_boundary_menu() -> None:
    rows = WorkerDescriptionCatalog(
        CapabilityWorkerDirectory(),
        CapabilityRegistry(),
        worker_tool_directory=_ScopeToolDirectory(),
    ).descriptions(effect_limit="read")
    w02 = next(row for row in rows if row["worker_id"] == "W02")

    assert w02["capability_scope_mode"] == "worker_level"
    assert "supported_boundaries" not in w02
    assert "supported_boundary_ids" not in w02
    assert "portfolio" in w02["output_data_examples"]
    assert "positions" in w02["output_data_examples"]
    assert "user_profile" in w02["output_data_examples"]
    assert "user_constraints" in w02["output_data_examples"]
    assert "portfolio" in w02["produced_data_patterns"]
    assert "user_profile" in w02["produced_data_patterns"]


def test_w02_single_worker_scope_can_cover_portfolio_and_user_context_outputs() -> None:
    validator = CapabilityPlanValidator(CapabilityRegistry(), CapabilityWorkerDirectory())
    payload = {
        "goal_contract": {
            "desired_outputs": [
                "portfolio",
                "positions",
                "user_profile",
                "user_constraints",
            ],
            "required_information_slots": [],
            "effect_limit": "read",
        },
        "tasks": [{
            "task_id": "T01",
            "worker_id": "W02",
            "boundary_id": "system_internal_fact_provider",
            "objective": "读取当前持仓、用户画像和交易约束",
            "effect_limit": "read",
            "contracts": [_contract(
                inputs=[],
                outputs=[
                    "portfolio",
                    "positions",
                    "user_profile",
                    "user_constraints",
                ],
                rules=[
                    "schema_valid",
                    "business_empty_explicit",
                    "no_persistent_write",
                ],
            )],
        }],
    }
    tasks = validator.validate(payload)
    assert tasks[0].worker_id == "W02"
    assert set(tasks[0].output_data_names()) == {
        "portfolio",
        "positions",
        "user_profile",
        "user_constraints",
    }


def test_worker_scope_still_rejects_cross_worker_semantic_output() -> None:
    validator = CapabilityPlanValidator(CapabilityRegistry(), CapabilityWorkerDirectory())
    payload = {
        "goal_contract": {
            "desired_outputs": ["evidence"],
            "required_information_slots": [],
            "effect_limit": "read",
        },
        "tasks": [{
            "task_id": "T01",
            "worker_id": "W02",
            "boundary_id": "system_internal_fact_provider",
            "objective": "错误地产出外部证据",
            "effect_limit": "read",
            "contracts": [_contract(
                inputs=[],
                outputs=["evidence"],
                rules=["schema_valid", "no_persistent_write"],
            )],
        }],
    }
    with pytest.raises(Exception, match="capability_contract_outside_worker_scope"):
        validator.validate(payload)


def test_session_summary_remains_runtime_context_not_w04_business_data_name() -> None:
    rows = WorkerDescriptionCatalog(
        CapabilityWorkerDirectory(),
        CapabilityRegistry(),
        worker_tool_directory=_ScopeToolDirectory(),
    ).descriptions(effect_limit="read")
    w04 = next(row for row in rows if row["worker_id"] == "W04")
    assert "session_summary" not in w04["input_data_examples"]
    assert "session_summary" not in w04["produced_data_patterns"]
    assert "risk" in w04["produced_data_patterns"]


def test_portfolio_adjustment_uses_request_need_then_worker_assignment_without_third_mainagent_dag_call() -> None:
    class FakeLLM:
        def __init__(self) -> None:
            self.stages = []

        def generate_json(self, **kwargs):
            stage = kwargs["stage"]
            self.stages.append(stage)
            if stage == "upfront_merged_need_worker_planning":
                # 合并规划：每个 Need 生成时直接声明 worker_binding
                payload = {
                    "needs": [
                        {
                            "description": "获取当前持仓、用户画像和交易约束",
                            "required": True,
                            "requirements": [
                                {"semantic_key": "portfolio_state", "direction": "output", "required": True},
                                {"semantic_key": "portfolio_positions", "direction": "output", "required": True},
                                {"semantic_key": "user_profile", "direction": "output", "required": True},
                                {"semantic_key": "user_constraints", "direction": "output", "required": True},
                            ],
                            "binding": {"worker_binding": {
                                "worker_id": "W02",
                                "objective": "读取当前持仓、用户画像和交易约束",
                                "desired_output_data_names": ["portfolio", "positions", "user_profile", "user_constraints"],
                            }},
                        },
                        {
                            "description": "评估当前组合风险",
                            "required": True,
                            "requirements": [
                                {"semantic_key": "portfolio_state", "direction": "input", "required": True},
                                {"semantic_key": "portfolio_positions", "direction": "input", "required": True},
                                {"semantic_key": "user_constraints", "direction": "input", "required": True},
                                {"semantic_key": "portfolio_risk", "direction": "output", "required": True},
                            ],
                            "binding": {"worker_binding": {
                                "worker_id": "W04",
                                "objective": "评估当前组合风险",
                                "desired_output_data_names": ["risk"],
                            }},
                        },
                        {
                            "description": "形成待审批持仓调整方案",
                            "required": True,
                            "requirements": [
                                {"semantic_key": "portfolio_risk", "direction": "input", "required": True},
                                {"semantic_key": "user_constraints", "direction": "input", "required": True},
                                {"semantic_key": "rebalance_proposal", "direction": "output", "required": True},
                                {"semantic_key": "rebalance_instructions", "direction": "output", "required": True},
                            ],
                            "binding": {"worker_binding": {
                                "worker_id": "W05",
                                "objective": "形成待审批持仓调整方案",
                                "desired_output_data_names": ["proposal", "rebalance"],
                            }},
                        },
                    ],
                    "selection_reason": "W02提供组合与用户事实，W04评估风险，W05形成Proposal。",
                }
            else:
                raise AssertionError(stage)
            kwargs["validator"](payload)
            return payload

    llm = FakeLLM()
    planner = CoordinatorPlanner(
        CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_ScopeToolDirectory()
    )
    tasks, metadata = planner.plan(
        query="生成当前组合的待审批调整方案",
        effect_limit="proposal",
        session_id="s", run_id="r", user_id="u",
        focus_refs=[], context_refs=[], memory_summary="",
        request_id="R01", request_target={"portfolio": "current"},
    )

    assert llm.stages == ["upfront_merged_need_worker_planning"]
    assert [task.worker_id for task in tasks] == ["W02", "W04", "W05"]
    assert tasks[0].dependency_task_ids == []
    assert tasks[1].dependency_task_ids == ["T01"]
    assert set(tasks[2].dependency_task_ids) == {"T01", "T02"}
    assert metadata["capability_scope_mode"] == "worker_level"
    assert metadata["request_need_contract"]["request_objective"] == "生成当前组合的待审批调整方案"
    assert metadata["request_need_contract"]["request_target"] == {"portfolio": "current"}
    assert metadata["raw_request_semantic_owner"] == "request_bundle.objective"

def test_w02_broad_scope_private_tool_dag_reuses_existing_tool_io_contracts() -> None:
    class FakeIdentity:
        pass

    class FakeProvider:
        identity = FakeIdentity()

        def public_entity_descriptor(self, ref):
            return {}

    registry = ToolRegistry(build_internal_system_tool_definitions(FakeProvider()))
    directory = WorkerToolDirectory(registry)
    validator = ToolDagValidator(registry, directory)

    # 直接使用现有 W02 私有 Tool 合同，确认无需新增 Tool 传输协议。
    portfolio_tool = registry.get(INTERNAL_PORTFOLIO_GET_STATE)
    profile_tool = registry.get(INTERNAL_USER_PROFILE_GET)
    assert {item.slot_id for item in portfolio_tool.output_contracts} == {
        "current_portfolio_state",
        "portfolio_positions",
    }
    assert {item.slot_id for item in profile_tool.output_contracts} == {
        "user_profile_state",
        "user_constraints",
    }

    class FakeLLM:
        def generate_json(self, **kwargs):
            assert kwargs["stage"] == "worker_private_tool_dag_planner"
            payload = {
                "tasks": [
                    {
                        "tool_task_id": "P01",
                        "tool_name": INTERNAL_PORTFOLIO_GET_STATE,
                        "objective": "读取当前组合和持仓",
                        "args": {},
                        "inputs": {"user_id": {"from_context": "user_id"}},
                        "priority": 1,
                    },
                    {
                        "tool_task_id": "P02",
                        "tool_name": INTERNAL_USER_PROFILE_GET,
                        "objective": "读取用户画像和约束",
                        "args": {},
                        "inputs": {"user_id": {"from_context": "user_id"}},
                        "priority": 1,
                    },
                ],
                "final_output_task_ids": ["P01", "P02"],
            }
            kwargs["validator"](payload)
            return payload

    planner = WorkerToolDagPlanner(
        llm_service=FakeLLM(),
        directory=directory,
        validator=validator,
    )
    plan = planner.plan(
        worker_task_id="T01",
        worker_role=PORTFOLIO_ANALYST,
        worker_objective="一次读取当前持仓、用户画像和交易约束",
        # 仅保留为兼容/审计字段；MainAgent 不再选择细能力。
        boundary_id="system_internal_fact_provider",
        worker_prompt="在W02整体专业能力范围内自主规划私有Tool DAG",
        available_context={"user_id": "u"},
        required_output_keys=[
            "current_portfolio_state",
            "portfolio_positions",
            "user_profile_state",
            "user_constraints",
        ],
        completion_criteria=["schema_valid", "provenance_present"],
        allowed_tool_names=[INTERNAL_PORTFOLIO_GET_STATE, INTERNAL_USER_PROFILE_GET],
        run_id="run",
        read_only=True,
    )
    assert [task.tool_name for task in plan.tasks] == [
        INTERNAL_PORTFOLIO_GET_STATE,
        INTERNAL_USER_PROFILE_GET,
    ]
    assert plan.tasks[0].inputs["user_id"] == {"from_context": "user_id"}
    assert plan.tasks[1].inputs["user_id"] == {"from_context": "user_id"}
