from __future__ import annotations

import traceback
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends

from server.api.contracts import OperationRequest, OperationResponse
from server.api.serialization import decode_transport, encode_transport
from server.api.security import AuthUser, require_user


def _apply_principal_identity(kwargs: dict[str, Any], user: AuthUser) -> dict[str, Any]:
    """Force client-supplied identity fields to the authenticated principal.

    管理员保留显式跨用户操作能力；普通用户提交的 user_id/owner_id 一律改写为
    登录身份，防止通过 kwargs 冒充他人。
    """
    if user.role == "admin":
        return kwargs
    patched = dict(kwargs or {})
    for key in ("user_id", "owner_id"):
        if key in patched:
            patched[key] = user.user_id
    return patched


def build_operation_router(
    *,
    prefix: str,
    tag: str,
    invoker: Callable[[str, list[Any], dict[str, Any]], Any],
    bootstrap: Callable[[], dict[str, Any]] | None = None,
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag])

    if bootstrap is not None:
        @router.get("/bootstrap", response_model=OperationResponse)
        def read_bootstrap() -> OperationResponse:
            request_id = uuid.uuid4().hex
            try:
                return OperationResponse(success=True, data=encode_transport(bootstrap()), request_id=request_id)
            except Exception as exc:
                return OperationResponse(
                    success=False,
                    error={
                        "code": type(exc).__name__,
                        "message": str(exc),
                        "details": {"traceback_tail": traceback.format_exc()[-4000:]},
                    },
                    request_id=request_id,
                )

    @router.post("/operations/{operation}", response_model=OperationResponse)
    def call_operation(
        operation: str, request: OperationRequest, user: AuthUser = Depends(require_user)
    ) -> OperationResponse:
        request_id = uuid.uuid4().hex
        try:
            args = decode_transport(request.args)
            kwargs = _apply_principal_identity(
                dict(decode_transport(request.kwargs) or {}), user
            )
            result = invoker(str(operation), list(args or []), kwargs)
            return OperationResponse(success=True, data=encode_transport(result), request_id=request_id)
        except KeyError as exc:
            return OperationResponse(
                success=False,
                error={"code": "OPERATION_NOT_ALLOWED", "message": str(exc), "details": {}},
                request_id=request_id,
            )
        except Exception as exc:
            return OperationResponse(
                success=False,
                error={
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "details": {"traceback_tail": traceback.format_exc()[-4000:]},
                },
                request_id=request_id,
            )

    return router
