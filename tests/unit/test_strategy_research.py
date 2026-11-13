"""完整迁移：高级回测、动态权重、指数择时、分层分析与同源筛选。"""
from copy import deepcopy
from datetime import date, timedelta
import json

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from backtesting.contracts import BacktestSpec, AnalysisSpec, ScreenSpec, Sizing
from backtesting.engine import simulate
from backtesting.analysis import analyze, screen
from backtesting.signals import SignalState, members, spearman, target_weights
from backtesting.market import normalize_index

D = date(2026, 1, 5)


def q(px=10, score=1, **kw):
    return dict(open=px,close=px,volume=1000,adj_factor=1,KMID=score,**kw)


def series(*rows): return [(D+timedelta(days=i),r) for i,r in enumerate(rows)]


def params(**changes):
    return {'start_date':D,'end_date':D+timedelta(days=20),'factors':[{'field':'KMID'}],
        'initial_cash':10000,'max_positions':1,'rebalance_days':20,'buy_fee':0,'sell_fee':0,**changes}


def bs(**changes): return BacktestSpec.model_validate(params(**changes))


def test_spearman_ties_missing_zero_variance():
    assert spearman([1,2,2,3],[10,20,20,30])==pytest.approx(1)
    assert spearman([1,2,3,None],[3,2,1,9])==pytest.approx(-1)
    assert spearman([1,1,1],[1,2,3]) is None
    assert spearman([1,2],[2,1]) is None


def test_dynamic_weights_only_use_realized_returns_and_direction():
    p=bs(factors=[{'field':'KMID','direction':1},{'field':'MA5','direction':-1}],weighting={'method':'rank_ic','window':5})
    state=SignalState(p)
    old={str(i).zfill(6):q(10,i,MA5=i) for i in (1,2,3)}
    new={str(i).zfill(6):q(10+i,i,MA5=i) for i in (1,2,3)}
    state.observe(D,old); assert state.weights()==[.5,.5]
    state.observe(D+timedelta(days=1),new); assert state.weights()==pytest.approx([1,0])
    # 负IC不是取绝对值，所有负IC回退等权。
    for key in state.ics: state.ics[key].clear();state.ics[key].append(-.5)
    assert state.weights()==[.5,.5]


def test_dynamic_future_change_does_not_rewrite_earlier_weights():
    p=bs(rebalance_days=1,factors=[{'field':'KMID'},{'field':'MA5'}],weighting={'method':'rank_ic','window':5})
    data=series(*[{str(c).zfill(6):q(10+d*c,c,MA5=4-c) for c in (1,2,3)} for d in range(8)])
    altered=deepcopy(data);altered[-1][1]['000003']['close']=.1
    a,b=simulate(p,data),simulate(p,altered)
    assert a['signals'][:-1]==b['signals'][:-1]
    assert a['trades']==b['trades']


def test_sizing_fixed_residual_and_factor_positive():
    p=bs(max_positions=3,sizing={'method':'fixed','proportions':[.4,.1,.1]})
    assert sum(target_weights(p,['a','b','c'],{}).values())==pytest.approx(.6)
    p=bs(max_positions=3,sizing={'method':'fixed','proportions':[.5]})
    assert target_weights(p,['a','b','c'],{})=={'a':.5,'b':.25,'c':.25}
    p=bs(max_positions=2,sizing={'method':'factor','field':'MA5'})
    assert target_weights(p,['a','b'],{'a':{'MA5':3},'b':{'MA5':1}})=={'a':.75,'b':.25}
    assert sum(target_weights(p,['a','b'],{'a':{'MA5':-3}}).values())==0


def test_position_function_uses_total_budget_not_one_stock_budget():
    p=bs(max_positions=2,sizing={'method':'fixed','proportions':[.5,.5]})
    r=simulate(p,series({'000001':q(),'000002':q()},{'000001':q(),'000002':q()}))
    assert sum(t['amount'] for t in r['trades'])==10000 and r['equity'][-1]['cash']==0


def test_rank_out_and_field_sell_work_in_both_modes():
    data=series({'000001':q(10,3),'000002':q(10,1)},
                {'000001':q(10,1),'000002':q(10,2)},
                {'000001':q(10,1),'000002':q(10,2)})
    for change in ({'rank_out':1},{'sell_filters':[{'field':'KMID','op':'lt','value':2}]}):
        for mode in (0,20):
            r=simulate(bs(rebalance_days=mode,entry_filters=[{'field':'KMID','op':'gt','value':0}],**change),data)
            assert any(t['side']=='sell' and t['date']==str(D+timedelta(days=2)) for t in r['trades'])


