from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent.collaboration.models import GraphWorkerResult, ResultStatus
from agent.collaboration.planner import CoordinatorPlanner
from agent.collaboration.request_bundle import (
    RequestBundleError,
    RequestDecomposer,
    explicit_request_boundaries,
)
from agent.context.context_types import ContextBundle
from agent.context.run_context_store import InMemoryRunContextStore
from agent.runtime_state.run_checkpoint_store import RunCheckpoint


class _FakeLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        validator = kwargs.get("validator")
        if validator is not None:
            validator(self.payload)
        return self.payload


def _binding(*, scope: str = "explicit_entities", inherit: bool = False, ref_type: str = "security") -> dict:
    return {
        "entity_scope": scope,
        "inherit_previous_focus": inherit,
        "reference_entity_type": ref_type,
        "reason": "",
        "freshness_expectation": "unspecified",
        "reuse_reference": [],
    }


def _business_request(
    *,
    display_text: str = "贵州茅台",
    source: str = "explicit",
    status: str = "identified",
    entity_source: str = "explicit",
) -> dict:
    return {
        "source_index": 1,
        "category": "business",
        "objective": "分析目标证券风险",
        "request_type": "read",
        "proposal_required": False,
        "semantic_target": {
            "entity_type": "security",
            "display_text": display_text,
            "source": source,
            "status": status,
        },
        "semantic_entities": (
            [{
                "text": display_text,
                "entity_type": "security",
                "role": "focus",
                "source": entity_source,
            }]
            if display_text else []
        ),
        "constraints": [],
        "depends_on": [],
        "scope": "current_turn",
        "status": "pending",
        "reason": "",
        "action_type": "",
        "presentation": None,
        "context_binding": _binding(
            scope="conversation_focus" if source == "conversation_context" else "explicit_entities",
            inherit=source == "conversation_context",
        ),
    }


class RequestSemanticCutoverTests(unittest.TestCase):
    def test_explicit_boundaries_do_not_duplicate_user_text(self):
        result = explicit_request_boundaries("1. 分析贵州茅台\n2. 分析五粮液")
        self.assertEqual(result["boundary_count"], 2)
        self.assertEqual(result["source_indexes"], [1, 2])
        self.assertNotIn("text", result)

    def test_llm_can_emit_conversation_semantic_entity_but_not_authority(self):
        fake = _FakeLLM({
            "requests": [
                _business_request(
                    display_text="贵州茅台",
                    source="conversation_context",
                    entity_source="conversation_context",
                )
            ]
        })
        bundle = RequestDecomposer(llm_service=fake).decompose(
            query="那它最近风险怎么样？",
            memory_summary="上一轮明确分析对象是贵州茅台。",
            execution_context={},
            language="zh",
            run_id="RUN1",
            reuse_candidates={"items": []},
        )
        request = bundle.requests[0]
        self.assertEqual(request.semantic_target.display_text, "贵州茅台")
        self.assertEqual(request.semantic_target.source, "conversation_context")
        self.assertEqual(request.semantic_entities[0].source, "conversation_context")
        as_dict = request.to_dict()
        self.assertNotIn("target", as_dict)
        self.assertNotIn("mentions", as_dict)
        self.assertEqual(bundle.schema_version, "request_bundle.v3")

        user_payload = json.loads(fake.calls[0]["messages"][1]["content"])
        self.assertIn("conversation_context", user_payload)
        self.assertIn("session_summary", user_payload["conversation_context"])
        self.assertIn("reuse_candidates", user_payload["conversation_context"])
        self.assertIn("explicit_request_boundaries", user_payload)
        self.assertNotIn("deterministic_segments", user_payload)

    def test_missing_target_is_explicit_not_empty_dict(self):
        fake = _FakeLLM({
            "requests": [
                _business_request(display_text="", source="none", status="missing")
            ]
        })
        bundle = RequestDecomposer(llm_service=fake).decompose(
            query="帮我分析一下股票风险",
            memory_summary="",
            execution_context={},
            language="zh",
            run_id="RUN2",
            reuse_candidates={"items": []},
        )
        target = bundle.requests[0].semantic_target
        self.assertEqual(target.status, "missing")
        self.assertEqual(target.source, "none")
        self.assertEqual(target.display_text, "")

    def test_old_target_or_mentions_contract_is_rejected(self):
        payload = _business_request()
        payload["target"] = {"business_object": "贵州茅台"}
        fake = _FakeLLM({"requests": [payload]})
        with self.assertRaisesRegex(RequestBundleError, "legacy_request_semantic_fields_forbidden"):
            RequestDecomposer(llm_service=fake).decompose(
                query="分析贵州茅台",
                memory_summary="",
                execution_context={},
                language="zh",
                run_id="RUN3",
                reuse_candidates={"items": []},
            )


