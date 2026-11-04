"""Map LangGraph run activity onto the legacy run-recording surfaces.

Keeps ``/runs/{run_id}``, trace and reflection pages functional for the new
engine by writing the same tables the self-built runtime wrote: agent_runs
(via ``AgentRuntimeRecorder``) and console trace events (via
``trace_event``). Fidelity is intentionally reduced: one step per worker task
plus supervisor stages, not per-token/per-stage LLM telemetry.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent_graph.trace import trace_event
from agent_graph.runtime.recorder import AgentRuntimeRecorder
from agent_graph.runtime.recorder import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIALLY_COMPLETED,
    RUN_PLANNING,
    RUN_RUNNING,
    RUN_WAITING_FOR_APPROVAL,
)

from agent_graph.state import WorkerResult

_SUPERVISOR_STEPS = {"decompose": "request_decomposition", "plan": "worker_planning", "synthesize": "result_composition"}


class GraphRunTracer:
    """Record one LangGraph run into agent_runs + console trace."""

    def __init__(
        self,
        *,
        user_id: str,
        goal: str,
        db_path: str | Path | None,
        session_id: str,
        run_id: str | None = None,
    ) -> None:
        self.recorder = AgentRuntimeRecorder(
            user_id=user_id,
            goal=goal,
            db_path=db_path,
            session_id=session_id,
            run_id=run_id,
        )
        self.steps: set[str] = set()
        self.run_id = self.recorder.run_id
        trace_event(
            "agent_graph.run_started",
            {"query": goal, "user_id": user_id, "session_id": session_id},
            run_id=self.run_id,
        )

    # -- lifecycle ---------------------------------------------------------

    def planning(self) -> None:
        self._transition(RUN_PLANNING, "decompose_started")

    def running(self, reason: str = "workers_dispatched") -> None:
        self._transition(RUN_RUNNING, reason)

    def waiting(self) -> None:
        self._transition(RUN_WAITING_FOR_APPROVAL, "approval_interrupted")

    def _transition(self, status: str, reason: str) -> None:
        try:
            self.recorder.transition_run(status, reason)
        except ValueError:
            # The legacy state machine rejects some jumps (e.g. created->running
            # without planning); degrade to an event instead of failing the run.
            trace_event("agent_graph.transition_skipped", {"to": status, "reason": reason}, run_id=self.run_id)

    # -- supervisor stages -------------------------------------------------

    def stage(self, node: str, payload: dict[str, Any] | None = None) -> None:
        intent = _SUPERVISOR_STEPS.get(node, node)
        if intent not in self.steps:
            self.steps.add(intent)
            try:
                self.recorder.create_step(intent, intent=intent, depends_on=[], metadata=dict(payload or {}))
                self.recorder.transition_step(intent, "succeeded", reason=f"{node}_done")
            except Exception:  # noqa: BLE001 - trace must never break the run
                trace_event("agent_graph.step_record_failed", {"node": node}, run_id=self.run_id)
        trace_event(f"agent_graph.node.{node}", dict(payload or {}), run_id=self.run_id)

    # -- workers -----------------------------------------------------------

    def worker_started(self, task: dict[str, Any]) -> None:
        step_id = str(task.get("task_id"))
        if step_id in self.steps:
            return
        self.steps.add(step_id)
        try:
            self.recorder.create_step(
                step_id,
                intent=str(task.get("objective") or ""),
                depends_on=[],
                metadata={"worker_id": task.get("worker_id"), "attempt": task.get("attempt")},
            )
            self.recorder.transition_step(step_id, "running", reason="worker_started")
        except Exception:  # noqa: BLE001
            pass

    def worker_finished(self, result: WorkerResult) -> None:
        step_id = str(result.get("task_id"))
        status = str(result.get("status") or "failed")
        try:
            for call in list(result.get("tool_calls") or []):
                self.recorder.record_tool_call(
                    step_id=step_id,
                    tool_name=str(call.get("tool") or "unknown_tool"),
                    arguments=dict(call.get("args") or {}),
                    result={"preview": str(call.get("preview") or "")[:400], "success": bool(call.get("success", True))},
                )
            self.recorder.transition_step(step_id, "succeeded" if status == "completed" else "failed", reason=f"worker_{status}")
            self.recorder.record_step_result(
                step_id,
                {
                    "success": status == "completed",
                    "answer": str(result.get("answer") or "")[:800],
                    "errors": list(result.get("errors") or []),
                    "status": status,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        trace_event(
            "agent_graph.worker_finished",
            {
                "task_id": step_id,
                "worker_id": result.get("worker_id"),
                "status": status,
                "tool_call_count": len(list(result.get("tool_calls") or [])),
                "duration_ms": result.get("duration_ms"),
            },
            run_id=self.run_id,
        )

    def approval_requested(self, payload: dict[str, Any]) -> None:
        trace_event(
            "agent_graph.approval_requested",
            {"tool_name": payload.get("tool_name"), "gate": payload.get("gate")},
            run_id=self.run_id,
        )

    # -- finalize ----------------------------------------------------------

    def finalize(self, *, success: bool, answer: str, warnings: list[str], errors: list[str], started_at: float) -> str:
        status = RUN_COMPLETED if success else RUN_PARTIALLY_COMPLETED
        try:
            self._transition(status, "run_finished")
        except Exception:  # noqa: BLE001
            status = RUN_FAILED
            self._transition(RUN_FAILED, "run_failed")
        try:
            self.recorder.merge_metadata(
                {
                    "langgraph_runtime": {
                        "engine": "langgraph",
                        "duration_ms": round((time.perf_counter() - started_at) * 1000),
                        "warning_count": len(warnings),
                        "error_count": len(errors),
                    }
                }
            )
        except Exception:  # noqa: BLE001
            pass
        trace_event(
            "agent_graph.run_finished",
            {"success": success, "status": status, "errors": errors[:5]},
            run_id=self.run_id,
        )
        return self.run_id


__all__ = ["GraphRunTracer"]
