from __future__ import annotations

from types import MethodType

from agent.collaboration.coordinator import AgentCollaborationCoordinator
from agent.collaboration.models import GraphWorkerResult, ResultStatus
from agent.collaboration.presentation_policy import PresentationPolicyResolver, PresentationValidator
from agent.collaboration.request_bundle import (
    PresentationRequest, RequestBundle, RequestCategory, RequestItem, RequestStatus,
)


def test_request_coverage_keeps_independent_statuses_in_source_order() -> None:
    bundle = RequestBundle(raw_message="", requests=[
        RequestItem("R01", 1, RequestCategory.BUSINESS, "分析贵州茅台"),
        RequestItem("R02", 2, RequestCategory.BUSINESS, "订机票", status=RequestStatus.UNSUPPORTED),
        RequestItem("R03", 3, RequestCategory.PRESENTATION, "英文回答"),
    ])
    coverage = AgentCollaborationCoordinator._request_coverage(bundle, {
        "R01": {"status": "completed"}, "R02": {"status": "unsupported", "reason": "no_matching_capability"},
        "R03": {"status": "presentation_applied"},
    })
    assert [row["request_id"] for row in coverage["requests"]] == ["R01", "R02", "R03"]
    assert coverage["counts_by_status"] == {"completed": 1, "unsupported": 1, "presentation_applied": 1}
    assert coverage["all_terminal"] is True


def test_request_coverage_keeps_user_input_tool_failure_and_business_empty_distinct() -> None:
    bundle = RequestBundle(raw_message="", requests=[
        RequestItem("R01", 1, RequestCategory.BUSINESS, "完整请求"),
        RequestItem("R02", 2, RequestCategory.BUSINESS, "缺用户参数"),
        RequestItem("R03", 3, RequestCategory.BUSINESS, "工具超时"),
        RequestItem("R04", 4, RequestCategory.BUSINESS, "业务空结果"),
    ])
    coverage = AgentCollaborationCoordinator._request_coverage(bundle, {
        "R01": {"status": "completed"}, "R02": {"status": "waiting_user_input"},
        "R03": {"status": "tool_failed"}, "R04": {"status": "business_empty"},
    })
    assert coverage["counts_by_status"]["waiting_user_input"] == 1
    assert coverage["counts_by_status"]["tool_failed"] == 1
    assert coverage["counts_by_status"]["business_empty"] == 1


def test_bundle_final_report_task_reads_request_aggregate_directly_not_business_data() -> None:
    coordinator = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    class _Directory:
        class _Card:
            agent_id="REPORT_WRITER"; role="result_composition"; execution_mode="pure_llm"
        def get(self, worker_id):
            assert worker_id == "W06"; return self._Card()
    coordinator.directory = _Directory()
    task = coordinator._build_bundle_report_task(run_id="r", session_id="s", user_id="u", objective="汇总本轮Request")
    assert task.contracts[0]["required_data"] == []
    assert task.expected_data_names == ["report", "result.user_facing"]
    assert task.dependency_task_ids == []
    assert task.metadata["bundle_report"] is True


def test_request_result_classifier_distinguishes_failure_classes() -> None:
    classify = AgentCollaborationCoordinator._classify_request_result
    assert classify({"execution_status":"waiting_context","worker_results":{"T":{"completion":{"failure_kind":"user_input_required","business_status":"unknown"},"error":{"error_id":"user_input_required"}}}}) == RequestStatus.WAITING_USER_INPUT
    assert classify({"worker_results":{"T":{"completion":{"failure_kind":"tool_execution_failure","business_status":"unknown"},"error":{"error_id":"tool_execution_failure"}}}}) == RequestStatus.TOOL_FAILED
    assert classify({"worker_results":{"T":{"completion":{"failure_kind":"none","business_status":"business_empty"}}}}) == RequestStatus.BUSINESS_EMPTY


def test_bundle_execute_keeps_parent_run_and_dependency_is_order_only(tmp_path) -> None:
    coordinator = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    coordinator.output_dir=tmp_path; coordinator.db_path=None; coordinator.runtime_services=None
    coordinator.store=type("_Store",(),{"graph_id":"financial_graph"})()
    bundle = RequestBundle(raw_message="分析A，然后基于结果分析B，用英文回答", requests=[
        RequestItem("R01",1,RequestCategory.BUSINESS,"分析A"),
        RequestItem("R02",2,RequestCategory.BUSINESS,"基于R01结果分析B",depends_on=["R01"]),
        RequestItem("R03",3,RequestCategory.PRESENTATION,"用英文回答",presentation=PresentationRequest(language="en",scope="whole_bundle")),
    ])
    class _Decomposer:
        def decompose(self, **kwargs): return bundle
    class _Session:
        def build_summary(self, session_id, limit=40): return ""
        def get(self, session_id, key): return None
        def put(self, **kwargs): return None
    class _Checkpoints:
        def __init__(self): self.saved=[]
        def save(self,v): self.saved.append(v); return v
    class _Directory:
        class _Card:
            agent_id="REPORT_WRITER"; role="result_composition"; execution_mode="pure_llm"
        def get(self, worker_id): assert worker_id=="W06"; return self._Card()
    coordinator.request_decomposer=_Decomposer(); coordinator.session_state=_Session(); coordinator.checkpoints=_Checkpoints()
    coordinator.presentation_policy_resolver=PresentationPolicyResolver(); coordinator.presentation_validator=PresentationValidator(); coordinator.directory=_Directory()
    calls=[]
    def _business(self, **kwargs):
        calls.append(kwargs)
        return {"success":True,"execution_status":"completed","worker_results":{},"graph_runtime":{"worker_dag":{"tasks":[]}},"need_completion":{"goal_status":"completed"},"context_sufficiency":{"status":"sufficient"},"warnings":[],"errors":[],"replan_count":0,"agent_timeline":[],"execution_batches":[],"session_mutation_proposal":{"operations":[]}}
    def _materialize(self, *, request, result, run_id):
        return {"request_id":request.request_id,"status":"completed","business_data":{"analysis":{"from":request.request_id}}}
    def _run_dag(self, tasks, **kwargs):
        task=tasks[0]
        data={"content":"- R01 completed\n- R02 completed","business_data":{"report":"- R01 completed\n- R02 completed","result.user_facing":"- R01 completed\n- R02 completed"},"produced_data_names":["report","result.user_facing"]}
        result=GraphWorkerResult(task_id=task.task_id,agent_id=task.assigned_agent,status=ResultStatus.COMPLETED,output_type="FinalReport",data=data,summary="ok")
        return {task.task_id:result},[{"batch_index":1,"task_ids":[task.task_id]}],[{"task_id":task.task_id,"status":"completed"}]
    coordinator._execute_read_request=MethodType(_business,coordinator); coordinator._materialize_request_payload=MethodType(_materialize,coordinator); coordinator._run_dag=MethodType(_run_dag,coordinator)
    result=coordinator.execute(query=bundle.raw_message,decomposition={},user_id="u",default_top_k=5,session_id="s",run_id="parent-run",language="zh",execution_context={})
    assert [call["request_id"] for call in calls] == ["R01","R02"]
    assert all(call["run_id"] == "parent-run" for call in calls)
    assert calls[1]["execution_context"]["dependency_request_ids"] == ["R01"]
    assert "request_dependency_results" not in calls[1]["execution_context"]
    assert result["request_coverage"]["counts_by_status"]["completed"] == 2
    assert result["presentation_policy"]["language"] == "en"
