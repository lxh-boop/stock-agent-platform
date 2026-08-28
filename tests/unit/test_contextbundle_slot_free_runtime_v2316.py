from __future__ import annotations

import json
from pathlib import Path

from agent.capabilities import CapabilityContractValidator, TaskDependencyCompiler
from agent.collaboration.coordinator import AgentCollaborationCoordinator
from agent.collaboration.models import GraphAgentTask, GraphWorkerResult, ResultStatus
from agent.collaboration.specialist_runtime import SpecialistRuntime
from agent.collaboration.worker_directory import CapabilityWorkerDirectory
from agent.collaboration.workers.entity_analysis import run_entity_analysis
from agent.context.context_types import ContextBundle
from agent.graph.contracts import GraphNodeKind, GraphRef


def _ref(code="600519"):
    return GraphRef(graph_id="financial_graph", node_id=f"cn:security:sse:{code}", node_kind=GraphNodeKind.OBJECT,
                    role="focus", source="test", confidence=1.0, locked=True)


def _contract(*names, mutation=False):
    return [{"contract_id":"C01", "required_data":[], "required_parameters":[],
             "promised_data":[{"name":name,"required_paths":[]} for name in names],
             "acceptance_rule_ids":["schema_valid"], "forbidden_data_names":[], "criticality":"required",
             "mutation_allowed":mutation, "allowed_terminal_states":["completed","business_empty","business_insufficient"]}]


def _task(worker_id, agent, role, names, code="600519"):
    return GraphAgentTask(task_id=f"{worker_id}-T", run_id="run", session_id="s", worker_id=worker_id,
        assigned_agent=agent, objective="test", user_id="u", boundary_id=role, contracts=_contract(*names),
        expected_data_names=list(names), focus_refs=[_ref(code)])


def test_contextbundle_keeps_empty_successful_value_as_existing_data_name():
    bundle = ContextBundle(user_id="u", conversation_id="s", run_id="run")
    bundle.put_business_data(entity_ref=_ref().to_dict(), name="evidence", value=[])
    assert bundle.has_business_data(entity_id=_ref().node_id, name="evidence")
    view = bundle.business_data_context(entity_refs=[_ref().to_dict()])
    assert view["entities"][0]["data"]["evidence"] == []
    assert view["available_names"] == ["evidence"]


def test_contextbundle_carries_unified_artifact_contract_and_provenance():
    bundle = ContextBundle(user_id="u", conversation_id="s", run_id="run")
    bundle.put_business_data(
        entity_ref=_ref().to_dict(),
        name="portfolio_risk",
        value={"level": "low"},
        contract="portfolio.risk",
        version="1.0",
        schema_id="PortfolioRisk.v1",
        provenance={"producer_id": "RISK_ANALYST", "task_id": "T04"},
    )
    view = bundle.business_data_context(entity_refs=[_ref().to_dict()])
    assert view["schema_version"] == "run_context_business_data.v1"
    assert view["entities"][0]["contracts"]["portfolio_risk"] == {
        "contract": "portfolio.risk",
        "version": "1.0",
        "schema_id": "PortfolioRisk.v1",
        "provenance": {"producer_id": "RISK_ANALYST", "task_id": "T04"},
    }


def test_failed_result_is_not_published_to_contextbundle():
    runtime = object.__new__(SpecialistRuntime)
    runtime.context_bundle = ContextBundle(user_id="u", conversation_id="s", run_id="run")
    task = _task("W02", "PORTFOLIO_ANALYST", "system_internal_fact_provider", ["prediction"])
    failed = GraphWorkerResult(task_id=task.task_id, agent_id=task.assigned_agent, status=ResultStatus.FAILED,
        output_type="CapabilityResult", data=None, error={"code":"tool_failed"}, focus_refs=task.focus_refs)
    assert runtime._publish_business_data(task, failed) == []
    assert not runtime.context_bundle.has_business_data(entity_id=_ref().node_id, name="prediction")


def test_provider_reuses_same_run_contextbundle_data_without_query():
    runtime = object.__new__(SpecialistRuntime)
    runtime.directory = CapabilityWorkerDirectory()
    runtime.context_bundle = ContextBundle(user_id="u", conversation_id="s", run_id="run")
    runtime.context_bundle.put_business_data(entity_ref=_ref().to_dict(), name="prediction", value={"rank":229})
    task = _task("W02", "PORTFOLIO_ANALYST", "system_internal_fact_provider", ["prediction"])
    result = runtime._provider_reuse(task)
    assert result is not None
    assert result.metadata["working_memory_reused"] is True
    assert result.data["business_data"]["prediction"]["rank"] == 229


