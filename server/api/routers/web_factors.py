"""Factor catalog API. Authentication determines all database and preview scope."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path, Query

from application.factor_catalog_service import FactorCatalogService
from core.user_scope import user_scope
from server.api.contracts import OperationResponse
from server.api.routers.web_common import web_ok
from server.api.schemas.factors import FactorCreateRequest, FactorDefinitionRequest
from server.api.security import AuthUser, require_user

router = APIRouter(prefix='/api/v1/web/factors', tags=['web-factors'])
logger = logging.getLogger(__name__)


def _call(user: AuthUser, action) -> OperationResponse:
    with user_scope(user.user_id):
        try:
            return web_ok(action(FactorCatalogService()))
        except KeyError:
            return OperationResponse(success=False, error={
                'code': 'FACTOR_NOT_FOUND', 'message': '因子不存在或不属于当前账号'})
        except ValueError as exc:
            return OperationResponse(success=False, error={
                'code': 'FACTOR_VALIDATION_FAILED', 'message': str(exc)})
        except Exception:
            logger.exception('Factor catalog operation failed')
            return OperationResponse(success=False, error={
                'code': 'FACTOR_CATALOG_UNAVAILABLE', 'message': '因子数据库暂时不可用，请稍后重试'})


@router.get('', response_model=OperationResponse)
def catalog(user: AuthUser = Depends(require_user)):
    return _call(user, lambda service: service.catalog())


@router.post('/validate', response_model=OperationResponse)
def validate(request: FactorDefinitionRequest, user: AuthUser = Depends(require_user)):
    return _call(user, lambda service: service.validate(request.name, request.expression))


@router.post('', response_model=OperationResponse)
def create(request: FactorCreateRequest, user: AuthUser = Depends(require_user)):
    return _call(user, lambda service: service.create(**request.model_dump()))


@router.get('/{factor_id}/values', response_model=OperationResponse)
def values(factor_id: str = Path(min_length=1, max_length=128),
           user: AuthUser = Depends(require_user), limit: int = Query(30, ge=1, le=100),
           code: str | None = Query(None, pattern=r'^\d{6}$')):
    return _call(user, lambda service: service.values(factor_id, limit=limit, code=code))
