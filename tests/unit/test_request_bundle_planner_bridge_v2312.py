from __future__ import annotations

from agent.capabilities import CapabilityContract
from agent.collaboration.planner import CoordinatorPlanner
from agent.collaboration.worker_directory import CapabilityWorkerDirectory


class _ScopeToolDirectory:
    def semantic_output_slots(self, worker_role, *, tool_names=None):
        del tool_names
        if worker_role == "ENTITY_ANALYST":
            return []
        if worker_role == "PORTFOLIO_ANALYST":
            return [
                "current_portfolio_state", "portfolio_positions", "user_constraints",
                "user_profile_state", "entity_model_signals", "market_ranking_signals",
            ]
        return []


class _BusinessNeedLLM:
    def __init__(self):
        self.stages = []

    def generate_json(self, **kwargs):
        stage = kwargs["stage"]
        self.stages.append(stage)
        if stage == "upfront_merged_need_worker_planning":
            payload = {
                "needs": [{
                    "description": "基于当前Request可见的工作记忆形成比较分析",
                    "required": True,
                    "requirements": [{
                        "semantic_key": "entity_analysis",
                        "direction": "output",
                        "required": True,
                    }],
                    "binding": {
                        "worker_binding": {
                            "worker_id": "W09",
                            "objective": "基于当前Request目标形成比较分析",
                            "desired_output_data_names": ["analysis"],
                        },
                    },
                }],
                "selection_reason": "W09负责形成结构化分析",
            }
        else:
            raise AssertionError(f"unexpected MainAgent stage: {stage}")
        kwargs["validator"](payload)
        return payload


def _plan(request_id: str = "R03"):
    llm = _BusinessNeedLLM()
    planner = CoordinatorPlanner(
        CapabilityWorkerDirectory(), llm_service=llm, worker_tool_directory=_ScopeToolDirectory()
    )
    tasks, meta = planner.plan(
        query="比较前面两只股票的分析结果",
        effect_limit="read",
        session_id="s",
        run_id="r",
        user_id="u",
        focus_refs=[],
        context_refs=[],
        memory_summary="",
        request_id=request_id,
        task_id_prefix=f"{request_id}-",
        authoritative_target={"semantic_target": {"entity_type": "security", "display_text": "前序已解析实体", "source": "conversation_context", "status": "identified"}, "graph_refs": []},
        request_constraints=["沿用前序Request已验证结果"],
    )
    return llm, tasks, meta


def test_business_request_need_carries_authoritative_request_fields_and_no_per_request_final_worker() -> None:
    llm, tasks, meta = _plan()

    assert llm.stages == ["upfront_merged_need_worker_planning"]
    assert len(tasks) == 1
    assert tasks[0].task_id == "R03-T01"
    assert tasks[0].worker_id == "W09"
    assert tasks[0].metadata["request_id"] == "R03"

    request_need_contract = meta["request_need_contract"]
    assert request_need_contract["schema_version"] == "request_need_contract.v1"
    assert request_need_contract["request_id"] == "R03"
    assert request_need_contract["request_objective"] == "比较前面两只股票的分析结果"
    assert request_need_contract["authoritative_target"]["semantic_target"]["display_text"] == "前序已解析实体"
    assert request_need_contract["constraints"] == ["沿用前序Request已验证结果"]
    assert all(need["request_id"] == "R03" for need in request_need_contract["needs"])
    assert all(need["need_id"] != "N_FINAL" for need in request_need_contract["needs"])

    contract = CapabilityContract.from_dict(tasks[0].contracts[0])
    # Request DAG only controls execution order. Prior business values are read from
    # RunContextStore Working Memory rather than transported as a dependency Slot.
    assert "request_dependency_results" not in contract.input_data_names(required_only=True)
    assert CapabilityWorkerDirectory().get("W09").working_memory_mode == "consumer"
    assert "analysis" in tasks[0].expected_data_names


def test_request_dependencies_do_not_reappear_as_need_transport_contract() -> None:
    _, _, meta = _plan()
    serialized = str(meta["request_need_contract"])
    assert "request_dependency_results" not in serialized
    assert meta["business_data_owner"] == "run_context_store"
    assert meta["task_dependency_owner"] == "request_task_state"


def test_bundle_business_task_namespace_prevents_cross_request_task_id_collision() -> None:
    for request_id in ("R01", "R02"):
        _, tasks, _ = _plan(request_id=request_id)
        assert tasks[0].task_id == f"{request_id}-T01"
