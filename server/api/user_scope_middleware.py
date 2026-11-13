"""在 ASGI 请求任务中绑定身份，确保线程池中的同步路由继承同一数据域。"""
from __future__ import annotations
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from fastapi import HTTPException
from core.user_scope import user_scope


class UserScopeMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith("/api/v1/auth/") or path in {"/api/v1/health", "/api/v1/liveness", "/api/v1/readiness"}:
            return await self.app(scope, receive, send)
        from server.api.security import require_user
        try:
            principal = await run_in_threadpool(require_user, Request(scope, receive))
        except HTTPException:
            # 保留已有路由认证响应，未认证请求不会得到个人或系统数据。
            return await self.app(scope, receive, send)
        scope.setdefault("state", {})["authenticated_principal"] = principal
        with user_scope(principal.user_id):
            await self.app(scope, receive, send)
