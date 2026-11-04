"""Map LangGraph run activity onto the legacy run-recording surfaces.

Keeps ``/runs/{run_id}`` functional for the single-agent engine by writing
the same tables: agent_runs (via ``AgentRuntimeRecorder``) plus one step for
the agent loop and one persisted row per tool call. Fidelity is intentionally
reduced: no per-token or per-stage LLM telemetry.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agent_graph.runtime.recorder import (
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PLANNING,
    RUN_RUNNING,
    RUN_WAITING_FOR_APPROVAL,
    AgentRuntimeRecorder,
)
from agent_graph.messages_utils import json_view
from agent_graph.trace import trace_event


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
        self._transition(RUN_PLANNING, "run_started")

    def running(self, reason: str = "agent_started") -> None:
        self._transition(RUN_RUNNING, reason)

    def waiting(self) -> None:
        self._transition(RUN_WAITING_FOR_APPROVAL, "approval_interrupted")

    def _transition(self, status: str, reason: str) -> None:
        try:
            self.recorder.transition_run(status, reason)
        except ValueError:
            trace_event("agent_graph.transition_skipped", {"to": status, "reason": reason}, run_id=self.run_id)

    # -- agent loop --------------------------------------------------------

    def agent_loop_step(self) -> None:
        step_id = "agent_loop"
        if step_id in self.steps:
            return
        self.steps.add(step_id)
        try:
            self.recorder.create_step(step_id, intent="main agent tool loop", depends_on=[])
            self.recorder.transition_step(step_id, "running", reason="agent_started")
        except Exception:  # noqa: BLE001 - trace must never break the run
            trace_event("agent_graph.step_record_failed", {"step": step_id}, run_id=self.run_id)

    def record_tool_trace(self, tool_trace: list[dict[str, Any]]) -> None:
        step_id = "agent_loop"
        for call in list(tool_trace or []):
            args = dict(call.get("args") or {})
            try:
                self.recorder.record_tool_call(
                    step_id=step_id,
                    tool_name=str(call.get("tool") or "unknown_tool"),
                    arguments=args,
                    result={"success": True, "preview": json_view(args, 400)},
                )
            except Exception:  # noqa: BLE001
                pass
        trace_event(
            "agent_graph.agent_finished",
            {"tool_call_count": len(list(tool_trace or []))},
            run_id=self.run_id,
        )

    def finish_agent_step(self, success: bool) -> None:
        try:
            self.recorder.transition_step("agent_loop", "succeeded" if success else "failed", reason="agent_finished")
        except Exception:  # noqa: BLE001
            pass

    def approval_requested(self, payload: dict[str, Any]) -> None:
        trace_event(
            "agent_graph.approval_requested",
            {"tool_name": payload.get("tool_name"), "gate": payload.get("gate")},
            run_id=self.run_id,
        )

    # -- finalize ----------------------------------------------------------

    def finalize(self, *, success: bool, answer: str, warnings: list[str], errors: list[str], started_at: float) -> str:
        status = RUN_COMPLETED if success else RUN_FAILED
        try:
            self._transition(status, "run_finished")
        except Exception:  # noqa: BLE001
            pass
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