def test_keep_rule_protects_sell_but_not_timing_zero_exposure():
    timing={'rules':[{'index':'sse','field':'close','op':'gt','value':100}],'bear_exposure':0,'enter_votes':1,'exit_votes':1}
    data=series({'000001':q()}, {'000001':q()}, {'000001':q()})
    market={day:{'sse':{'close':val}} for (day,_),val in zip(data,[110,90,90])}
    r=simulate(bs(timing=timing,keep_rules=[{'field':'KMID','op':'gt','value':0}]),data,markets=market)
    assert [t['side'] for t in r['trades']]==['buy','sell']
    assert r['trades'][1]['reason']=='择时减仓'
    assert r['equity'][1]['exposure']==1 and r['equity'][2]['exposure']==0


def test_timing_hysteresis_and_abstention():
    p=bs(timing={'rules':[{'field':'close','op':'gt','value':100,'index':'sse'},
                          {'field':'ret20','op':'gt','value':0,'index':'sse'}],
                 'enter_votes':2,'exit_votes':2,'bear_exposure':.2})
    state=SignalState(p)
    assert state.timing_step({'sse':{'close':80}})[0]==1  # 缺失的一票不当作熊。
    assert state.timing_step({'sse':{'close':80,'ret20':-.1}})[0]==.2
    assert state.timing_step({'sse':{'close':110,'ret20':-.1}})[0]==.2
    assert state.timing_step({'sse':{'close':110,'ret20':.1}})[0]==1


def test_true_benchmark_and_market_filters_are_not_synthetic():
    data=series({'000001':q()}, {'000001':q()}, {'000001':q()})
    p=bs(benchmark='hs300',market_filters=[{'index':'sse','field':'close','op':'gt','value':100}])
    markets={d:{'hs300':{'close':100*(1.1**i)},'sse':{'close':90}} for i,(d,_) in enumerate(data)}
    r=simulate(p,data,markets=markets)
    assert r['metrics']['benchmark_return']==pytest.approx(.21) and r['trades']==[]
    with pytest.raises(ValueError,match='真实基准'): simulate(p,data)


def test_fixed_limits_apply_to_entry_exit_and_delayed_exit():
    data=series({'000001':q(10)}, {'000001':q(11)}, {'000001':q(11)})
    assert simulate(bs(limit_pct=.1),data)['trades']==[]
    data=series({'000001':q(10)}, {'000001':q(10)}, {'000001':q(9)}, {'000001':q(9.1)})
    r=simulate(bs(limit_pct=.1,max_hold_days=1),data)
    assert len(r['trades'])==2 and r['trades'][1]['date']==str(D+timedelta(days=3))
    assert any('跌幅' in row['reason'] for row in r['skipped_orders'])


def test_added_shares_do_not_lock_old_holding_and_sellable_is_reported():
    r=simulate(bs(max_positions=2,rebalance_days=1),series({'000001':q(10)}, {'000001':q(10)}, {'000001':q(5)}))
    end=r['positions'][-1]
    assert end['shares']==700 and end['sellable_shares']==500


def test_trigger_band_rebalances_existing_positions():
    r=simulate(bs(rebalance_days=0,entry_filters=[{'field':'KMID','op':'gt','value':0}],exposure=.5),
        series({'000001':q(10)}, {'000001':q(10)}, {'000001':q(20)}))
    assert any(t['reason']=='仓位偏离减持' for t in r['trades'])


def test_pause_marks_to_market_does_not_freeze_nav_or_replay_pending():
    data=series({'000001':q()}, {'000001':q()}, {'000001':q(8)}, {'000001':q(9)})
    p=bs(exclude_ranges=[{'start':D+timedelta(days=2),'end':D+timedelta(days=2)}])
    r=simulate(p,data)
    assert r['equity'][2]['nav']==.8 and r['diagnostics']['paused_days']==1
    p=bs(exclude_ranges=[{'start':D+timedelta(days=1),'end':D+timedelta(days=2)}])
    assert simulate(p,data)['trades']==[]  # 跳过首日待执行买单，不在解冻时补做。


def test_point_in_time_pool_never_uses_future_snapshot():
    p=bs(pool_schedule=[{'as_of':D,'stock_codes':['000001']},{'as_of':D+timedelta(days=2),'stock_codes':['000002']}])
    assert members(p,D,['000001','000002'])==['000001']
    data=series(*[{'000001':q(10,1),'000002':q(10,100)} for _ in range(4)])
    r=simulate(p,data)
    assert r['trades'][0]['code']=='000001'


def test_all_factors_missing_is_not_a_clear_position_signal():
    data=series({'000001':q()}, {'000001':q()}, {'000001':q(10,None)}, {'000001':q(10,None)})
    r=simulate(bs(rebalance_days=1),data)
    assert all(t['side']=='buy' for t in r['trades'])
    assert any('截面排序因子全部缺失' in t['reason'] for t in r['skipped_orders'])


def analysis_data():
    return series(*[{str(c).zfill(6):q(10*(1+c/100)**i,c,MA5=-c) for c in range(1,11)} for i in range(9)])


