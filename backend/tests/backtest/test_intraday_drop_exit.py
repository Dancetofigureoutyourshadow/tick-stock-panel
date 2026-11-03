"""前收锚日内跌幅线 (intraday_drop_pct) 与跌停当日排单 (queue_limit_down_same_day)。

两条都是 opt-in: 不设置时引擎行为与既有版本逐字一致(由 test_limit_down_close_exit
与基线护栏覆盖)。本文件只锁新行为:
- 跌幅线以「上一有效收盘价」为锚, 盘中触及按触发价成交, 建仓当日不触发;
- 一字跌停日任何风控线都卖不出, 递延到首个可卖日;
- queue_limit_down_same_day 只放行「盘中开过板」的封死跌停日, 一字板仍拦截。
"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig

START = date(2024, 1, 1)


def _day(i: int, o: float, h: float, l: float, c: float, *, ld: bool = False) -> dict:
    return {
        "symbol": "A", "date": START + timedelta(days=i),
        "open": o, "high": h, "low": l, "close": c,
        "volume": 100_000,
        "signal_limit_up": False, "signal_limit_down": ld,
    }


def _normal(i: int) -> dict:
    px = 10.0 + i * 0.05
    return _day(i, px, px + 0.02, px - 0.02, px)


def _run(days: list[dict], entry_days: list[int], exit_days: list[int], **matcher):
    panel = pl.DataFrame(days).sort(["symbol", "date"])
    dates = panel["date"].to_list()
    entry_mask = pl.Series([d in {START + timedelta(days=i) for i in entry_days} for d in dates], dtype=pl.Boolean)
    exit_mask = pl.Series([d in {START + timedelta(days=i) for i in exit_days} for d in dates], dtype=pl.Boolean)
    kw: dict = {"matching": "close_t", "fees_pct": 0, "slippage_bps": 0}
    kw.update(matcher)
    return BacktestEngine(repo=None).simulate_independent_candidates(  # type: ignore[arg-type]
        panel, entry_mask, exit_mask, MatcherConfig(**kw),
    )


def _run_portfolio(days: list[dict], entry_days: list[int], exit_days: list[int], **matcher):
    panel = pl.DataFrame(days).sort(["symbol", "date"])
    dates = panel["date"].to_list()
    entry_mask = pl.Series([d in {START + timedelta(days=i) for i in entry_days} for d in dates], dtype=pl.Boolean)
    exit_mask = pl.Series([d in {START + timedelta(days=i) for i in exit_days} for d in dates], dtype=pl.Boolean)
    kw: dict = {"matching": "close_t", "fees_pct": 0, "slippage_bps": 0}
    kw.update(matcher)
    return BacktestEngine(repo=None).simulate_portfolio(  # type: ignore[arg-type]
        panel, entry_mask, exit_mask, MatcherConfig(**kw),
    )


def _crash_panel() -> list[dict]:
    """day0 收盘 10.00; day1 收盘 10.05; day2 盘中最低 9.10(前收 -9.45%)。"""
    return [
        _normal(0),
        _normal(1),
        _day(2, 9.90, 9.95, 9.10, 9.20),
        _normal(3),
        _normal(4),
    ]


@pytest.mark.parametrize("runner", [_run, _run_portfolio])
def test_intraday_drop_uses_previous_close_anchor(runner):
    """跌幅线锚定前收 10.05, 触发价 10.05×0.915=9.19575, 当日盘中成交。"""
    result = runner(_crash_panel(), entry_days=[0], exit_days=[], intraday_drop_pct=0.085)
    assert result.stats.get("n_trades") == 1
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-03"
    assert trade.exit_reason == "intraday_drop"
    assert trade.exit_price == pytest.approx(9.1958, abs=1e-4)
    assert trade.blocked_exit_days == 0


def test_intraday_drop_does_not_fire_on_entry_day():
    """建仓当日不触发风控线(当日最低价发生在建仓之前)。"""
    days = [_normal(0), _day(1, 9.10, 9.15, 8.90, 9.00)] + [_normal(i) for i in range(2, 5)]
    result = _run(days, entry_days=[1], exit_days=[], intraday_drop_pct=0.085)
    assert result.stats.get("n_trades") == 1
    assert result.trades[0].exit_reason != "intraday_drop"


def test_intraday_drop_blocked_by_one_price_limit_down():
    """一字跌停日跌幅线也卖不出: 当日受阻, 递延到下一可卖日按收盘价落账。"""
    days = [
        _normal(0),
        _normal(1),
        _day(2, 9.00, 9.00, 9.00, 9.00, ld=True),
        _day(3, 9.10, 9.15, 9.05, 9.10),
        _normal(4),
    ]
    result = _run(days, entry_days=[0], exit_days=[], intraday_drop_pct=0.085)
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-04"
    # blocked_exit_days 计的是受阻尝试次数: 风控线受阻后同日 pending 重试再计一次,
    # 与既有 stop_loss 在一字板日的口径一致(2), 不是多持有了一天。
    assert trade.blocked_exit_days == 2
    assert trade.exit_price == pytest.approx(9.10, abs=1e-4)


@pytest.mark.parametrize("runner", [_run, _run_portfolio])
def test_queue_limit_down_same_day_fills_sealed_but_opened_board(runner):
    """盘中开过板的封死跌停日: 开启排单后当日按跌停价成交, 不再递延。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 10.10, 10.12, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    blocked = runner(days, entry_days=[0], exit_days=[3])
    assert blocked.trades[0].exit_date == "2024-01-05"
    assert blocked.trades[0].blocked_exit_days == 1

    filled = runner(days, entry_days=[0], exit_days=[3], queue_limit_down_same_day=True)
    trade = filled.trades[0]
    assert trade.exit_date == "2024-01-04"
    assert trade.exit_price == pytest.approx(9.09, abs=1e-4)
    assert trade.blocked_exit_days == 0


