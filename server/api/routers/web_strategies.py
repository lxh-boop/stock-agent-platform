"""Only the authenticated account may manage its research strategies."""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from application.strategy_service import StrategyService
from core.user_scope import user_scope
from server.api.contracts import OperationResponse
from server.api.routers.web_common import web_ok
from server.api.schemas.strategies import StrategyFromReport, StrategyDelete, StrategyRerun, StrategySave
from server.api.security import AuthUser, require_user

router = APIRouter(prefix='/api/v1/web/strategies', tags=['web-strategies'])


def call(user, operation):
    with user_scope(user.user_id):
        try:
            return web_ok(operation())
        except KeyError:
            raise HTTPException(404, '记录不存在或不属于当前用户') from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except HTTPException:
            raise
        except Exception:
            logging.getLogger(__name__).exception('Personal strategy operation failed')
            raise HTTPException(503, '策略数据库暂不可用，请稍后重试') from None


@router.get('', response_model=OperationResponse)
def listing(user: AuthUser = Depends(require_user), limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return call(user, lambda: StrategyService.list(limit, offset))


@router.post('/from-report', response_model=OperationResponse)
def from_report(request: StrategyFromReport, user: AuthUser = Depends(require_user)):
    return call(user, lambda: StrategyService.from_report(str(request.run_id), request_id=str(request.request_id), name=request.name))


@router.get('/{strategy_id}', response_model=OperationResponse)
def detail(strategy_id: UUID, user: AuthUser = Depends(require_user)):
    return call(user, lambda: StrategyService.detail(str(strategy_id)))


@router.put('/{strategy_id}', response_model=OperationResponse)
def save(strategy_id: UUID, request: StrategySave, user: AuthUser = Depends(require_user)):
    return call(user, lambda: StrategyService.save(str(strategy_id), request.spec.model_dump(mode='json'),
        request_id=str(request.request_id), expected_version=request.expected_version))


@router.post('/{strategy_id}/rerun', response_model=OperationResponse)
def rerun(strategy_id: UUID, request: StrategyRerun, user: AuthUser = Depends(require_user)):
    from server.api.tasks import _public_task
    return call(user, lambda: _public_task(StrategyService.rerun(str(strategy_id), request_id=str(request.request_id), expected_version=request.expected_version)))


@router.delete('/{strategy_id}', response_model=OperationResponse)
def delete(strategy_id: UUID, request: StrategyDelete, user: AuthUser = Depends(require_user)):
    return call(user, lambda: StrategyService.delete(str(strategy_id), request_id=str(request.request_id), expected_version=request.expected_version))
