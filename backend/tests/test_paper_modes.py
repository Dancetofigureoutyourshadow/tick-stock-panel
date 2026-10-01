from __future__ import annotations

from datetime import date

from app.strategy import paper, paper_auto, paper_exit, paper_intents


def _account(tmp_path, **kwargs):
    return paper.create_account(tmp_path, 100_000, account_id="default", **kwargs)


def test_account_settings_and_score_weight_preview(tmp_path):
    _account(
        tmp_path,
        entry_fill="close_t",
        exit_fill="open_t+1",
        position_sizing="score_weight",
        max_positions=2,
        max_total_position=0.8,
    )
    acc = paper.get_account(tmp_path)
    assert acc["entry_fill"] == "close_t"
    assert acc["exit_fill"] == "open_t+1"
    assert acc["position_sizing"] == "score_weight"
    assert acc["max_positions"] == 2
    preview = paper.preview_buys(
        tmp_path,
        [
            {"symbol": "600000.SH", "strategy_id": "s1", "score": 3, "price": 10},
            {"symbol": "600001.SH", "strategy_id": "s1", "score": 1, "price": 10},
        ],
        {},
    )
    assert preview["remaining_slots"] == 2
    assert preview["items"][0]["suggested_qty"] > preview["items"][1]["suggested_qty"]


def test_auto_follow_disabled_creates_intent(tmp_path):
    _account(tmp_path, auto_follow_enabled=False)
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "策略跟单", "match_kind": "strategy", "match_id": "s1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "cooldown_days": 5, "enabled": True,
    })
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "s1", "symbol": "600000.SH", "price": 10,
    }])
    assert len(created) == 1
    assert created[0]["kind"] == "intent"
    assert paper.load_orders(tmp_path) == []
    assert paper_intents.load(tmp_path)[0]["status"] == "pending"
    assert paper_intents.load(tmp_path)[0]["source"] == f"auto:{rule['id']}"
    assert paper_intents.load(tmp_path)[0]["strategy_snapshot"] is not None


def test_score_weight_is_applied_to_strategy_follow_orders(tmp_path):
    _account(tmp_path, position_sizing="score_weight")
    paper_auto.create_auto_rule(tmp_path, {
        "name": "评分跟单", "match_kind": "strategy", "match_id": "s1",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 10_000,
        "order_type": "next_open", "cooldown_days": 0, "enabled": True,
    })
    created = paper_auto.on_rule_events(tmp_path, [
        {"source": "strategy", "strategy_id": "s1", "symbol": "600000.SH", "price": 10, "score": 3,
         "strategy_snapshot": {"stop_loss": 0.05}},
        {"source": "strategy", "strategy_id": "s1", "symbol": "600001.SH", "price": 10, "score": 1,
         "strategy_snapshot": {"take_profit": 0.1}},
    ])
    orders = {item["symbol"]: item for item in created}
    assert orders["600000.SH"]["qty"] == 700
    assert orders["600001.SH"]["qty"] == 200  # 2500 元按 A 股整手取整
    assert orders["600000.SH"]["strategy_snapshot"]["stop_loss"] == 0.05


def test_manual_exit_creates_intent_and_continue_holding(tmp_path, monkeypatch):
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _account(tmp_path, auto_exit_enabled=False)
    paper._append_fill(tmp_path, {
        "seq": 1, "ts": "", "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 10.0, "fee": 5.0, "kind": "fill", "strategy_id": "s1",
        "strategy_snapshot": {"stop_loss": 0.05},
    })
    paper._materialize(tmp_path)
    events = paper_exit.evaluate(tmp_path, [{
        "symbol": "600000.SH", "raw_close": 9.0, "raw_low": 8.9, "raw_high": 9.1,
    }], trade_date="2026-09-24")
    assert len(events) == 1
    intents = paper_intents.load(tmp_path)
    assert len(intents) == 1 and intents[0]["side"] == "sell"
    assert paper_exit.continue_holding(tmp_path, intents[0]["lot_id"]) is not None


def test_auto_exit_creates_market_order(tmp_path, monkeypatch):
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _account(tmp_path, auto_exit_enabled=True)
    paper._append_fill(tmp_path, {
        "seq": 1, "ts": "", "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 10.0, "fee": 5.0, "kind": "fill", "strategy_id": "s1",
        "strategy_snapshot": {"take_profit": 0.05},
    })
    paper._materialize(tmp_path)
    events = paper_exit.evaluate(tmp_path, [{
        "symbol": "600000.SH", "raw_close": 10.6, "raw_low": 10.5, "raw_high": 10.6,
    }], trade_date="2026-09-24")
    assert len(events) == 1
    orders = paper.load_orders(tmp_path)
    assert len(orders) == 1 and orders[0]["side"] == "sell" and orders[0]["order_type"] == "market"
    assert orders[0]["target_lot_id"] == "buy1"
