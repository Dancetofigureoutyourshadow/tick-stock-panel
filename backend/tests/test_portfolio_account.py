"""本地策略账户: 通过公开服务接口验证账本与成交行为。"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.services.portfolio import PortfolioAccount, PortfolioError


def test_cash_deposit_withdrawal_and_reversal_are_immutable(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)

    deposit = account.record_cash("deposit", Decimal("10000.00"), note="初始资金")
    withdrawal = account.record_cash("withdrawal", Decimal("1200.25"))
    reversal = account.reverse_cash(withdrawal["id"], note="出金误操作")

    assert account.summary({}) == {
        "available_cash": "10000.00",
        "market_value": "0.00",
        "total_assets": "10000.00",
        "net_deposits": "10000.00",
        "realized_pnl": "0.00",
        "unrealized_pnl": "0.00",
        "total_pnl": "0.00",
    }
    assert reversal["amount"] == "1200.25"
    assert reversal["reverses_id"] == withdrawal["id"]

    with pytest.raises(PortfolioError, match="可用现金不足"):
        account.record_cash("withdrawal", Decimal("10000.01"))
    with pytest.raises(PortfolioError, match="已冲正"):
        account.reverse_cash(withdrawal["id"])

    rows = account.transactions()["cash_flows"]
    assert [row["type"] for row in rows] == ["reversal", "withdrawal", "deposit"]
    assert deposit["amount"] == "10000.00"


def test_buy_preview_reuses_score_weighting_and_account_caps(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings(
        {
            "max_positions": 4,
            "max_total_position": "0.80",
            "commission_rate": "0.001",
            "stamp_tax_rate": "0.0005",
        }
    )

    preview = account.preview_buys(
        [
            {"symbol": "600001.SH", "price": "10", "score": "3"},
            {"symbol": "600002.SH", "price": "10", "score": "1"},
        ],
        current_prices={},
    )

    assert preview["total_budget"] == "40000.00"
    assert preview["single_position_cap"] == "20000.00"
    assert preview["remaining_slots"] == 4
    assert preview["items"] == [
        {
            "symbol": "600001.SH",
            "score": "3",
            "weight": "0.75",
            "price": "10",
            "suggested_budget": "20000.00",
            "suggested_qty": 1900,
            "estimated_fee": "19.00",
            "estimated_cash_required": "19019.00",
            "blocked_reason": None,
        },
        {
            "symbol": "600002.SH",
            "score": "1",
            "weight": "0.25",
            "price": "10",
            "suggested_budget": "10000.00",
            "suggested_qty": 900,
            "estimated_fee": "9.00",
            "estimated_cash_required": "9009.00",
            "blocked_reason": None,
        },
    ]


def test_preview_equal_weight_fallback_and_keeps_blocked_rows(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 2, "max_total_position": "1"})

    preview = account.preview_buys(
        [
            {"symbol": "LOW", "score": "0", "price": "10"},
            {"symbol": "MISSING", "score": "99", "price": None},
            {"symbol": "HIGH", "score": None, "price": "20"},
            {"symbol": "EXTRA", "score": "0", "price": "10"},
        ],
        current_prices={},
    )

    by_symbol = {item["symbol"]: item for item in preview["items"]}
    assert len(preview["items"]) == 4
    assert by_symbol["LOW"]["weight"] == "0.5"
    assert by_symbol["HIGH"]["weight"] == "0.5"
    assert by_symbol["MISSING"]["blocked_reason"] == "行情缺失"
    assert by_symbol["EXTRA"]["blocked_reason"] == "持仓名额已满"


def test_commission_can_make_one_board_lot_exceed_cash(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "1000")
    account.update_settings({
        "max_positions": 1,
        "max_total_position": "1",
        "commission_rate": "0.001",
    })
    preview = account.preview_buys(
        [{"symbol": "600001.SH", "score": 1, "price": "10"}],
        current_prices={},
    )
    assert preview["items"][0]["blocked_reason"] == "预算不足一手"
    with pytest.raises(PortfolioError, match="可用现金不足"):
        account.buy(
            symbol="600001.SH", name="测试", price="10", quantity=100,
            strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-01",
        )


def test_buy_deducts_cash_records_trade_and_freezes_strategy_snapshot(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "50000")
    account.update_settings({"max_positions": 4, "commission_rate": "0.001"})
    added: list[str] = []
    snapshot = {
        "strategy_id": "trend_breakout",
        "strategy_name": "趋势突破",
        "strategy_version": "2.1.0",
        "buy_score": "88.5",
        "params": {"lookback": 20},
        "exit_signals": ["signal_ma_dead_cross"],
        "stop_loss": "0.05",
        "take_profit": "0.10",
        "trailing_stop": None,
        "max_hold_days": 20,
    }

    result = account.buy(
        symbol="600519.SH",
        name="贵州茅台",
        price="10",
        quantity=1000,
        strategy_snapshot=snapshot,
        trade_date="2026-09-07",
        add_to_watchlist=lambda symbol: added.append(symbol) or True,
    )
    snapshot["stop_loss"] = "0.99"

    assert added == ["600519.SH"]
    assert result["trade"]["gross_amount"] == "10000.00"
    assert result["trade"]["commission"] == "10.00"
    assert result["position"]["remaining_qty"] == 1000
    assert result["position"]["strategy_snapshot"]["stop_loss"] == "0.05"
    assert account.summary({"600519.SH": "11"}) == {
        "available_cash": "39990.00",
        "market_value": "11000.00",
        "total_assets": "50990.00",
        "net_deposits": "50000.00",
        "realized_pnl": "0.00",
        "unrealized_pnl": "990.00",
        "total_pnl": "990.00",
    }


def test_buy_rechecks_exposure_with_current_position_marks(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 2, "max_total_position": "0.8"})
    first = account.buy(
        symbol="AAA", name="甲", price="40", quantity=1000,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-01",
    )
    assert first["position"]["remaining_qty"] == 1000

    with pytest.raises(PortfolioError, match="最大总仓位"):
        account.buy(
            symbol="BBB", name="乙", price="40", quantity=1000,
            strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-02",
            current_prices={"AAA": "70"},
        )


def test_buy_warnings_require_explicit_override(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 4, "max_total_position": "1"})

    with pytest.raises(PortfolioError, match="买入金额超过单票仓位上限"):
        account.buy(
        symbol="AAA", name="甲", price="10", quantity=3000,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-01",
    )

    bought = account.buy(
        symbol="AAA", name="甲", price="10", quantity=3000,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-01",
        allow_buy_warnings=True,
    )
    assert bought["trade"]["gross_amount"] == "30000.00"


def test_buy_warnings_can_override_slots_exposure_and_cash_together(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 1, "max_total_position": "0.8"})
    account.buy(
        symbol="AAA", name="甲", price="10", quantity=1000,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-01",
    )

    with pytest.raises(PortfolioError, match="持仓名额已满"):
        account.buy(
            symbol="BBB", name="乙", price="10", quantity=10000,
            strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-02",
            current_prices={"AAA": "10"},
        )

    bought = account.buy(
        symbol="BBB", name="乙", price="10", quantity=10000,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-02",
        current_prices={"AAA": "10"}, allow_buy_warnings=True,
    )
    assert bought["trade"]["gross_amount"] == "100000.00"


def test_buy_rolls_back_ledger_when_watchlist_link_fails(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "10000")
    account.update_settings({"max_positions": 1})

    def fail(_symbol: str) -> bool:
        raise RuntimeError("watchlist write failed")

    with pytest.raises(RuntimeError, match="watchlist write failed"):
        account.buy(
            symbol="600001.SH",
            name="测试",
            price="10",
            quantity=100,
            strategy_snapshot={"strategy_id": "s1", "exit_signals": []},
            trade_date="2026-09-07",
            add_to_watchlist=fail,
        )

    assert account.summary({})["available_cash"] == "10000.00"
    assert account.transactions()["trades"] == []
    assert account.positions({}) == []


def test_same_stock_can_have_independent_strategy_batches(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 2, "max_total_position": "1"})
    for strategy_id in ("s1", "s2"):
        account.buy(
            symbol="600001.SH", name="测试", price="10", quantity=100,
            strategy_snapshot={"strategy_id": strategy_id},
            trade_date="2026-09-01",
        )

    positions = account.positions({}, as_of="2026-09-02")
    assert len(positions) == 2
    assert {position["source_strategy_id"] for position in positions} == {"s1", "s2"}
    with pytest.raises(PortfolioError, match="持仓名额已满"):
        account.buy(
            symbol="600002.SH", name="第三批", price="10", quantity=100,
            strategy_snapshot={"strategy_id": "s3"}, trade_date="2026-09-02",
        )


def test_sell_enforces_t_plus_one_and_handles_partial_then_full_sale(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "50000")
    account.update_settings(
        {
            "max_positions": 4,
            "commission_rate": "0.001",
            "stamp_tax_rate": "0.0005",
        }
    )
    bought = account.buy(
        symbol="600519.SH",
        name="贵州茅台",
        price="10",
        quantity=1000,
        strategy_snapshot={"strategy_id": "s1", "exit_signals": ["signal_exit"]},
        trade_date="2026-09-07",
    )
    position_id = bought["position"]["id"]

    with pytest.raises(PortfolioError, match=r"T\+1"):
        account.sell(
            position_id,
            price="12",
            quantity=400,
            trade_date="2026-09-07",
        )

    assert account.evaluate_exit_rules(
        [{"symbol": "600519.SH", "close": 10, "signal_exit": True}],
        trade_date="2026-09-08",
    )

    partial = account.sell(
        position_id,
        price="12",
        quantity=400,
        trade_date="2026-09-08",
    )
    assert partial["trade"]["commission"] == "4.80"
    assert partial["trade"]["stamp_tax"] == "2.40"
    assert partial["trade"]["realized_pnl"] == "788.80"
    assert partial["position"]["remaining_qty"] == 600
    assert partial["position"]["remaining_cost"] == "5217.20"
    assert partial["position"]["status"] == "pending_sell"
    marked = account.positions({"600519.SH": "12"}, as_of="2026-09-08")[0]
    assert marked["unrealized_pnl"] == "1194.00"
    assert account.summary({"600519.SH": "12"})["total_pnl"] == "1982.80"

    removed: list[str] = []
    closed = account.sell(
        position_id,
        price="11",
        quantity=600,
        trade_date="2026-09-09",
        remove_from_watchlist=True,
        remove_watchlist=lambda symbol: removed.append(symbol),
    )
    assert closed["position"]["status"] == "closed"
    assert closed["trade"]["realized_pnl"] == "584.10"
    assert removed == ["600519.SH"]
    assert account.summary({}) == {
        "available_cash": "51372.90",
        "market_value": "0.00",
        "total_assets": "51372.90",
        "net_deposits": "50000.00",
        "realized_pnl": "1372.90",
        "unrealized_pnl": "0.00",
        "total_pnl": "1372.90",
    }


def test_partial_loss_sale_increases_remaining_funding_cost(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "20000")
    account.update_settings({"max_positions": 1, "max_total_position": "1"})
    bought = account.buy(
        symbol="600001.SH",
        name="test",
        price="10",
        quantity=1000,
        strategy_snapshot={"strategy_id": "s1"},
        trade_date="2026-09-07",
    )

    sold = account.sell(
        bought["position"]["id"],
        price="8",
        quantity=400,
        trade_date="2026-09-08",
    )

    # Default buy cost is 10003.00; the 400-share sale returns 3197.44 after
    # fees, leaving 6805.56 of capital tied to the remaining 600 shares.
    assert sold["position"]["remaining_cost"] == "6805.56"
    assert sold["trade"]["realized_pnl"] == "-803.76"
    marked = account.positions({"600001.SH": "8"}, as_of="2026-09-08")[0]
    assert marked["unrealized_pnl"] == "-1201.80"
    assert account.summary({"600001.SH": "8"})["total_pnl"] == "-2005.56"


def test_multiple_partial_sales_preserve_cost_basis_rounding(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "20000")
    account.update_settings(
        {
            "max_positions": 1,
            "max_total_position": "1",
            "commission_rate": "0",
            "stamp_tax_rate": "0",
        }
    )
    bought = account.buy(
        symbol="600001.SH",
        name="test",
        price="10.01",
        quantity=1000,
        strategy_snapshot={"strategy_id": "s1"},
        trade_date="2026-09-07",
    )
    position_id = bought["position"]["id"]

    first = account.sell(
        position_id, price="12", quantity=333, trade_date="2026-09-08"
    )
    second = account.sell(
        position_id, price="8", quantity=333, trade_date="2026-09-09"
    )
    closed = account.sell(
        position_id, price="10", quantity=334, trade_date="2026-09-10"
    )

    assert first["position"]["remaining_cost"] == "6014.00"
    assert second["position"]["remaining_cost"] == "3350.00"
    assert closed["position"]["remaining_cost"] == "0.00"
    assert closed["position"]["realized_pnl"] == "-10.00"
    assert closed["trade"]["realized_pnl"] == "-3.34"


def test_reinitializing_account_repairs_legacy_partial_sale_cost(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "20000")
    account.update_settings({"max_positions": 1, "max_total_position": "1"})
    bought = account.buy(
        symbol="600001.SH",
        name="test",
        price="10",
        quantity=1000,
        strategy_snapshot={"strategy_id": "s1"},
        trade_date="2026-09-07",
    )
    sold = account.sell(
        bought["position"]["id"],
        price="8",
        quantity=400,
        trade_date="2026-09-08",
    )
    assert sold["position"]["remaining_cost"] == "6805.56"

    # Simulate the pre-fix persisted cost and verify account startup repairs it.
    with sqlite3.connect(account.path) as connection:
        connection.execute(
            "UPDATE positions SET remaining_cost_cents = 600180 WHERE id = ?",
            (bought["position"]["id"],),
        )

    repaired = PortfolioAccount(tmp_path).get_position(bought["position"]["id"])
    assert repaired["remaining_cost"] == "6805.56"
    assert PortfolioAccount(tmp_path).get_position(bought["position"]["id"])[
        "remaining_cost"
    ] == "6805.56"


def test_t_plus_one_uses_local_trading_days_when_available(tmp_path) -> None:
    (tmp_path / "kline_daily" / "date=2026-09-11").mkdir(parents=True)
    (tmp_path / "kline_daily" / "date=2026-09-14").mkdir(parents=True)
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    bought = account.buy(
        symbol="600001.SH", name="测试", price="10", quantity=100,
        strategy_snapshot={"strategy_id": "s1"}, trade_date="2026-09-11",
    )

    position_id = bought["position"]["id"]
    assert account.positions({}, as_of="2026-09-12")[0]["available_qty"] == 0
    assert account.positions({}, as_of="2026-09-14")[0]["available_qty"] == 100
    with pytest.raises(PortfolioError, match=r"T\+1"):
        account.sell(position_id, price="10", quantity=100, trade_date="2026-09-12")


def test_concurrent_cash_writes_are_serialized_by_sqlite_transaction(tmp_path) -> None:
    def deposit_once(_index: int) -> None:
        PortfolioAccount(tmp_path).record_cash("deposit", "100.00")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(deposit_once, range(24)))

    account = PortfolioAccount(tmp_path)
    assert account.summary({})["available_cash"] == "2400.00"
    assert len(account.transactions()["cash_flows"]) == 24


def test_exit_monitor_uses_snapshot_priority_and_deduplicates_until_cleared(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "10000")
    account.update_settings({"max_positions": 1})
    bought = account.buy(
        symbol="600001.SH",
        name="测试股票",
        price="10",
        quantity=100,
        strategy_snapshot={
            "strategy_id": "s1",
            "strategy_name": "测试策略",
            "strategy_version": "1.0",
            "exit_signals": ["signal_exit"],
            "stop_loss": "0.05",
            "max_hold_days": 99,
            "webhook_channels": ["custom"],
        },
        trade_date="2026-09-07",
    )
    position_id = bought["position"]["id"]
    crossed = [{"symbol": "600001.SH", "close": 9, "low": 9, "high": 10, "signal_exit": True}]

    first = account.evaluate_exit_rules(crossed, trade_date="2026-09-08", now_ms=1000)
    assert len(first) == 1
    assert first[0]["type"] == "position_exit"
    assert first[0]["exit_reason"] == "stop_loss"
    assert first[0]["signals"] == []
    assert first[0]["webhook_channels"] == ["custom"]
    assert account.evaluate_exit_rules(crossed, trade_date="2026-09-08", now_ms=2000) == []

    continued = account.continue_holding(position_id)
    assert continued["status"] == "holding"
    assert account.evaluate_exit_rules(crossed, trade_date="2026-09-09", now_ms=3000) == []

    account.evaluate_exit_rules(
        [{"symbol": "600001.SH", "close": 10, "low": 10, "high": 10, "signal_exit": False}],
        trade_date="2026-09-10",
        now_ms=4000,
    )
    repeated = account.evaluate_exit_rules(crossed, trade_date="2026-09-11", now_ms=5000)
    assert len(repeated) == 1
    assert repeated[0]["position_id"] == position_id


def test_no_exit_rules_are_explicit_and_signal_precedes_max_hold(tmp_path) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.update_settings({"max_positions": 2})
    no_exit = account.buy(
        symbol="600001.SH", name="无退出", price="10", quantity=100,
        strategy_snapshot={"strategy_id": "none", "exit_signals": []},
        trade_date="2026-09-01",
    )
    signal_exit = account.buy(
        symbol="600002.SH", name="有退出", price="10", quantity=100,
        strategy_snapshot={
            "strategy_id": "signal",
            "exit_signals": ["signal_exit"],
            "max_hold_days": 1,
        },
        trade_date="2026-09-01",
    )

    assert no_exit["position"]["has_exit_rules"] is False
    events = account.evaluate_exit_rules(
        [
            {"symbol": "600001.SH", "close": 10},
            {"symbol": "600002.SH", "close": 10, "signal_exit": True},
        ],
        trade_date="2026-09-02",
    )
    assert len(events) == 1
    assert events[0]["position_id"] == signal_exit["position"]["id"]
    assert events[0]["exit_reason"] == "signal"
