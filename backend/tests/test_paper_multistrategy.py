from __future__ import annotations

from datetime import date

from app.strategy import paper, paper_auto


def _account(tmp_path):
    return paper.create_account(tmp_path, 100_000, account_id="default")


def _fill(data_dir, *, order_id, symbol, strategy_id, qty, price, day="2026-01-02"):
    paper._append_fill(data_dir, {
        "seq": int(order_id[-1]),
        "ts": f"{day}T10:00:00+08:00",
        "date": day,
        "order_id": order_id,
        "symbol": symbol,
        "asset_type": "stock",
        "side": "buy",
        "qty": qty,
        "price": price,
        "fee": 5.0,
        "kind": "fill",
        "strategy_id": strategy_id,
        "strategy_snapshot": {"stop_loss": 0.08} if strategy_id else {},
    })


def test_multistrategy_groups_and_targeted_exit(tmp_path, monkeypatch):
    _account(tmp_path)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 1, 10))
    _fill(tmp_path, order_id="buy_a1", symbol="600000.SH", strategy_id="strategy_a", qty=500, price=10)
    _fill(tmp_path, order_id="buy_b1", symbol="600000.SH", strategy_id="strategy_b", qty=300, price=12)
    _fill(tmp_path, order_id="buy_m1", symbol="600000.SH", strategy_id=None, qty=200, price=11)
    paper._materialize(tmp_path)

    overview = paper.overview(tmp_path)
    holding = overview["holdings"][0]
    groups = {(item["owner_type"], item.get("strategy_id")): item for item in holding["groups"]}
    assert holding["qty"] == 1000
    assert holding["avg_cost"] == 10.815
    assert groups[("strategy", "strategy_a")]["qty"] == 500
    assert groups[("strategy", "strategy_a")]["avg_cost"] == 10.01
    assert groups[("strategy", "strategy_b")]["qty"] == 300
    assert groups[("manual", None)]["qty"] == 200

    order, error = paper.create_order(
        tmp_path, "600000.SH", "sell", qty=500, order_type="market", ref_price=13,
        source="exit:buy_a1", strategy_id="strategy_a", target_lot_id="buy_a1",
    )
    assert error is None
    assert order["target_lot_id"] == "buy_a1"
    fill = paper._fill_order(tmp_path, order, 13, "2026-01-10")
    assert fill is not None
    assert fill["lot_allocations"] == [{"lot_id": "buy_a1", "qty": 500}]

    after = paper.load_positions(tmp_path)["600000.SH"]["lots"]
    assert {lot["strategy_id"] for lot in after} == {"strategy_b", None}
    assert sum(lot["qty"] for lot in after) == 500


def test_manual_fifo_skips_reserved_strategy_exit(tmp_path, monkeypatch):
    _account(tmp_path)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 1, 10))
    _fill(tmp_path, order_id="buy_a1", symbol="600000.SH", strategy_id="strategy_a", qty=100, price=10)
    _fill(tmp_path, order_id="buy_b1", symbol="600000.SH", strategy_id="strategy_b", qty=100, price=12)
    paper._materialize(tmp_path)

    reserved, error = paper.create_order(
        tmp_path, "600000.SH", "sell", qty=100, order_type="market", ref_price=13,
        source="exit:buy_b1", strategy_id="strategy_b", target_lot_id="buy_b1",
    )
    assert error is None
    manual, error = paper.create_order(
        tmp_path, "600000.SH", "sell", qty=100, order_type="market", ref_price=13,
        source="manual",
    )
    assert error is None
    manual_fill = paper._fill_order(tmp_path, manual, 13, "2026-01-10")
    assert manual_fill is not None
    assert manual_fill["lot_allocations"] == [{"lot_id": "buy_a1", "qty": 100}]
    assert reserved["target_lot_id"] == "buy_b1"


def test_strategy_auto_sell_targets_only_its_own_lot(tmp_path, monkeypatch):
    _account(tmp_path)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 1, 10))
    _fill(tmp_path, order_id="buy_a1", symbol="600000.SH", strategy_id="strategy_a", qty=100, price=10)
    _fill(tmp_path, order_id="buy_b1", symbol="600000.SH", strategy_id="strategy_b", qty=100, price=11)
    paper._materialize(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "策略 A 自动跟单", "match_kind": "strategy", "match_id": "strategy_a",
        "side": "auto", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "cooldown_days": 0, "enabled": True,
    })
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "strategy_a", "type": "sell_signal",
        "symbol": "600000.SH", "price": 13,
    }])
    assert len(created) == 1
    assert created[0]["side"] == "sell"
    assert created[0]["target_lot_id"] == "buy_a1"
    assert created[0]["strategy_id"] == "strategy_a"


def test_two_strategy_buy_events_keep_two_orders_and_event_direction(tmp_path):
    _account(tmp_path)
    for strategy_id in ("strategy_a", "strategy_b"):
        paper_auto.create_auto_rule(tmp_path, {
            "name": f"{strategy_id} 自动跟单", "match_kind": "strategy", "match_id": strategy_id,
            "side": "auto", "size_mode": "fixed_amount", "size_value": 10_000,
            "cooldown_days": 0, "enabled": True,
        })
    created = paper_auto.on_rule_events(tmp_path, [
        {"source": "strategy", "strategy_id": "strategy_a", "type": "buy_signal", "symbol": "600000.SH", "price": 10},
        {"source": "strategy", "strategy_id": "strategy_b", "type": "buy_signal", "symbol": "600000.SH", "price": 12},
    ])
    assert len(created) == 2
    assert {order["strategy_id"] for order in created} == {"strategy_a", "strategy_b"}
    assert {order["side"] for order in created} == {"buy"}