def test_queue_limit_down_same_day_still_blocks_one_price():
    """排单开关不放行一字板: 无一笔可成交, 仍递延。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 9.09, 9.09, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    result = _run(days, entry_days=[0], exit_days=[3], queue_limit_down_same_day=True)
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-05"
    assert trade.blocked_exit_days == 1


@pytest.mark.parametrize("runner", [_run, _run_portfolio])
def test_queue_max_hold_only_fills_on_max_hold_day(runner):
    """max_hold 到期日遇「封死但盘中开板」的跌停: 收窄排单只在此时按收盘价成交。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 10.10, 10.12, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    result = runner(
        days,
        entry_days=[0],
        exit_days=[],
        max_hold_days=3,
        queue_limit_down_same_day=True,
        queue_limit_down_max_hold_only=True,
    )
    trade = result.trades[0]
    assert trade.exit_reason == "max_hold"
    assert trade.exit_date == "2024-01-04"
    assert trade.exit_price == pytest.approx(9.09, abs=1e-4)
    assert trade.blocked_exit_days == 0
    assert (result.stats.get("execution") or {}).get("sell_limit_down_queue_fill") == 1


@pytest.mark.parametrize("runner", [_run, _run_portfolio])
def test_queue_max_hold_only_leaves_signal_exit_deferred(runner):
    """收窄排单不影响 signal 出场: 同一跌停日仍按原口径递延。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 10.10, 10.12, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    result = runner(
        days,
        entry_days=[0],
        exit_days=[3],
        queue_limit_down_same_day=True,
        queue_limit_down_max_hold_only=True,
    )
    trade = result.trades[0]
    assert trade.exit_reason == "signal"
    assert trade.exit_date == "2024-01-05"
    assert trade.blocked_exit_days == 1
    assert (result.stats.get("execution") or {}).get("sell_limit_down_queue_fill", 0) == 0


@pytest.mark.parametrize("runner", [_run, _run_portfolio])
def test_queue_max_hold_only_still_blocks_one_price(runner):
    """收窄排单不放行一字板: max_hold 到期日仍递延。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 9.09, 9.09, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    result = runner(
        days,
        entry_days=[0],
        exit_days=[],
        max_hold_days=3,
        queue_limit_down_same_day=True,
        queue_limit_down_max_hold_only=True,
    )
    trade = result.trades[0]
    assert trade.exit_reason == "max_hold"
    assert trade.exit_date == "2024-01-05"
    assert trade.blocked_exit_days == 1
    assert (result.stats.get("execution") or {}).get("sell_limit_down_queue_fill", 0) == 0


def test_queue_max_hold_only_without_queue_keeps_baseline():
    """未开 queue_limit_down_same_day 时, 收窄开关不产生排单成交。"""
    days = [
        _normal(0),
        _normal(1),
        _normal(2),
        _day(3, 10.10, 10.12, 9.09, 9.09, ld=True),
        _normal(4),
    ]
    result = _run(
        days,
        entry_days=[0],
        exit_days=[],
        max_hold_days=3,
        queue_limit_down_max_hold_only=True,
    )
    trade = result.trades[0]
    assert trade.exit_date == "2024-01-05"
    assert trade.blocked_exit_days == 1
    assert (result.stats.get("execution") or {}).get("sell_limit_down_queue_fill", 0) == 0


def test_new_options_rejected_by_legacy_paths():
    """legacy 路径不支持新选项时必须显式报错, 不得静默忽略。"""
    panel = pl.DataFrame(_crash_panel()).sort(["symbol", "date"])
    dates = panel["date"].to_list()
    entries = pl.Series([d == START for d in dates], dtype=pl.Boolean)
    exits = pl.Series([False for _ in dates], dtype=pl.Boolean)
    with pytest.raises(ValueError):
        BacktestEngine(repo=None).simulate_independent_candidates_legacy(  # type: ignore[arg-type]
            panel, entries, exits, MatcherConfig(intraday_drop_pct=0.085),
        )
    with pytest.raises(ValueError):
        BacktestEngine(repo=None).simulate_portfolio_legacy(  # type: ignore[arg-type]
            panel, entries, exits, MatcherConfig(queue_limit_down_same_day=True),
        )
    with pytest.raises(ValueError):
        BacktestEngine(repo=None).simulate_independent_candidates_legacy(  # type: ignore[arg-type]
            panel, entries, exits, MatcherConfig(queue_limit_down_max_hold_only=True),
        )
