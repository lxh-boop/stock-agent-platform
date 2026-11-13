"""可手算的真实行情样本：验证账户、时序和边界，不用预设收益填充报告。"""
from copy import deepcopy
from datetime import date, timedelta
import json

import pytest
from pydantic import ValidationError
from backtesting.contracts import BacktestSpec, Condition
from backtesting.engine import simulate, rank_candidates, matches

D = date(2026, 1, 5)


def make_spec(**changes):
    values = dict(start_date=D, end_date=D+timedelta(days=9), factors=[{'field':'KMID'}],
        initial_cash=10000, max_positions=1, rebalance_days=20, buy_fee=0, sell_fee=0)
    values.update(changes)
    return BacktestSpec.model_validate(values)


def quote(price=10., score=1., **kw):
    return dict(open=price, close=price, adj_factor=1., volume=1000., KMID=score, **kw)


def days(*rows):
    return [(D+timedelta(days=i), row) for i, row in enumerate(rows)]


def test_hand_calculated_cash_fees_and_next_day_open():
    p=make_spec(buy_fee=.001, sell_fee=.002, max_hold_days=1)
    data=days({'000001':quote(10)}, {'000001':quote(11)}, {'000001':quote(12)}, {'000001':quote(13)})
    report=simulate(p,data)
    buy,sell=report['trades']
    assert buy['date']==str(D+timedelta(days=1)) and buy['signal_date']==str(D)
    assert buy['shares']==900 and buy['price']==11 and buy['fee']==pytest.approx(9.9)
    assert sell['date']==str(D+timedelta(days=2)) and sell['price']==12
    assert sell['fee']==pytest.approx(21.6)
    assert report['metrics']['final_equity']==pytest.approx(10868.5)
    assert report['metrics']['total_fees']==pytest.approx(31.5)


def test_no_same_day_signal_or_last_day_liquidation():
    data=days({'000001':quote(10)}, {'000001':quote(10)}, {'000001':quote(20)})
    r=simulate(make_spec(),data)
    assert len(r['trades'])==1
    assert r['metrics']['final_equity']==20000
    assert r['positions'][-1]['shares']==1000


def test_future_mutation_cannot_change_past_trades_or_nav():
    data=days(*[{'000001':quote(10+i, i), '000002':quote(20-i, 5-i)} for i in range(8)])
    modified=deepcopy(data); modified[-1][1]['000001']['close']=1000
    a,b=[simulate(make_spec(rebalance_days=1),x) for x in (data,modified)]
    assert a['equity'][:-1]==b['equity'][:-1]
    assert a['trades']==b['trades']  # 最后一天收盘变动不能改变当天开盘订单。
    assert a['data_hash']!=b['data_hash']


def test_next_close_not_signal_close():
    data=days({'000001':quote(10)}, {'000001':{**quote(11),'close':12}}, {'000001':quote(13)})
    r=simulate(make_spec(execution='close'),data)
    assert r['trades'][0]['price']==12
    assert r['trades'][0]['shares']==800


def test_stop_on_entry_day_executes_next_day_not_two_days_later():
    data=days({'000001':quote()}, {'000001':{**quote(), 'close':8}}, {'000001':quote(7)})
    r=simulate(make_spec(stop_loss=.1),data)
    assert [t['side'] for t in r['trades']]==['buy','sell']
    assert r['trades'][1]['date']==str(D+timedelta(days=2))


def test_missing_quote_exit_is_retried_and_old_price_only():
    data=days({'000001':quote()}, {'000001':{**quote(), 'close':8}},
              {'000002':quote()}, {'000001':quote(6),'000002':quote()})
    r=simulate(make_spec(stop_loss=.1),data)
    assert r['trades'][1]['date']==str(D+timedelta(days=3))
    assert r['positions'][1]['close']==8 and r['positions'][1]['stale'] is True
    assert r['diagnostics']['stale_valuations']==1
    assert '顺延' in r['skipped_orders'][0]['reason']


def test_zero_volume_no_buy():
    r=simulate(make_spec(),days({'000001':quote()}, {'000001':{**quote(),'volume':0}}, {'000001':quote()}))
    assert r['trades']==[] and r['metrics']['total_return']==0
    assert r['skipped_orders']


def test_rebalance_sells_all_reductions_before_buys_and_no_negative_cash():
    data=days({'000001':quote(10,2),'000002':quote(10,1)},
              {'000001':quote(20,1),'000002':quote(10,2)},
              {'000001':quote(20,1),'000002':quote(10,2)})
    r=simulate(make_spec(rebalance_days=1),data)
    assert [t['side'] for t in r['trades']]==['buy','sell','buy']
    assert all(row['cash']>=0 and row['exposure']<=1+1e-9 for row in r['equity'])


