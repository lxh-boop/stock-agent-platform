from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent.runtime import (
    AgentRuntimeRecorder,
    STEP_FAILED,
    STEP_PENDING,
    STEP_READY,
    STEP_RUNNING,
    STEP_SKIPPED,
    STEP_SUCCEEDED,
    now_text,
)

from .models import GraphAgentTask, GraphWorkerResult, ResultStatus


_TERMINAL_STEP_STATUS: dict[ResultStatus, str] = {
    ResultStatus.COMPLETED: STEP_SUCCEEDED,
    ResultStatus.PARTIAL: STEP_SUCCEEDED,
    ResultStatus.PROPOSAL_READY: STEP_SUCCEEDED,
    ResultStatus.WAITING_APPROVAL: STEP_SUCCEEDED,
    ResultStatus.NEED_CONTEXT: STEP_SKIPPED,
    ResultStatus.BLOCKED: STEP_SKIPPED,
    ResultStatus.NOT_EXECUTED: STEP_SKIPPED,
    ResultStatus.FAILED: STEP_FAILED,
}


@dataclass
class CollaborationRuntimeServices:
    """Run-scoped persistence services shared by coordinator and Workers.

    The object is intentionally small.  It does not expose repository details to
    the planner or Worker LLM and it never changes task routing.  Its only job in
    phase 1 is to persist the already validated Worker DAG lifecycle.
    """

    recorder: AgentRuntimeRecorder
    run_id: str
    user_id: str
    session_id: str
    strict: bool = True
    _started_monotonic: dict[str, float] = field(default_factory=dict, init=False)
    _started_at: dict[str, str] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    @classmethod
    def from_recorder(
        cls,
        recorder: AgentRuntimeRecorder,
        *,
        user_id: str,
        session_id: str,
        strict: bool = True,
    ) -> "CollaborationRuntimeServices":
        return cls(
            recorder=recorder,
            run_id=recorder.run_id,
            user_id=str(user_id or recorder.user_id),
            session_id=str(session_id or recorder.session_id),
            strict=bool(strict),
        )

    def validate_identity(self, *, run_id: str, user_id: str, session_id: str) -> None:
        mismatches: list[str] = []
        if str(run_id or "") != self.run_id:
            mismatches.append("run_id")
        if str(user_id or "") != self.user_id:
            mismatches.append("user_id")
        if str(session_id or "") != self.session_id:
            mismatches.append("session_id")
        if mismatches:
            raise ValueError(
                "collaboration_runtime_identity_mismatch:" + ",".join(mismatches)
            )

    @staticmethod
    def _ref_summary(task: GraphAgentTask) -> dict[str, Any]:
        return {
            "focus_refs": [
                {
                    "node_id": ref.node_id,
                    "node_kind": ref.node_kind.value,
                    "role": ref.role,
                    "locked": ref.locked,
                }
                for ref in task.focus_refs[:20]
            ],
            "context_refs": [
                {
                    "node_id": ref.node_id,
                    "node_kind": ref.node_kind.value,
                    "role": ref.role,
                    "locked": ref.locked,
                }
                for ref in task.context_refs[:20]
            ],
        }

    def register_tasks(self, tasks: list[GraphAgentTask]) -> None:
        with self._lock:
            for task in tasks:
                if task.run_id != self.run_id:
                    raise ValueError(
                        f"worker_task_run_id_mismatch:{task.task_id}:{task.run_id}"
                    )
                initial_status = STEP_READY if not task.dependency_task_ids else STEP_PENDING
                self.recorder.create_step(
                    task.task_id,
                    task.boundary_id,
                    depends_on=list(task.dependency_task_ids),
                    status=initial_status,
                    metadata={
                        "runtime_layer": "capability_dag",
                        "agent_role": task.assigned_agent,
                        "assigned_agent": task.assigned_agent,
                        "boundary_id": task.boundary_id,
                        "task_contract_version": task.contract_version,
                        "required_outputs": list(task.expected_data_names),
                                                "priority": task.priority,
                        "attempt": task.attempt,
                        "effect_limit": task.metadata.get("effect_limit") or task.effect_limit,
                        "dependency_derivation": "execution_order_only",
                        **self._ref_summary(task),
                    },
                )

    def mark_ready(self, task: GraphAgentTask) -> None:
        with self._lock:
            self.recorder.transition_step(
                task.task_id,
                STEP_READY,
                reason="worker_dependencies_satisfied",
                metadata={"worker_task_status": "ready"},
            )

    def mark_running(self, task: GraphAgentTask) -> None:
        with self._lock:
            started_at = now_text()
            self._started_at[task.task_id] = started_at
            self._started_monotonic[task.task_id] = time.perf_counter()
            self.recorder.transition_step(
                task.task_id,
                STEP_RUNNING,
                reason="worker_dispatched",
                metadata={
                    "worker_task_status": "running",
                    "attempt": task.attempt,
                },
            )

    def record_result(
        self,
        task: GraphAgentTask,
        result: GraphWorkerResult,
    ) -> None:
        with self._lock:
            finished_at = now_text()
            started_at = self._started_at.pop(task.task_id, "")
            started_monotonic = self._started_monotonic.pop(task.task_id, None)
            duration_seconds = (
                max(0.0, time.perf_counter() - started_monotonic)
                if started_monotonic is not None
                else max(0.0, float(result.metadata.get("duration_ms") or 0.0) / 1000.0)
            )
            step_status = _TERMINAL_STEP_STATUS.get(result.status, STEP_FAILED)
            audit = result.audit_view()
            self.recorder.record_step_result(
                task.task_id,
                {
                    "success": step_status == STEP_SUCCEEDED,
                    "step_status": step_status,
                    "intent": task.boundary_id,
                    "depends_on": list(task.dependency_task_ids),
                    "message": result.summary,
                    "warnings": list(result.warnings),
                    "errors": list(result.warnings) if result.status == ResultStatus.FAILED else [],
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_seconds": duration_seconds,
                    "execution_mode": task.execution_mode,
                    "agent_role": task.assigned_agent,
                    "agent_input_summary": {
                        "objective": task.objective[:500],
                        "required_outputs": list(task.expected_data_names),
                        "dependency_task_ids": list(task.dependency_task_ids),
                    },
                    "agent_output_summary": {
                        "worker_result_status": audit["status"],
                        "confidence": audit["confidence"],
                        "warning_count": audit["warning_count"],
                        "missing_context_count": audit["missing_context_count"],
                        "evidence_ref_count": audit["evidence_ref_count"],
                        "artifact_ref_count": audit["artifact_ref_count"],
                    },
                    "metadata": {
                        "runtime_layer": "capability_dag",
                        "worker_result_status": result.status.value,
                        "worker_contract_version": result.contract_version,
                        "tool_execution": (
                            dict(result.metadata.get("tool_execution") or {})
                            if isinstance(result.metadata.get("tool_execution"), dict)
                            else {}
                        ),
                        "confidence": result.confidence,
                        "missing_context_keys": [item.key for item in result.missing_items],
                        "artifact_refs": list(result.artifact_refs[:20]),
                        "evidence_refs": [ref.to_dict() for ref in result.evidence_refs[:30]],
                        "graph_path_ref_count": len(result.graph_path_refs),
                        "graph_patch_ref": result.graph_patch_ref,
                        "attempt": task.attempt,
                    },
                },
            )


__all__ = ["CollaborationRuntimeServices"]
