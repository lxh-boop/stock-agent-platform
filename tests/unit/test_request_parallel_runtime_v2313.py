from __future__ import annotations

import threading
import time
from types import MethodType

from agent.collaboration.coordinator import AgentCollaborationCoordinator
from agent.collaboration.presentation_policy import PresentationPolicyResolver, PresentationValidator
from agent.collaboration.request_bundle import RequestBundle, RequestCategory, RequestItem, RequestType
from agent.collaboration.request_parallel import BatchSessionMutationCommitter, SessionMutationProposal, SharedRunContext
from agent.runtime_state import RuntimeResourceBudget


class _Outcome:
    changed = True
    conflict = False


class _Session:
    def __init__(self):
        self.values = {}
        self.puts = []

    def build_summary(self, session_id, limit=40):
        return ""

    def get(self, session_id, key):
        return None

    def put(self, **kwargs):
        self.puts.append(dict(kwargs))
        self.values[kwargs["key"]] = kwargs["value"]
        return _Outcome()


class _Checkpoints:
    def save(self, value):
        return value

    def save_request(self, value):
        return value


class _Directory:
    class _Card:
        agent_id = "REPORT_WRITER"
        role = "result_composition"
        execution_mode = "pure_llm"

    def get(self, worker_id):
        return self._Card()

    def list(self):
        return []


class _Decomposer:
    def __init__(self, bundle): self.bundle = bundle
    def decompose(self, **kwargs): return self.bundle


def _coordinator(tmp_path, bundle):
    c = AgentCollaborationCoordinator.__new__(AgentCollaborationCoordinator)
    c.output_dir = tmp_path
    c.db_path = None
    c.runtime_services = None
    c.store = type("_Store", (), {"graph_id": "financial_graph"})()
    c.request_decomposer = _Decomposer(bundle)
    c.session_state = _Session()
    c.checkpoints = _Checkpoints()
    c.presentation_policy_resolver = PresentationPolicyResolver()
    c.presentation_validator = PresentationValidator()
    c.directory = _Directory()
    c.resource_budget = RuntimeResourceBudget(
        max_parallel_requests=2, max_parallel_workers=2,
        max_parallel_tools=2, max_parallel_llm=2,
    )
    c.session_mutation_committer = BatchSessionMutationCommitter(c.session_state)
    c.planner = None
    return c


def _stub_final(c):
    from agent.collaboration.models import GraphWorkerResult, ResultStatus
    def _run(self, tasks, **kwargs):
        task = tasks[0]
        result = GraphWorkerResult(
            task_id=task.task_id, agent_id=task.assigned_agent,
            status=ResultStatus.COMPLETED, output_type="FinalReport",
            data={"content": "ok", "slots": {"user_facing_report": "ok"},
                  "produced_information_slots": ["user_facing_report"]},
            summary="ok",
        )
        return {task.task_id: result}, [], [{"task_id": task.task_id, "status": "completed"}]
    c._run_dag = MethodType(_run, c)


