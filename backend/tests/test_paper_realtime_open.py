from __future__ import annotations

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services.quote_service import QuoteService
from app.strategy import paper, paper_daily, paper_exit

SYMBOL = '603978.SH'
DAY = date(2026, 10, 20)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime.combine(DAY - timedelta(days=1), time(21), tzinfo=CN_TZ)]
    monkeypatch.setattr(paper, 'cn_now', lambda: clock[0])
    monkeypatch.setattr(paper, 'cn_today', lambda: clock[0].date())
    monkeypatch.setattr(paper_daily, 'cn_now', lambda: clock[0])
    paper.create_account(tmp_path, 30000, slippage_bps=20)
    monkeypatch.setattr(paper, '_prev_close', lambda *args: 25.15)
    confirmation = {'signal_date': '2026-10-19', 'action_key': 'test-open', 'target_trade_date': None}
    order, error = paper.create_order(tmp_path, SYMBOL, 'buy', qty=200, order_type='next_open',
                                      ref_price=25.15, confirmation=confirmation)
    assert error is None
    clock[0] = datetime.combine(DAY, time(9, 31), tzinfo=CN_TZ)
    return tmp_path, clock, order


def bars(clock, **changes):
    row = {'symbol': SYMBOL, 'date': DAY, 'open': 25.03, 'close': 24.36,
           'volume': 55994.0, 'quote_ts': int(clock[0].timestamp() * 1000)}
    row.update(changes)
    return pl.DataFrame([row])


def evaluate(env, frame=None):
    path, clock, _ = env
    return paper.evaluate_intraday(path, {SYMBOL: 24.36},
                                   opening_snapshot=bars(clock) if frame is None else frame)


def test_confirmed_next_open_fills_from_raw_open_once_and_keeps_t1(env, monkeypatch):
    path, _, order = env
    assert len(evaluate(env)) == 1
    expected = paper.apply_slippage(25.03, 'buy', 20)
    assert paper.get_order(path, order['id'])['fill_price'] == pytest.approx(expected)
    assert paper.load_positions(path)[SYMBOL]['available_qty'] == 0
    assert paper.get_account(path)['cash'] == pytest.approx(30000 - 200 * expected - 5)
    assert evaluate(env) == []
    monkeypatch.setattr(paper, 'read_daily_bar', lambda *args, **kwargs: {'open': 25.03, 'close': 24.36})
    assert paper.settle_day(path, DAY.isoformat())['filled'] == 0
    assert len(paper.load_fills(path)) == 1


@pytest.mark.parametrize('changes', [
    {'open': None}, {'open': 0.0}, {'open': -1.0}, {'open': float('nan')},
    {'volume': 0.0}, {'volume': None}, {'quote_ts': None},
    {'date': DAY - timedelta(days=1)},
    {'quote_ts': int(datetime(2026, 10, 19, 14, tzinfo=CN_TZ).timestamp() * 1000)},
    {'quote_ts': int(datetime(2026, 10, 20, 9, 25, tzinfo=CN_TZ).timestamp() * 1000)},
    {'quote_ts': int(datetime(2026, 10, 20, 10, tzinfo=CN_TZ).timestamp() * 1000)},
])
def test_invalid_opening_snapshot_leaves_order_pending(env, changes):
    path, clock, order = env
    assert evaluate(env, bars(clock, **changes)) == []
    got = paper.get_order(path, order['id'])
    assert got['status'] == 'pending' and got['postponed'] == 0


@pytest.mark.parametrize('column', ['open', 'quote_ts', 'volume', 'date'])
def test_missing_snapshot_column_keeps_post_close_fallback(env, column):
    path, clock, order = env
    assert evaluate(env, bars(clock).drop(column)) == []
    assert paper.get_order(path, order['id'])['status'] == 'pending'


@pytest.mark.parametrize('created', ['2026-10-20T09:30:00+08:00', '2026-10-20T10:00:00+08:00',
                                    '2026-10-21T08:00:00+08:00', 'invalid'])
def test_order_after_open_or_unknown_time_cannot_backfill_known_open(env, created):
    path, _, order = env
    order['created_at'] = created
    paper.save_order(path, order)
    assert evaluate(env) == []


def test_preopen_order_and_naive_beijing_timestamp_are_supported(env):
    path, _, order = env
    order['created_at'] = '2026-10-20T08:00:00'
    paper.save_order(path, order)
    assert len(evaluate(env)) == 1


@pytest.mark.parametrize('change', ['queued', 'review', 'future_target'])
def test_queued_review_and_future_target_orders_are_not_filled(env, change):
    path, _, order = env
    if change == 'queued':
        order['queued_at'] = '2026-10-20T09:30:00+08:00'
    elif change == 'review':
        order['daily_review_required'] = True
    else:
        order['confirmation']['target_trade_date'] = '2026-10-21'
    paper.save_order(path, order)
    assert evaluate(env) == []


