from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends

from application.web_settings_service import web_settings_service
from server.api.contracts import OperationResponse
from server.api.presenters.settings import present_settings
from server.api.schemas.settings import SettingsUpdateRequest
from server.api.security import require_admin

router = APIRouter(prefix="/api/v1/web/settings", tags=["web-settings"])
logger = logging.getLogger(__name__)

_DRIVE_PATH = re.compile(r"(?i)\b[a-z]:[\/][^\s\"']+")
_SAFE_REASONS = {
    "settings_update_confirmation_required",
    "request_id_required",
    "idempotency_key_required",
    "invalid_llm_mode",
    "api_credential_clear_conflict",
    "tushare_credential_clear_conflict",
    "local_llm_endpoint_must_be_local",
    "api_model_required",
    "local_model_required",
    "endpoint_required",
    "invalid_endpoint",
    "invalid_scheduler_hour",
    "invalid_scheduler_minute",
}


def _safe_reason(exc: Exception) -> str:
    raw = str(exc or "")
    if isinstance(exc, ValueError) and raw in _SAFE_REASONS:
        return raw
    raw = _DRIVE_PATH.sub("[server-path-redacted]", raw)
    return raw[:240] if raw else "settings_operation_failed"


def _call(action: Callable[[], Any], *, write: bool = False) -> OperationResponse:
    request_id = uuid.uuid4().hex
    try:
        return OperationResponse(success=True, data=action(), request_id=request_id)
    except Exception as exc:
        logger.exception("Settings operation failed (write=%s)", write)
        return OperationResponse(
            success=False,
            data=None,
            error={
                "code": "SETTINGS_WRITE_REJECTED" if write else "SETTINGS_UNAVAILABLE",
                "message": "配置未保存" if write else "配置暂时不可用",
                "details": {
                    "error_type": type(exc).__name__,
                    "reason": _safe_reason(exc),
                },
            },
            request_id=request_id,
        )


@router.get("", response_model=OperationResponse)
def settings() -> OperationResponse:
    return _call(lambda: present_settings(web_settings_service.public_settings()))


@router.put("", response_model=OperationResponse)
def update_settings(request: SettingsUpdateRequest, _admin: object = Depends(require_admin)) -> OperationResponse:
    # LLM/数据源/调度配置是平台级全局资源，仅管理员可修改。
    return _call(
        lambda: {
            **web_settings_service.update_settings(**request.model_dump()),
            "settings": present_settings(web_settings_service.public_settings()),
        },
        write=True,
    )
