from __future__ import annotations

import json
from datetime import date

from app.strategy import paper, paper_auto, paper_exit, paper_intents


def _account(tmp_path, **kwargs):
    return paper.create_account(tmp_path, 100_000, account_id="default", **kwargs)


def test_account_settings_and_score_weight_preview(tmp_path):
    _account(tmp_path)
    acc = paper.get_account(tmp_path)
    assert "max_total_position" not in acc
    preview = paper.preview_buys(
        tmp_path,
        [
            {"symbol": "600000.SH", "strategy_id": "s1", "score": 3, "price": 10},
            {"symbol": "600001.SH", "strategy_id": "s1", "score": 1, "price": 10},
        ],
        {},
    )
    assert preview["remaining_slots"] == 50
    assert preview["items"][0]["suggested_qty"] == preview["items"][1]["suggested_qty"]


def test_auto_follow_disabled_creates_intent(tmp_path):
    _account(tmp_path)
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "策略跟单", "match_kind": "strategy", "match_id": "s1",
        "side": "auto", "entry_fill": "open_t+1", "exit_fill": "close_t",
        "position_sizing": "equal", "max_positions": 1, "max_total_position": 10,
        "auto_follow_enabled": False, "auto_exit_enabled": False,
        "cooldown_days": 5, "enabled": True,
    })
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "type": "buy_signal", "strategy_id": "s1", "symbol": "600000.SH", "price": 10,
    }])
    assert len(created) == 1
    assert created[0]["kind"] == "intent"
    assert paper.load_orders(tmp_path) == []
    assert paper_intents.load(tmp_path)[0]["status"] == "pending"
    assert paper_intents.load(tmp_path)[0]["source"] == f"auto:{rule['id']}"
    assert paper_intents.load(tmp_path)[0]["automation_source"] == "auto_follow"
    assert paper_intents.load(tmp_path)[0]["strategy_snapshot"] is not None
    event = paper_auto.auto_order_events(created)[0]
    assert event["type"] == "auto_intent"
    assert "待确认" in event["message"]


def test_only_one_enabled_rule_can_own_a_strategy(tmp_path):
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "策略跟单 A", "match_kind": "strategy", "match_id": "s1",
        "side": "auto", "auto_follow_enabled": True, "auto_exit_enabled": False,
        "cooldown_days": 0,
    })
    try:
        paper_auto.create_auto_rule(tmp_path, {
            "name": "策略跟单 B", "match_kind": "strategy", "match_id": "s1",
            "side": "auto", "auto_follow_enabled": True, "auto_exit_enabled": False,
            "cooldown_days": 0,
        })
    except ValueError as exc:
        assert "已有启用" in str(exc)
    else:
        raise AssertionError("同一策略不应同时存在两条启用规则")


def test_strategy_rule_always_follows_both_sides(tmp_path):
    _account(tmp_path)
    try:
        paper_auto.create_auto_rule(tmp_path, {
            "name": "策略单向规则", "match_kind": "strategy", "match_id": "s1",
            "side": "buy", "auto_follow_enabled": True, "auto_exit_enabled": False,
            "cooldown_days": 0,
        })
    except ValueError as exc:
        assert "side" in str(exc) and "auto" in str(exc)
    else:
        raise AssertionError("策略跟单不应再选择单向买入或卖出")


def test_stored_one_sided_strategy_rule_migrates_to_buy_and_sell(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 24))
    _account(tmp_path)
    rule_dir = paper._root(tmp_path, "default") / "auto_rules"
    rule_dir.mkdir(parents=True)
    (rule_dir / "arule_old.json").write_text(json.dumps({
        "id": "arule_old", "name": "旧单向跟单", "match_kind": "strategy",
        "match_id": "s1", "side": "buy", "size_mode": "fixed_amount",
        "size_value": 10000, "order_type": "next_open", "enabled": True,
    }), encoding="utf-8")
    rule = paper_auto.load_auto_rules(tmp_path)[0]
    assert rule["side"] == "auto"
    assert rule["auto_follow_enabled"] and rule["auto_exit_enabled"]
    assert len(paper_auto.strategy_monitor_rules(tmp_path)) == 1
    paper._append_fill(tmp_path, {
        "seq": 1, "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "side": "buy", "qty": 100,
        "price": 10, "fee": 5, "kind": "fill", "strategy_id": "s1",
    })
    paper._materialize(tmp_path)
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "s1", "type": "sell_signal",
        "symbol": "600000.SH", "price": 11,
    }])
    assert len(created) == 1 and created[0]["side"] == "sell"


def test_score_weight_is_applied_to_strategy_follow_orders(tmp_path):
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "评分跟单", "match_kind": "strategy", "match_id": "s1",
        "side": "auto", "entry_fill": "close_t", "exit_fill": "open_t+1",
        "position_sizing": "score_weight", "max_positions": 10, "max_total_position": 100,
        "auto_follow_enabled": True, "auto_exit_enabled": False,
        "cooldown_days": 0, "enabled": True,
    })
    created = paper_auto.on_rule_events(tmp_path, [
        {"source": "strategy", "type": "buy_signal", "strategy_id": "s1", "symbol": "600000.SH", "price": 10, "score": 3,
         "strategy_snapshot": {"stop_loss": 0.05}},
        {"source": "strategy", "type": "buy_signal", "strategy_id": "s1", "symbol": "600001.SH", "price": 10, "score": 1,
         "strategy_snapshot": {"take_profit": 0.1}},
    ])
    orders = {item["symbol"]: item for item in created}
    assert orders["600000.SH"]["qty"] == 900  # 回测单只目标仓位上限 10%
    assert orders["600001.SH"]["qty"] == 400  # 评分权重 1/4 的预算向下取整
    assert orders["600000.SH"]["strategy_snapshot"]["stop_loss"] == 0.05