def test_before_continuous_open_cannot_match(env):
    _, clock, _ = env
    clock[0] = clock[0].replace(hour=9, minute=25)
    assert evaluate(env) == []


def test_opening_limit_up_reuses_existing_rejection(env):
    path, clock, order = env
    assert evaluate(env, bars(clock, open=27.67)) == []
    assert paper.get_order(path, order['id'])['status'] == 'expired'


def test_opening_limit_queue_is_not_retried_in_same_session(env):
    path, clock, order = env
    account = paper.get_account(path)
    account['queue_limit_orders'] = True
    paper.save_account(path, account)
    assert evaluate(env, bars(clock, open=27.67)) == []
    assert evaluate(env) == []
    got = paper.get_order(path, order['id'])
    assert got['postponed'] == 1 and got['status'] == 'pending'


def test_quote_service_passes_raw_snapshot_and_broadcasts_fill(env, monkeypatch):
    path, clock, order = env
    service = QuoteService()
    service._app_state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=path)),
                                        strategy_engine=object())
    service.get_enriched_today = lambda: (pl.DataFrame({'symbol': [SYMBOL], 'raw_close': [24.36],
                                                       'open': [12.515]}), DAY)
    monkeypatch.setattr(service, '_is_continuous_trading', lambda: True)
    monkeypatch.setattr('app.services.quote_service.cn_today', lambda: DAY)
    monkeypatch.setattr(paper_daily, 'daily_strategy_ids', lambda *args: set())
    monkeypatch.setattr(paper_exit, 'evaluate', lambda *args, **kwargs: [])
    events = []
    monkeypatch.setattr(service, '_broadcast_alerts', lambda rows: events.extend(rows))
    monkeypatch.setattr(service, '_maybe_send_system_notifications', lambda *args: None)
    monkeypatch.setattr(service, '_maybe_send_paper_webhook', lambda *args: None)
    service._evaluate_monitors(bars(clock), None)
    assert paper.get_order(path, order['id'])['status'] == 'filled'
    assert paper.get_order(path, order['id'])['fill_price'] == pytest.approx(paper.apply_slippage(25.03, 'buy', 20))
    assert any(e.get('account_id') == 'default' and e.get('symbol') == SYMBOL for e in events)


def test_missing_realtime_data_still_settles_at_official_open(env, monkeypatch):
    path, _, order = env
    assert paper.evaluate_intraday(path, {SYMBOL: 24.36}) == []
    monkeypatch.setattr(paper, 'read_daily_bar', lambda *args, **kwargs: {'open': 25.03, 'close': 24.36})
    assert paper.settle_day(path, DAY.isoformat())['filled'] == 1
    assert paper.get_order(path, order['id'])['fill_price'] == pytest.approx(paper.apply_slippage(25.03, 'buy', 20))


def test_next_open_sell_uses_opening_price_and_existing_t1_allocations(env):
    path, clock, order = env
    paper.cancel_order(path, order['id'])
    clock[0] -= timedelta(days=1)
    _, error = paper.create_order(path, SYMBOL, 'buy', qty=200, ref_price=25)
    assert error is None
    assert len(paper.evaluate_intraday(path, {SYMBOL: 25})) == 1
    clock[0] = datetime.combine(DAY, time(8), tzinfo=CN_TZ)
    sell, error = paper.create_order(path, SYMBOL, 'sell', qty=200, order_type='next_open')
    assert error is None
    clock[0] = clock[0].replace(hour=9, minute=31)
    assert len(evaluate(env)) == 1
    assert paper.get_order(path, sell['id'])['fill_price'] == pytest.approx(paper.apply_slippage(25.03, 'sell', 20))
    assert paper.load_positions(path)[SYMBOL]['qty'] == 0


@pytest.mark.parametrize('opening', [None, 0.0, 25.03])
def test_quote_normalization_does_not_use_chart_placeholder_as_open(env, monkeypatch, opening):
    path, clock, order = env
    service = QuoteService()
    monkeypatch.setattr('app.services.quote_service.cn_today', lambda: DAY)
    monkeypatch.setattr('app.services.quote_service._persist_last_fetch', lambda *args: None)
    frame, _ = service._process_full_market_records([{
        'symbol': SYMBOL, 'last_price': 24.36, 'open': opening, 'high': 25.2,
        'low': 24.0, 'volume': 55994.0, 'amount': 100000.0,
        'timestamp': int(clock[0].timestamp() * 1000),
    }], t0=0.0, now_ts=0.0)
    events = evaluate(env, frame)
    assert len(events) == (1 if opening else 0)
    assert paper.get_order(path, order['id'])['status'] == ('filled' if opening else 'pending')
