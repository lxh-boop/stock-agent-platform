from __future__ import annotations

import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from application.web_agent_service import web_agent_service
from server.api.contracts import OperationResponse
from server.api.presenters.agent import present_agent
from server.api.schemas.agent import (
    AgentMessageCreateRequest,
    AgentPendingActionRequest,
    AgentSessionCreateRequest,
    AgentSessionUpdateRequest,
    AgentTaskFinalizeRequest,
)
from server.api.security import AuthUser, require_user
from server.api.tasks import task_manager


router = APIRouter(prefix="/api/v1/web/agent", tags=["web-agent"])


def _safe_reason(exc: Exception) -> str:
    value = str(exc or "").replace("\\", "/")
    if ":/" in value[:8] or value.startswith("/app/"):
        return type(exc).__name__
    return value[:300]


def _call(callable_obj: Callable[[], Any], *, write: bool = False) -> OperationResponse:
    request_id = uuid.uuid4().hex
    try:
        return OperationResponse(
            success=True,
            data=present_agent(callable_obj()),
            request_id=request_id,
        )
    except Exception as exc:
        return OperationResponse(
            success=False,
            data=None,
            error={
                "code": type(exc).__name__,
                "message": "写操作未执行" if write else "Agent 数据暂时不可用",
                "details": {
                    "error_type": type(exc).__name__,
                    "reason": _safe_reason(exc),
                },
            },
            request_id=request_id,
        )


def _with_principal(request: Any, user: AuthUser) -> dict[str, Any]:
    """Body-supplied user_id is ignored; the authenticated principal wins."""
    payload = dict(request.model_dump())
    payload["user_id"] = user.user_id
    return payload


@router.get("/sessions", response_model=OperationResponse)
def sessions(
    user: AuthUser = Depends(require_user),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> OperationResponse:
    return _call(lambda: web_agent_service.list_sessions(user.user_id, limit=limit, offset=offset))


@router.post("/sessions", response_model=OperationResponse)
def create_session(request: AgentSessionCreateRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: web_agent_service.create_session(**_with_principal(request, user)), write=True)


@router.get("/sessions/{conversation_id}", response_model=OperationResponse)
def session(conversation_id: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: web_agent_service.get_session(user.user_id, conversation_id))


@router.patch("/sessions/{conversation_id}", response_model=OperationResponse)
def rename_session(conversation_id: str, request: AgentSessionUpdateRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: web_agent_service.rename_session(
            user_id=user.user_id,
            conversation_id=conversation_id,
            title=request.title,
        ),
        write=True,
    )


@router.delete("/sessions/{conversation_id}", response_model=OperationResponse)
def delete_session(conversation_id: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: web_agent_service.delete_session(user_id=user.user_id, conversation_id=conversation_id), write=True)


@router.get("/sessions/{conversation_id}/messages", response_model=OperationResponse)
def messages(
    conversation_id: str,
    user: AuthUser = Depends(require_user),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> OperationResponse:
    return _call(
        lambda: web_agent_service.list_messages(
            user.user_id,
            conversation_id,
            limit=limit,
            offset=offset,
        )
    )


@router.post("/sessions/{conversation_id}/messages", response_model=OperationResponse)
def create_message(conversation_id: str, request: AgentMessageCreateRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: web_agent_service.append_message(
            conversation_id=conversation_id,
            **_with_principal(request, user),
        ),
        write=True,
    )


@router.post("/sessions/{conversation_id}/finalize-task", response_model=OperationResponse)
def finalize_task(conversation_id: str, request: AgentTaskFinalizeRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    task = task_manager.store.get(request.task_id)
    if str(task.get("owner_id") or "") != user.user_id and not user.is_admin:
        raise HTTPException(status_code=403, detail="task_forbidden")
    return _call(
        lambda: web_agent_service.finalize_task(
            user_id=user.user_id,
            conversation_id=conversation_id,
            task=task,
        ),
        write=True,
    )


@router.get("/runs/{run_id}", response_model=OperationResponse)
def run_detail(run_id: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: web_agent_service.run_detail(user.user_id, run_id))


@router.get("/pending-actions", response_model=OperationResponse)
def pending_actions(
    user: AuthUser = Depends(require_user),
    conversation_id: str = Query(""),
) -> OperationResponse:
    return _call(lambda: web_agent_service.pending_actions(user.user_id, conversation_id=conversation_id))


@router.post("/pending-actions/{plan_id}/confirm", response_model=OperationResponse)
def confirm_action(plan_id: str, request: AgentPendingActionRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: web_agent_service.control_pending_action(
            action="confirm",
            plan_id=plan_id,
            **_with_principal(request, user),
        ),
        write=True,
    )


@router.post("/pending-actions/{plan_id}/reject", response_model=OperationResponse)
def reject_action(plan_id: str, request: AgentPendingActionRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: web_agent_service.control_pending_action(
            action="reject",
            plan_id=plan_id,
            **_with_principal(request, user),
        ),
        write=True,
    )


@router.get("/sessions/{conversation_id}/strategy-proposal", response_model=OperationResponse)
def strategy_proposal(
    conversation_id: str,
    user: AuthUser = Depends(require_user),
) -> OperationResponse:
    return _call(lambda: web_agent_service.strategy_proposal(user.user_id, conversation_id))
