"""个人研究API；替换旧latest文件契约，所有身份来自登录态。"""
from typing import Literal
from uuid import UUID
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from backtesting.contracts import BacktestRequest, AnalysisRequest, ScreenRequest, StrategyRequest, DeleteRequest
from backtesting import store
from application.backtest_service import BacktestService
from core.user_scope import user_scope
from server.api.contracts import OperationResponse
from server.api.routers.web_common import web_ok
from server.api.security import AuthUser, require_user

router = APIRouter(prefix='/api/v1/web/backtests', tags=['web-backtests'])


def call(user: AuthUser, operation):
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
            logging.getLogger(__name__).exception('Personal research operation failed')
            raise HTTPException(503, '回测数据库暂不可用，请查看服务器私有日志') from None


@router.get('/catalog', response_model=OperationResponse)
def catalog(user: AuthUser = Depends(require_user)):
    return call(user, store.catalog)


@router.get('/strategies', response_model=OperationResponse)
def strategies(user: AuthUser = Depends(require_user), limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return call(user, lambda: store.list_strategies(limit, offset))


@router.put('/strategies', response_model=OperationResponse)
def save_strategy(request: StrategyRequest, user: AuthUser = Depends(require_user)):
    return call(user, lambda: store.save_strategy(str(request.strategy_id), request.spec.model_dump(mode='json')))


@router.delete('/strategies/{strategy_id}', response_model=OperationResponse)
def remove_strategy(strategy_id: UUID, request: DeleteRequest, user: AuthUser = Depends(require_user)):
    return call(user, lambda: store.delete_strategy(str(strategy_id)) or {'deleted': True})


@router.get('', response_model=OperationResponse)
def history(user: AuthUser = Depends(require_user), limit: int = Query(30, ge=1, le=100),
            offset: int = Query(0, ge=0), kind: Literal['backtest', 'analysis', 'screen'] | None = None):
    return call(user, lambda: store.list_reports(limit, offset, kind))


def submit(user, request, kind):
    from server.api.tasks import _public_task
    return call(user, lambda: _public_task(BacktestService.submit(
        request.spec.model_dump(mode='json'), request_id=str(request.request_id), kind=kind)))


@router.post('', response_model=OperationResponse)
def run(request: BacktestRequest, user: AuthUser = Depends(require_user)):
    return submit(user, request, 'backtest')


@router.post('/analyses', response_model=OperationResponse)
def analysis(request: AnalysisRequest, user: AuthUser = Depends(require_user)):
    return submit(user, request, 'analysis')


@router.post('/screen', response_model=OperationResponse)
def screen(request: ScreenRequest, user: AuthUser = Depends(require_user)):
    return submit(user, request, 'screen')


@router.get('/{run_id}', response_model=OperationResponse)
def detail(run_id: UUID, user: AuthUser = Depends(require_user)):
    return call(user, lambda: store.get_report(str(run_id), summary=True))


@router.delete('/{run_id}', response_model=OperationResponse)
def remove_report(run_id: UUID, request: DeleteRequest, user: AuthUser = Depends(require_user)):
    return call(user, lambda: store.delete_report(str(run_id)) or {'deleted': True})


@router.get('/{run_id}/rows', response_model=OperationResponse)
def rows(run_id: UUID, kind: Literal['equity', 'trades', 'positions', 'skipped_orders', 'signals', 'periods',
         'ic_series', 'segments', 'long_short', 'ic_decay', 'correlation', 'selections'],
         user: AuthUser = Depends(require_user), limit: int = Query(50, ge=1, le=1500), offset: int = Query(0, ge=0), factor: str | None = Query(None, max_length=30)):
    return call(user, lambda: store.report_rows(str(run_id), kind, limit, offset, factor))


@router.get('/{run_id}/export')
def export(run_id: UUID, user: AuthUser = Depends(require_user)):
    from fastapi.responses import Response
    import json
    with user_scope(user.user_id):
        try:
            report = store.get_report(str(run_id))
        except KeyError:
            raise HTTPException(404, '报告不存在或不属于当前用户') from None
        except Exception:
            logging.getLogger(__name__).exception('Personal research export failed')
            raise HTTPException(503, '报告导出失败，请查看服务器私有日志') from None
    return Response(json.dumps(report, ensure_ascii=False, allow_nan=False), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="research-{run_id}.json"'})
