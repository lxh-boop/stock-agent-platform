from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, Query

from application.web_paper_trading_service import web_paper_trading_service
from server.api.contracts import OperationResponse
from server.api.presenters.paper_trading import (
    present_daily_history,
    present_profile,
    present_proposals,
    present_snapshot,
    present_write_result,
)
from server.api.schemas.paper_trading import (
    BackfillPreviewRequest,
    CapitalChangePreviewRequest,
    CashFlowCancelRequest,
    ProfileUpdateRequest,
    ProposalCommitRequest,
    ProposalRejectRequest,
)
from server.api.security import AuthUser, require_user

router = APIRouter(prefix="/api/v1/web/paper-trading", tags=["web-paper-trading"])

_DRIVE_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\s\"']+")
_SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|token|password|secret|credential)\s*[:=]\s*[^,;\s]+")
_SAFE_VALUE_ERRORS = {
    "profile_update_confirmation_required", "request_id_required", "idempotency_key_required",
    "request_id_and_idempotency_key_required", "invalid_flow_type", "amount_must_be_positive",
    "start_date_required", "proposal_not_found", "confirmation_text_mismatch",
    "confirmation_token_missing_on_server", "cash_flow_cancel_confirmation_required",
    "paper_backfill_requires_task_runtime",
    "invalid_trade_date",
}

def _safe_reason(exc: Exception) -> str:
    raw = str(exc or "")
    if isinstance(exc, ValueError) and raw in _SAFE_VALUE_ERRORS:
        return raw
    raw = _DRIVE_PATH.sub("[server-path-redacted]", raw)
    raw = _SECRET_VALUE.sub(r"\1=[redacted]", raw)
    return raw[:240] if raw else "operation_failed"



def _call(action: Callable[[], Any], *, write: bool = False) -> OperationResponse:
    request_id = uuid.uuid4().hex
    try:
        return OperationResponse(success=True, data=action(), request_id=request_id)
    except Exception as exc:
        code = "WRITE_OPERATION_REJECTED" if write else "PAPER_TRADING_DATA_UNAVAILABLE"
        return OperationResponse(
            success=False,
            data=None,
            error={
                "code": code,
                "message": "写操作未执行" if write else "模拟盘数据暂时不可用",
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


@router.get("/summary", response_model=OperationResponse)
def summary(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_snapshot(web_paper_trading_service.snapshot(user.user_id)))


@router.get("/account", response_model=OperationResponse)
def account(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_write_result(web_paper_trading_service.snapshot(user.user_id).get("account") or {}))


@router.get("/positions", response_model=OperationResponse)
def positions(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_snapshot(web_paper_trading_service.snapshot(user.user_id))["positions"])


@router.get("/orders", response_model=OperationResponse)
def orders(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_snapshot(web_paper_trading_service.snapshot(user.user_id))["orders"])


@router.get("/history", response_model=OperationResponse)
def history(
    user: AuthUser = Depends(require_user),
    trade_date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
) -> OperationResponse:
    return _call(
        lambda: present_daily_history(
            web_paper_trading_service.daily_history(user.user_id, trade_date)
        )
    )


@router.get("/profile", response_model=OperationResponse)
def profile(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_profile(web_paper_trading_service.profile(user.user_id)))


@router.put("/profile", response_model=OperationResponse)
def update_profile(request: ProfileUpdateRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.save_profile(
                user_id=user.user_id,
                profile=request.profile,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                confirmed=request.confirmed,
            )
        ),
        write=True,
    )


@router.get("/cash-flows", response_model=OperationResponse)
def cash_flows(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_snapshot(web_paper_trading_service.snapshot(user.user_id))["cash_flows"])


@router.post("/cash-flows/preview", response_model=OperationResponse)
def preview_cash_flow(request: CapitalChangePreviewRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.preview_capital_change(**_with_principal(request, user))
        ),
        write=True,
    )


@router.post("/cash-flows/{cash_flow_id}/cancel", response_model=OperationResponse)
def cancel_cash_flow(cash_flow_id: str, request: CashFlowCancelRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.cancel_cash_flow(
                cash_flow_id=cash_flow_id,
                **_with_principal(request, user),
            )
        ),
        write=True,
    )


@router.post("/backfill/preview", response_model=OperationResponse)
def preview_backfill(request: BackfillPreviewRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.preview_backfill(**_with_principal(request, user))
        ),
        write=True,
    )


@router.get("/proposals", response_model=OperationResponse)
def proposals(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(lambda: present_proposals(web_paper_trading_service.proposals(user.user_id)))


@router.get("/proposals/{plan_id}", response_model=OperationResponse)
def proposal(plan_id: str, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            next(
                (item for item in web_paper_trading_service.proposals(user.user_id) if item.get("plan_id") == plan_id),
                {},
            )
        )
    )


@router.post("/proposals/{plan_id}/commit", response_model=OperationResponse)
def commit_proposal(plan_id: str, request: ProposalCommitRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.commit_proposal(
                plan_id=plan_id,
                **_with_principal(request, user),
            )
        ),
        write=True,
    )


@router.post("/proposals/{plan_id}/reject", response_model=OperationResponse)
def reject_proposal(plan_id: str, request: ProposalRejectRequest, user: AuthUser = Depends(require_user)) -> OperationResponse:
    return _call(
        lambda: present_write_result(
            web_paper_trading_service.reject_proposal(
                plan_id=plan_id,
                **_with_principal(request, user),
            )
        ),
        write=True,
    )
