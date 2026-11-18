"""Durable dispatch and execution of one explicitly reviewed Agent interrupt.

The approval row is the idempotency boundary. Short advisory locks protect
dispatch/claim transitions; model calls never hold the approval lock.
An uncertain crash after invocation begins is never silently replayed.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from agent_graph.approval_store import ApprovalStore
from core.user_scope import current_owner, workspace

_ACTIVE_TASKS = {"queued", "running", "cancelling"}


def _previous_task(entry, task_store):
    previous = entry.latest_task()
    if not previous:
        return None
    task = task_store.get(str(previous['task_id']))
    row = entry.row
    if (task.get('owner_id') != row['user_id'] or task.get('session_id') != row['session_id']
            or task.get('task_type') != 'agent.resume'
            or (task.get('metadata') or {}).get('approval_id') != row['approval_id']):
        raise PermissionError('approval_task_scope_mismatch')
    return task


def _reconcile(entry, task):
    """A killed Worker cannot update the approval; recover conservatively."""
    if entry.row['state'] == 'queued' and not task and not entry.row.get('task_id'):
        entry.update(state='retryable', error='准备提交后服务中断，尚未创建恢复任务，可以重试。')
        return
    if entry.row['state'] not in {'queued', 'running'} or not task or task['status'] in _ACTIVE_TASKS:
        return
    invoked = entry.row.get('phase') == 'invoking'
    entry.update(state='blocked' if invoked else 'retryable', error=(
        '执行已中断，需核对结果，未自动重放已开始的操作。' if invoked
        else '恢复任务未完成准备检查，可重试，尚未执行待审批操作。'))


def list_approval_actions(*, user_id: str, session_id: str = '') -> list[dict]:
    from server.api.tasks import task_manager
    store = ApprovalStore()
    records = []
    for candidate in store.list_pending(user_id=user_id, session_id=session_id):
        try:
            with store.locked(candidate['approval_id'], user_id=user_id, session_id=candidate['session_id']) as entry:
                task = _previous_task(entry, task_manager.store)
                _reconcile(entry, task)
                row = dict(entry.row)
                row['task_id'] = str((task or {}).get('task_id') or row.get('task_id') or '')
                row['can_retry'] = row['state'] in {'pending', 'retryable'} and not (task and task['status'] in _ACTIVE_TASKS)
                if row['state'] not in {'completed', 'superseded', 'expired'}:
                    records.append(row)
        except ValueError as exc:
            if str(exc) != 'approval_expired_create_new_request':
                raise
    return records


def submit_approval_action(
    *, approval_id: str, user_id: str, session_id: str, decision: str,
    request_id: str, idempotency_key: str,
) -> dict[str, Any]:
    from server.api.tasks import task_manager

    if decision not in {"approved", "rejected"}:
        raise ValueError("invalid_approval_decision")
    if not request_id or not idempotency_key:
        raise ValueError("request_id_and_idempotency_key_required")
    with ApprovalStore().locked(approval_id, user_id=user_id, session_id=session_id) as entry:
        row = entry.row
        if row.get("decision") and row["decision"] != decision:
            raise ValueError("approval_decision_already_recorded")
        previous = _previous_task(entry, task_manager.store)
        if previous:
            if previous.get("status") in _ACTIVE_TASKS or row["state"] == "completed":
                if row.get("task_id") != previous["task_id"]:
                    entry.update(task_id=previous["task_id"])
                return previous
        _reconcile(entry, previous)
        row = entry.row
        if row.get("phase") == "invoking" or row["state"] == "blocked":
            raise ValueError("approval_execution_uncertain_do_not_replay")
        if row["state"] not in {'pending', 'retryable'}:
            raise ValueError("approval_not_available")
        entry.update(
            decision=decision, state="queued", phase="preparing", error="",
            task_id='',
            request_id=row.get("request_id") or request_id,
            idempotency_key=row.get("idempotency_key") or idempotency_key,
        )
        try:
            task = task_manager.submit(
                task_type="agent.resume", owner_id=user_id, session_id=session_id,
                kwargs={"approval_id": approval_id, "user_id": user_id, "session_id": session_id},
                metadata={"approval_id": approval_id, "run_id": row["run_id"],
                          "conversation_id": session_id, "surface": "react-agent"},
                timeout_seconds=99900, max_retries=0,
            )
        except Exception:
            # A task may already have been persisted before process launch failed.
            # Keep its association so a retry cannot create an unnoticed duplicate.
            persisted = entry.latest_task()
            entry.update(state="retryable", error="审批恢复任务未成功启动，可重试检查。",
                         task_id=str((persisted or {}).get("task_id") or row.get("task_id") or ""))
            raise RuntimeError("approval_task_submission_failed") from None
        entry.update(task_id=str(task["task_id"]))
        return task


def execute_approval_task(*, approval_id: str, user_id: str, session_id: str,
                          cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    from agent_graph.service import resume_langgraph_agent
    from application.user_credentials_service import get_user_llm_settings
    from server.task_runtime.store import TaskStore

    if not user_id or current_owner() != user_id:
        raise PermissionError("approval_owner_scope_mismatch")
    task_id = os.environ.get("STOCK_AGENT_TASK_ID", "")
    if not task_id:
        raise ValueError("approval_task_context_required")
    task = TaskStore().get(task_id)
    if (task.get("task_type") != "agent.resume" or task.get("owner_id") != user_id
            or task.get("session_id") != session_id
            or (task.get("metadata") or {}).get("approval_id") != approval_id):
        raise PermissionError("approval_task_scope_mismatch")
    store = ApprovalStore()
    with store.locked(approval_id, user_id=user_id, session_id=session_id) as entry:
        row = entry.row
        if row["state"] == "completed":
            return dict(row.get("result") or {})
        if row.get("task_id") != task_id:
            latest = entry.latest_task()
            if row.get("task_id") or not latest or latest.get("task_id") != task_id:
                raise PermissionError("approval_task_mismatch")
            entry.update(task_id=task_id)
        if row.get("phase") == "invoking" or row["state"] == "blocked":
            raise RuntimeError("approval_execution_uncertain_do_not_replay")
        if row['state'] != 'queued':
            raise ValueError('approval_task_already_claimed')
        if row.get("decision") not in {"approved", "rejected"}:
            raise ValueError("approval_decision_required")
        row = entry.update(state="running", phase="preparing", error="")

    def before_invoke():
        if cancelled():
            raise InterruptedError('审批恢复已取消，尚未执行待审批操作')
        with store.locked(approval_id, user_id=user_id, session_id=session_id) as entry:
            if entry.row['task_id'] != task_id or entry.row['state'] != 'running' or entry.row['phase'] != 'preparing':
                raise ValueError('approval_claim_changed')
            entry.update(phase='invoking')

    try:
        if cancelled():
            raise InterruptedError('审批恢复已取消，尚未执行待审批操作')
        result = resume_langgraph_agent(
            session_id=session_id, user_id=user_id, run_id=row["run_id"],
            decision=row["decision"], approval_record=dict(row),
            llm_settings=get_user_llm_settings(user_id),
            output_dir=str(workspace(user_id) / "outputs"),
            before_invoke=before_invoke,
        )
        with store.locked(approval_id, user_id=user_id, session_id=session_id) as entry:
            if entry.row['task_id'] != task_id or entry.row['phase'] != 'invoking':
                raise ValueError('approval_claim_changed')
            entry.update(state="completed", phase="completed", result=result, error="")
        return result
    except Exception:
        with store.locked(approval_id, user_id=user_id, session_id=session_id) as entry:
            invoked = entry.row.get("phase") == "invoking"
            if entry.row['task_id'] == task_id and entry.row['state'] != 'completed':
                entry.update(
                    state="blocked" if invoked else "retryable",
                    error=("恢复执行中断，需核对任务结果；为避免重复写入，未自动重放。" if invoked
                           else "恢复前检查失败，可以重试；尚未执行待审批工具。"),
                )
        raise RuntimeError("approval_resume_failed_after_invocation" if invoked
                           else "approval_resume_preflight_failed") from None