def test_partial_provider_reuse_queries_only_missing_entity():
    runtime = object.__new__(SpecialistRuntime)
    runtime.directory = CapabilityWorkerDirectory()
    runtime.context_bundle = ContextBundle(user_id="u", conversation_id="s", run_id="run")
    a,b=_ref("600519"),_ref("603833")
    runtime.context_bundle.put_business_data(entity_ref=a.to_dict(), name="prediction", value={"rank":229})
    task=_task("W02","PORTFOLIO_ANALYST","system_internal_fact_provider",["prediction"])
    task.focus_refs=[a,b]
    execution=runtime._provider_execution_task(task)
    assert [ref.node_id for ref in execution.focus_refs] == [b.node_id]


def test_w09_reads_contextbundle_without_producer_identity():
    class LLM:
        def generate_text(self, **kwargs):
            text="\n".join(str(x.get("content") or "") for x in kwargs["messages"])
            assert '"prediction"' in text and '"evidence"' in text
            assert "W01" not in text and "W02" not in text
            return json.dumps({"context_sufficient":True,"missing_information":[],
                "facts":[{"claim_id":"F1","statement":"已有事实"}],
                "analysis":[{"claim_id":"A1","statement":"可形成分析"}],
                "uncertainties":[],"conclusion":"完成"}, ensure_ascii=False)
    task=_task("W09","ENTITY_ANALYST","entity_analysis",["analysis","analysis_uncertainty"])
    context={"schema_version":"run_context_business_data.v1","run_id":"run",
             "entities":[{"entity_ref":_ref().to_dict(),"data":{"prediction":{"rank":229},"evidence":[]}}],
             "global_data":{},"available_names":["evidence","prediction"]}
    result=run_entity_analysis(LLM(),task,working_memory_context=context,language="zh")
    assert result.status == ResultStatus.COMPLETED
    assert result.data["business_data"]["analysis"]["facts"]


def test_w09_reports_business_information_gap_not_worker_or_tool():
    class LLM:
        def generate_text(self, **kwargs):
            return json.dumps({"context_sufficient":False,
                "missing_information":["缺少盈利能力相关经营数据"],"facts":[],"analysis":[],"uncertainties":[],
                "conclusion":"当前数据不足"}, ensure_ascii=False)
    task=_task("W09","ENTITY_ANALYST","entity_analysis",["analysis","analysis_uncertainty"])
    result=run_entity_analysis(LLM(),task,working_memory_context={"entities":[],"global_data":{},"available_names":[]},language="zh")
    assert result.status == ResultStatus.NEED_CONTEXT
    assert result.missing_items[0].searched_sources == ["ContextBundle"]
    assert "W01" not in result.missing_items[0].description and "W02" not in result.missing_items[0].description


def test_task_dependencies_order_execution_only_without_data_bindings():
    directory=CapabilityWorkerDirectory()
    compiler=TaskDependencyCompiler(directory)
    from agent.capabilities.models import CapabilityTask, CapabilityContract, DataGuarantee
    def cap(tid,wid,role,name):
        return CapabilityTask(task_id=tid,worker_id=wid,objective="x",boundary_id=role,
            contracts=[CapabilityContract(contract_id=tid+"C",description="x",promised_data=[DataGuarantee(name=name)])])
    tasks=[cap("T1","W02","system_internal_fact_provider","prediction"), cap("T2","W09","entity_analysis","analysis"), cap("T3","W05","state_change_proposal","proposal")]
    deps=compiler.compile(tasks)
    assert deps["T1"] == []
    assert deps["T2"] == ["T1"]
    assert set(deps["T3"]) == {"T1","T2"}


def test_mutation_permission_is_independent_of_proposal_semantics():
    directory=CapabilityWorkerDirectory()
    assert directory.get("W05").can_mutate is False
    assert directory.get("W05").execution_stage == "decision"
    assert directory.get("W08").can_mutate is True
    assert directory.get("W08").execution_stage == "mutation"


def test_empty_value_satisfies_materialized_business_data_contract():
    from agent.capabilities.models import CapabilityContract, DataGuarantee
    contract=CapabilityContract(contract_id="C",description="x",promised_data=[DataGuarantee(name="evidence")])
    report=CapabilityContractValidator().validate(contracts=[contract],produced_data_names={"evidence"},
        materialized_data={"evidence":[]},result_status="completed",result_payload={"business_empty":True})[0]
    assert report.status == "business_empty"
    assert report.missing_outputs == []


def test_bundle_report_task_has_no_business_data_input_binding():
    c=AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    c.directory=CapabilityWorkerDirectory()
    task=c._build_bundle_report_task(run_id="r",session_id="s",user_id="u",objective="汇总")
    assert task.dependency_task_ids == []
    assert task.expected_data_names == ["report","result.user_facing"]
    assert task.contracts[0]["required_data"] == []
    assert not hasattr(task,"resolved_input_bindings")