def test_two_independent_read_requests_overlap(tmp_path):
    bundle = RequestBundle(raw_message="A;B", requests=[
        RequestItem("R01", 1, RequestCategory.BUSINESS, "A"),
        RequestItem("R02", 2, RequestCategory.BUSINESS, "B"),
    ])
    c = _coordinator(tmp_path, bundle)
    _stub_final(c)
    lock = threading.Lock(); active = 0; peak = 0; windows = {}

    def _business(self, **kwargs):
        nonlocal active, peak
        rid = kwargs["request_id"]
        started = time.perf_counter()
        with lock:
            active += 1; peak = max(peak, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        windows[rid] = (started, time.perf_counter())
        return {"success": True, "execution_status": "completed", "worker_results": {},
                "graph_runtime": {"worker_dag": {"tasks": []}}, "need_completion": {},
                "warnings": [], "errors": [], "replan_count": 0, "agent_timeline": [],
                "execution_batches": [], "session_mutation_proposal": {"operations": []}}

    c._execute_read_request = MethodType(_business, c)
    c._materialize_request_payload = MethodType(lambda self, **kwargs: {"request_id": kwargs["request"].request_id, "business_data": {}}, c)
    result = c.execute(query="A;B", decomposition={}, user_id="u", default_top_k=5,
                       session_id="s", run_id="run", language="zh", execution_context={})
    assert peak == 2
    assert windows["R01"][0] < windows["R02"][1] and windows["R02"][0] < windows["R01"][1]
    assert result["request_execution_batches"][0]["execution_mode"] == "ready_batch_parallel_business"


def test_dependency_request_starts_after_parent_finishes(tmp_path):
    bundle = RequestBundle(raw_message="A then B", requests=[
        RequestItem("R01", 1, RequestCategory.BUSINESS, "A"),
        RequestItem("R02", 2, RequestCategory.BUSINESS, "B", depends_on=["R01"]),
    ])
    c = _coordinator(tmp_path, bundle); _stub_final(c)
    times = {}
    def _business(self, **kwargs):
        rid=kwargs["request_id"]; times[rid+"_start"]=time.perf_counter(); time.sleep(0.03); times[rid+"_end"]=time.perf_counter()
        return {"success": True, "execution_status": "completed", "worker_results": {},
                "graph_runtime": {"worker_dag": {"tasks": []}}, "need_completion": {},
                "warnings": [], "errors": [], "replan_count": 0, "agent_timeline": [],
                "execution_batches": [], "session_mutation_proposal": {"operations": []}}
    c._execute_read_request=MethodType(_business,c)
    c._materialize_request_payload=MethodType(lambda self, **kwargs: {"request_id": kwargs["request"].request_id, "business_data": {}}, c)
    c.execute(query="A then B", decomposition={}, user_id="u", default_top_k=5, session_id="s", run_id="run", language="zh", execution_context={})
    assert times["R02_start"] >= times["R01_end"]


def test_focus_commit_uses_source_order_not_completion_order():
    session=_Session(); committer=BatchSessionMutationCommitter(session)
    p1=SessionMutationProposal("R01",1); p2=SessionMutationProposal("R02",2)
    common=dict(session_id="s", value_type="graph_ref_list", summary="x", source_type="test", source_ref="r", confirmed=True, confidence=1.0)
    p2.add_put(key="typed_graph_focus:security", value=[{"node_id":"B"}], **common)
    p1.add_put(key="typed_graph_focus:security", value=[{"node_id":"A"}], **common)
    result=committer.commit([p2,p1])
    assert session.values["typed_graph_focus:security"] == [{"node_id":"B"}]
    assert result["conflicts"][0]["resolution"] == "source_order_last_wins"


def test_shared_run_context_returns_isolated_request_copy():
    shared=SharedRunContext(user_id="u",session_id="s",run_id="r", user_profile_snapshot={"risk":{"level":"mid"}})
    a=shared.for_request(); b=shared.for_request(); a["user_profile_snapshot"]["risk"]["level"]="high"
    assert b["user_profile_snapshot"]["risk"]["level"] == "mid"
    assert shared.for_request()["user_profile_snapshot"]["risk"]["level"] == "mid"


def test_resource_budget_caps_llm_peak():
    budget=RuntimeResourceBudget(max_parallel_requests=4,max_parallel_workers=4,max_parallel_tools=4,max_parallel_llm=2)
    peak=0; lock=threading.Lock()
    def job():
        nonlocal peak
        with budget.llm_slot():
            with lock: peak=max(peak,budget.llm_gate.active)
            time.sleep(0.03)
    threads=[threading.Thread(target=job) for _ in range(6)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert peak == 2
    assert budget.snapshot()["llm"]["peak"] == 2


def test_non_mutating_proposal_requests_can_parallelize(tmp_path):
    bundle = RequestBundle(raw_message="P1;P2", requests=[
        RequestItem("R01",1,RequestCategory.BUSINESS,"P1",request_type=RequestType.READ,proposal_required=True),
        RequestItem("R02",2,RequestCategory.BUSINESS,"P2",request_type=RequestType.READ,proposal_required=True),
    ])
    c=_coordinator(tmp_path,bundle); _stub_final(c)
    active=0; peak=0; lock=threading.Lock()
    def _business(self, **kwargs):
        nonlocal active, peak
        with lock: active+=1; peak=max(peak,active)
        time.sleep(0.03)
        with lock: active-=1
        return {"success":True,"execution_status":"completed","worker_results":{},"graph_runtime":{"worker_dag":{"tasks":[]}},"need_completion":{},"warnings":[],"errors":[],"replan_count":0,"agent_timeline":[],"execution_batches":[],"session_mutation_proposal":{"operations":[]}}
    c._execute_read_request=MethodType(_business,c)
    c._materialize_request_payload=MethodType(lambda self, **kwargs:{"request_id":kwargs["request"].request_id,"slots":{}},c)
    result=c.execute(query="P1;P2",decomposition={},user_id="u",default_top_k=5,session_id="s",run_id="r",language="zh",execution_context={})
    assert peak == 2
    assert [row["execution_mode"] for row in result["request_execution_batches"]] == ["ready_batch_parallel_business"]


def test_failed_parallel_request_does_not_cancel_independent_sibling(tmp_path):
    bundle=RequestBundle(raw_message="A;B",requests=[
        RequestItem("R01",1,RequestCategory.BUSINESS,"A"),RequestItem("R02",2,RequestCategory.BUSINESS,"B")])
    c=_coordinator(tmp_path,bundle); _stub_final(c)
    def _business(self, **kwargs):
        if kwargs["request_id"] == "R01":
            raise RuntimeError("tool boom")
        return {"success":True,"execution_status":"completed","worker_results":{},"graph_runtime":{"worker_dag":{"tasks":[]}},"need_completion":{},"warnings":[],"errors":[],"replan_count":0,"agent_timeline":[],"execution_batches":[],"session_mutation_proposal":{"operations":[]}}
    c._execute_read_request=MethodType(_business,c)
    c._materialize_request_payload=MethodType(lambda self, **kwargs:{"request_id":kwargs["request"].request_id,"slots":{}},c)
    result=c.execute(query="A;B",decomposition={},user_id="u",default_top_k=5,session_id="s",run_id="r",language="zh",execution_context={})
    assert result["request_results"]["R01"]["status"] == "failed"
    assert result["request_results"]["R02"]["status"] == "completed"
