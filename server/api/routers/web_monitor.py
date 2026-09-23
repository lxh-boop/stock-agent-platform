from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from application.web_read_service import web_read_service
from server.api.contracts import OperationResponse
from server.api.presenters.monitor import present_alerts, present_history, present_services, present_summary
from server.api.routers.web_common import web_call
from server.api.security import AuthUser, require_user

router = APIRouter(prefix="/api/v1/web/monitor", tags=["web-monitor"])

@router.get("/summary", response_model=OperationResponse)
def summary(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return web_call(lambda: present_summary(web_read_service.monitor_summary(user_id=user.user_id)))

@router.get("/services", response_model=OperationResponse)
def services(user: AuthUser = Depends(require_user)) -> OperationResponse:
    return web_call(lambda: present_services(web_read_service.monitor_services(user_id=user.user_id)))

@router.get("/history", response_model=OperationResponse)
def history(user: AuthUser = Depends(require_user), limit: int = Query(30, ge=1, le=200)) -> OperationResponse:
    return web_call(lambda: present_history(web_read_service.monitor_history(user_id=user.user_id, limit=limit)))

@router.get("/alerts", response_model=OperationResponse)
def alerts(user: AuthUser = Depends(require_user), limit: int = Query(100, ge=1, le=500)) -> OperationResponse:
    return web_call(lambda: present_alerts(web_read_service.monitor_alerts(user_id=user.user_id, limit=limit)))