def test_analysis_hand_calculated_ic_layers_decay_and_correlation():
    p=AnalysisSpec(start_date=D,end_date=D+timedelta(days=8),
        factors=[{'field':'KMID'},{'field':'MA5'}],interval_days=2,segment_count=5)
    r=analyze(p,analysis_data())
    assert r['factor_metrics']['KMID']['ic_mean']==pytest.approx(1)
    assert r['factor_metrics']['MA5']['ic_mean']==pytest.approx(-1)
    assert r['factor_metrics']['KMID']['segment_mean_returns'][0] > r['factor_metrics']['KMID']['segment_mean_returns'][-1]
    corr=next(x for x in r['correlation'] if x['left']=='KMID' and x['right']=='MA5')
    assert corr['correlation']==pytest.approx(-1)
    assert all(x['ic']==pytest.approx(1) for x in r['ic_decay'] if x['factor']=='KMID' and x['ic'] is not None)
    assert len(r['segments'])==2*5*4 and r['long_short'][-2]['nav']>1
    json.dumps(r,allow_nan=False)


def test_analysis_null_samples_not_manufactured_zero_and_direction():
    p=AnalysisSpec(start_date=D,end_date=D+timedelta(days=8),factors=[{'field':'KMID','direction':-1}],interval_days=2)
    r=analyze(p,analysis_data());assert r['factor_metrics']['KMID']['ic_mean']==pytest.approx(-1)
    data=series(*[{'000001':q(10,1),'000002':q(10,1)} for _ in range(4)])
    p=AnalysisSpec(start_date=D,end_date=D+timedelta(days=3),factors=[{'field':'KMID'}],interval_days=1)
    r=analyze(p,data)
    assert r['factor_metrics']['KMID']['ic_mean'] is None
    assert any(x['return'] is None for x in r['segments'])
    assert r['correlation'][0]['correlation'] is None


def test_daily_screen_matches_backtest_signal_and_no_trades():
    bars=analysis_data()
    p=bs(factors=[{'field':'KMID'},{'field':'MA5','weight':.1}],max_positions=3,rebalance_days=1,
         weighting={'method':'rank_ic','window':5})
    r=simulate(p,bars)
    s=ScreenSpec.model_validate({**p.model_dump(),'start_date':bars[-1][0],'end_date':bars[-1][0]})
    out=screen(s,bars)
    assert [v['code'] for v in out['selections']]==r['signals'][-1]['candidates']
    assert out['signals'][0]['weights']==r['signals'][-1]['weights']
    assert 'trades' not in out


def test_index_normalization_is_causal_and_checks_calendar():
    dates=pd.bdate_range('2025-01-01',periods=65)
    frame=pd.DataFrame({'trade_date':dates.strftime('%Y%m%d'),'open':np.arange(65)+100,
        'high':np.arange(65)+101,'low':np.arange(65)+99,'close':np.arange(65)+100,'pre_close':np.arange(65)+99})
    a=normalize_index(frame,list(dates.strftime('%Y%m%d')),dates[0].date(),dates[-1].date())
    other=frame.copy();other.loc[64,'close']=999
    b=normalize_index(other,list(dates.strftime('%Y%m%d')),dates[0].date(),dates[-1].date())
    assert a[:-1]==b[:-1] and a[0]['ma20'] is None and a[-1]['ma60'] is not None
    with pytest.raises(ValueError,match='缺少'):normalize_index(frame.iloc[1:],list(dates.strftime('%Y%m%d')),dates[0].date(),dates[-1].date())


@pytest.mark.parametrize('change',[
    {'weighting':{'method':'execute_python'}}, {'sizing':{'method':'factor','field':'password_hash'}},
    {'sizing':{'method':'fixed','proportions':[-1]}}, {'sizing':{'method':'fixed','proportions':[.5,.5]}},
    {'timing':{'rules':[{'index':'sse','field':'password','op':'gt','value':0}]}},
    {'timing':{'rules':[{'index':'sse','field':'close','op':'gt','value':0}],'enter_votes':0}},
    {'pool_schedule':[{'as_of':D+timedelta(days=1),'stock_codes':['000001']}]},
    {'limit_pct':1}, {'market_filters':[{'index':'secret','field':'close','op':'gt','value':0}]},
])
def test_unsupported_inputs_fail_not_silently_ignored(change):
    with pytest.raises(ValidationError):bs(**change)


def test_analysis_and_screen_cancellation():
    data=analysis_data()
    p=AnalysisSpec(start_date=D,end_date=D+timedelta(days=8),factors=[{'field':'KMID'}],interval_days=1)
    with pytest.raises(InterruptedError):analyze(p,data,cancelled=lambda:True)
    s=ScreenSpec.model_validate({**params(),'start_date':D,'end_date':D})
    with pytest.raises(InterruptedError):screen(s,data,cancelled=lambda:True)