def test_keep_rule_uses_signal_day_not_future_day_and_occupies_slot():
    data=days({'000001':quote(10,2),'000002':quote(10,1)},
              {'000001':quote(10,2),'000002':quote(10,3)},
              {'000001':quote(10,-3),'000002':quote(10,4)})
    r=simulate(make_spec(rebalance_days=1,keep_rules=[{'field':'KMID','op':'gt','value':1}]),data)
    assert len(r['trades'])==1 and r['positions'][-1]['code']=='000001'


def test_empty_filtered_universe_goes_to_cash_on_rebalance():
    data=days({'000001':quote(10,2)}, {'000001':quote(10,-1)}, {'000001':quote(10,-1)})
    r=simulate(make_spec(rebalance_days=1, entry_filters=[{'field':'KMID','op':'gt','value':0}]),data)
    assert [t['side'] for t in r['trades']]==['buy','sell']


def test_trigger_mode_requires_conditions_and_buys_once():
    with pytest.raises(ValidationError,match='至少'):
        make_spec(rebalance_days=0)
    r=simulate(make_spec(rebalance_days=0,entry_filters=[{'field':'KMID','op':'gt','value':0}]),
               days(*[{'000001':quote()} for _ in range(5)]))
    assert len(r['trades'])==1


def test_corporate_action_approximation_preserves_equity_and_rescales_cost():
    data=days({'000001':quote()}, {'000001':quote()},
              {'000001':{**quote(5),'adj_factor':2}}, {'000001':{**quote(6),'adj_factor':2}})
    r=simulate(make_spec(stop_loss=.1),data)
    assert r['equity'][2]['equity']==10000
    assert r['metrics']['final_equity']==12000
    assert r['diagnostics']['adjustment_events']==1
    assert len(r['trades'])==1


def test_missing_adjustment_fails_not_silent_one():
    with pytest.raises(ValueError,match='adj_factor'):
        simulate(make_spec(),days({'000001':{**quote(),'adj_factor':None}},{'000001':quote()}))


def test_rank_ties_direction_weights_and_missing_values():
    rows={c:{'KMID':v,'MA5':1.} for c,v in [('000002',2),('000001',2),('000003',1)]}
    p=make_spec(factors=[{'field':'KMID','direction':-1,'weight':2},{'field':'MA5','weight':1}])
    assert rank_candidates(rows,p)==['000003','000001','000002']
    rows['000003']['KMID']=None
    assert rank_candidates(rows,p)==['000001','000002']


@pytest.mark.parametrize('condition,value,expected',[
    ({'op':'gt','value':1},2,True),({'op':'gt','value':1},1,False),
    ({'op':'between','value':1,'upper':2},2,True),({'op':'eq','value':2},None,False),
    ({'op':'le','value':1},1,True),({'op':'ge','value':1},1,True)])
def test_filter_semantics(condition,value,expected):
    assert matches({'KMID':value},Condition(field='KMID',**condition)) is expected


@pytest.mark.parametrize('changes',[
    {'factors':[{'field':'DROP TABLE user_account'}]}, {'factors':[{'field':'KMID','weight':float('nan')}]},
    {'factors':[{'field':'KMID'},{'field':'KMID'}]}, {'stock_codes':['000001','000001']},
    {'stock_codes':['../../etc']}, {'max_positions':0}, {'rebalance_days':True},
    {'initial_cash':float('inf')}, {'start_date':'2027-01-01'}, {'user_id':'someone'},
    {'entry_filters':[{'field':'KMID','op':'between','value':3,'upper':1}]}])
def test_invalid_or_unsupported_inputs_fail_closed(changes):
    with pytest.raises(ValidationError): make_spec(**changes)


def test_cancel_does_not_return_report():
    with pytest.raises(InterruptedError): simulate(make_spec(),days({'000001':quote()},{'000001':quote()}),cancelled=lambda:True)


def test_determinism_hash_and_metrics_are_finite_json():
    data=days(*[{'000001':quote(10+i),'000002':quote(20-i)} for i in range(5)])
    a,b=[simulate(make_spec(),data) for _ in range(2)]
    assert a==b
    json.dumps(a,allow_nan=False)
    assert a['equity'][0]['nav']==1


def test_only_cash_has_null_sharpe_not_infinite():
    r=simulate(make_spec(entry_filters=[{'field':'KMID','op':'gt','value':100}]),
               days({'000001':quote()},{'000001':quote()}))
    assert r['metrics']['sharpe'] is None and r['metrics']['max_drawdown']==0


def test_no_formal_account_modules_imported_or_input_mutation():
    import sys
    modules=set(sys.modules)
    data=days({'000001':quote()},{'000001':quote()});original=deepcopy(data)
    simulate(make_spec(),data)
    assert data==original
    assert not any(n.startswith(('portfolio.','tushare','akshare')) for n in set(sys.modules)-modules)