class RunContextStoreCutoverTests(unittest.TestCase):
    def test_context_bundle_uses_store_and_snapshot_restore(self):
        bundle = ContextBundle(user_id="u", conversation_id="s", run_id="r")
        self.assertFalse(hasattr(bundle, "business_data"))
        bundle.put_business_data(
            entity_ref={"node_id": "sec:600519.SH", "name": "贵州茅台"},
            name="prediction",
            value={"score": 0.88},
            provenance={"producer_type": "worker", "producer_id": "W01"},
        )
        self.assertTrue(bundle.has_business_data(entity_id="sec:600519.SH", name="prediction"))
        snapshot = bundle.snapshot_working_memory()

        restored = ContextBundle(user_id="u", conversation_id="s", run_id="r")
        restored.restore_working_memory(snapshot)
        view = restored.business_data_context(entity_refs=[{"node_id": "sec:600519.SH", "name": "贵州茅台"}])
        self.assertEqual(view["schema_version"], "run_context_business_data.v1")
        self.assertEqual(view["entities"][0]["data"]["prediction"]["score"], 0.88)
        self.assertIn("run_context_store", restored.to_dict())

    def test_store_snapshot_isolation(self):
        store = InMemoryRunContextStore()
        store.put({"entity_id": "__run__", "name": "ranking", "value": [1, 2, 3]})
        snap = store.snapshot()
        snap["items"][0]["value"].append(4)
        self.assertEqual(store.items()[0]["value"], [1, 2, 3])

    def test_checkpoint_can_persist_working_memory_snapshot(self):
        cp = RunCheckpoint(
            run_id="r",
            session_id="s",
            user_id="u",
            status="running",
            working_memory_snapshot={"schema_version": "run_context_store_snapshot.v1", "items": []},
        )
        self.assertEqual(cp.to_dict()["working_memory_snapshot"]["schema_version"], "run_context_store_snapshot.v1")


class WorkerResultCutoverTests(unittest.TestCase):
    def test_worker_result_has_handoff_and_audit_views_only(self):
        result = GraphWorkerResult(
            task_id="T01",
            agent_id="W01",
            status=ResultStatus.COMPLETED,
            output_type="CapabilityResult",
            data={"business_data": {"prediction": {"score": 0.9}}},
            payload={"business_data": {"prediction": {"score": 0.9}}},
            summary="ok",
            confidence=0.9,
            metadata={"attempt": 1, "duration_ms": 12.5, "private": "hidden"},
        )
        handoff = result.handoff_view()
        audit = result.audit_view()
        self.assertEqual(handoff["task_id"], "T01")
        self.assertEqual(audit["worker_id"], "W01")
        self.assertNotIn("private", handoff["metadata"])
        self.assertFalse(hasattr(result, "safe_for_coordinator"))

    def test_old_upfront_planner_paths_are_deleted(self):
        self.assertFalse(hasattr(CoordinatorPlanner, "_plan_request_need_contract"))
        self.assertFalse(hasattr(CoordinatorPlanner, "_select_worker_calls"))
        self.assertTrue(hasattr(CoordinatorPlanner, "_select_recovery_worker_calls"))


class ActiveArchitectureCutoverTests(unittest.TestCase):
    def test_active_chain_contains_no_deleted_contract_symbols(self):
        root = Path(__file__).resolve().parents[2]
        files = [
            root / "agent" / "executor.py",
            root / "agent" / "collaboration" / "coordinator.py",
            root / "agent" / "collaboration" / "planner.py",
            root / "agent" / "context" / "context_builder.py",
            root / "agent" / "console_trace.py",
        ]
        forbidden = [
            '"task_results"',
            '"graph_worker_results"',
            '"agent_outputs"',
            "deterministic_segments",
            "upfront_request_need_planning",
            "upfront_worker_call_selection",
            "planning_gap_worker_call_repair",
            "context_bundle_business_data.v2",
            "context_bundle_working_memory",
        ]
        failures: list[str] = []
        for path in files:
            text = path.read_text(encoding="utf-8")
            for symbol in forbidden:
                if symbol in text:
                    failures.append(f"{path.relative_to(root)} contains {symbol}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
