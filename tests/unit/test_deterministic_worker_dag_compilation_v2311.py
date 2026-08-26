from __future__ import annotations

from types import SimpleNamespace

from agent.collaboration.planner import CoordinatorPlanner
from agent.collaboration.worker_directory import CapabilityWorkerDirectory


class _Tools:
    def semantic_output_slots(self, worker_role, *, tool_names=None):
        del tool_names
        if worker_role == "EVIDENCE_COLLECTOR": return ["entity_external_evidence"]
        if worker_role == "PORTFOLIO_ANALYST": return ["entity_model_signals"]
        return []


class _LLM:
    def __init__(self): self.stages=[]
    def generate_json(self, **kwargs):
        stage=kwargs["stage"]; self.stages.append(stage)
        if stage == "upfront_merged_need_worker_planning":
            # 合并规划：Need 生成时直接声明 binding（worker_binding 或 reuse_binding 二选一）
            payload={"needs":[
                {"description":"查询外部证据","required":True,"requirements":[
                    {"semantic_key":"external_evidence","direction":"output","required":True},
                ],"binding":{"worker_binding":{"worker_id":"W01","objective":"查询外部数据","desired_output_data_names":["evidence"]}}},
                {"description":"查询内部模型数据","required":True,"requirements":[
                    {"semantic_key":"entity_model_signals","direction":"output","required":True},
                ],"binding":{"worker_binding":{"worker_id":"W02","objective":"查询内部模型数据","desired_output_data_names":["prediction"]}}},
                {"description":"分析证券","required":True,"requirements":[
                    {"semantic_key":"entity_analysis","direction":"output","required":True},
                ],"binding":{"worker_binding":{"worker_id":"W09","objective":"分析证券","desired_output_data_names":["analysis"]}}},
            ],"selection_reason":"数据查询后由分析Worker读取ContextBundle。"}
        else: raise AssertionError(stage)
        kwargs["validator"](payload); return payload


def _plan():
    llm=_LLM(); planner=CoordinatorPlanner(CapabilityWorkerDirectory(),llm_service=llm,worker_tool_directory=_Tools())
    tasks,meta=planner.plan(query="分析600519",effect_limit="read",request_target={"stock_code":"600519"},session_id="s",run_id="r",user_id="u",focus_refs=[SimpleNamespace(role="focus")],context_refs=[],memory_summary="",request_id="R01",task_id_prefix="R01-")
    return llm,tasks,meta


def test_new_run_uses_only_one_merged_mainagent_llm_planning_stage() -> None:
    llm,tasks,_=_plan()
    assert llm.stages == ["upfront_merged_need_worker_planning"]
    assert [t.worker_id for t in tasks] == ["W01","W02","W09"]


def test_runtime_dependencies_are_execution_order_only() -> None:
    _,tasks,meta=_plan()
    assert tasks[0].dependency_task_ids == [] and tasks[1].dependency_task_ids == []
    assert set(tasks[2].dependency_task_ids) == {"R01-T01","R01-T02"}
    assert meta["business_data_owner"] == "context_bundle_working_memory"
    assert meta["task_dependency_owner"] == "request_task_state"
    assert not hasattr(tasks[2],"resolved_input_bindings")


def test_worker_private_planning_boundary_is_preserved() -> None:
    _,_,meta=_plan()
    assert meta["worker_private_planning_owner"] == "specialist_worker"
    assert meta["main_agent_llm_planning_stages"] == ["upfront_merged_need_worker_planning"]