def test_existing_strategy_symbol_does_not_consume_new_position_slot(tmp_path):
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "持仓补仓", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "close_t", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 2, "max_total_position": 100,
        "auto_follow_enabled": True, "auto_exit_enabled": False, "cooldown_days": 0,
    })
    paper._append_fill(tmp_path, {
        "seq": 1, "date": "2026-09-23", "order_id": "a1", "symbol": "600000.SH",
        "side": "buy", "qty": 100, "price": 10, "fee": 5, "kind": "fill", "strategy_id": "s1",
    })
    paper._materialize(tmp_path)
    created = paper_auto.on_rule_events(tmp_path, [
        {"source": "strategy", "strategy_id": "s1", "type": "buy_signal", "symbol": "600000.SH", "price": 10, "score": 1},
        {"source": "strategy", "strategy_id": "s1", "type": "buy_signal", "symbol": "600001.SH", "price": 10, "score": 1},
    ])
    assert {item["symbol"] for item in created} == {"600001.SH"}


def test_monitor_rule_must_choose_buy_or_sell(tmp_path):
    _account(tmp_path)
    try:
        paper_auto.create_auto_rule(tmp_path, {
            "name": "无方向", "match_kind": "rule", "match_id": "r1", "side": "auto",
            "cooldown_days": 0,
        })
    except ValueError as exc:
        assert "明确选择" in str(exc)
    else:
        raise AssertionError("监控规则的方向不能使用 auto")


def test_strategy_sell_signal_uses_auto_exit_switch(tmp_path):
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "策略买卖", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "close_t", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 10, "max_total_position": 10,
        "auto_follow_enabled": True, "auto_exit_enabled": False,
        "cooldown_days": 0, "enabled": True,
    })
    paper._append_fill(tmp_path, {
        "seq": 1, "ts": "", "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 10.0, "fee": 5.0, "kind": "fill", "strategy_id": "s1",
    })
    paper._materialize(tmp_path)
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "s1", "type": "sell_signal",
        "symbol": "600000.SH", "price": 11,
    }])
    assert len(created) == 1 and created[0]["kind"] == "intent"
    assert created[0]["automation_source"] == "strategy_exit"
    assert created[0]["exit_reason"] == "signal"
    assert paper.load_orders(tmp_path) == []


def test_strategy_signal_exit_uses_configured_close_order(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 24))
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "收盘卖出", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "open_t+1", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 1, "max_total_position": 100,
        "auto_follow_enabled": True, "auto_exit_enabled": True,
    })
    paper._append_fill(tmp_path, {
        "seq": 1, "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "side": "buy", "qty": 100, "price": 10,
        "fee": 5, "kind": "fill", "strategy_id": "s1",
    })
    paper._materialize(tmp_path)
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "s1", "type": "sell_signal",
        "symbol": "600000.SH", "price": 11,
    }])
    assert len(created) == 1
    assert created[0]["order_type"] == "close"
    assert created[0]["target_lot_id"] == "buy1"


def test_buy_cooldown_does_not_block_strategy_sell_signal(tmp_path, monkeypatch):
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _account(tmp_path)
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "策略冷却", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "close_t", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 10, "max_total_position": 20,
        "auto_follow_enabled": True, "auto_exit_enabled": True,
        "cooldown_days": 5, "enabled": True,
    })
    buy, error = paper.create_order(
        tmp_path, "600000.SH", "buy", qty=100, order_type="market", ref_price=10,
        source=f"auto:{rule['id']}", strategy_id="s1", strategy_scope_id="s1",
        auto_rule_id=rule["id"], max_positions=10, max_total_position=0.2,
    )
    assert error is None
    paper._fill_order(tmp_path, buy, 10, "2026-09-23")
    created = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy", "strategy_id": "s1", "type": "sell_signal",
        "symbol": "600000.SH", "price": 11,
    }])
    assert len(created) == 1 and created[0]["side"] == "sell"


def test_manual_exit_creates_intent_and_continue_holding(tmp_path, monkeypatch):
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "策略退出", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "open_t+1", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 10, "max_total_position": 10, "auto_follow_enabled": True,
        "auto_exit_enabled": False, "cooldown_days": 0, "enabled": True,
    })
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
    _account(tmp_path)
    paper_auto.create_auto_rule(tmp_path, {
        "name": "策略退出", "match_kind": "strategy", "match_id": "s1", "side": "auto",
        "entry_fill": "open_t+1", "exit_fill": "close_t", "position_sizing": "equal",
        "max_positions": 10, "max_total_position": 10, "auto_follow_enabled": True,
        "auto_exit_enabled": True, "cooldown_days": 0, "enabled": True,
    })
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
    fills = paper.evaluate_intraday(tmp_path, {"600000.SH": 10.6})
    assert len(fills) == 1 and fills[0]["side"] == "sell"
    assert paper.load_orders(tmp_path)[0]["status"] == "filled"
