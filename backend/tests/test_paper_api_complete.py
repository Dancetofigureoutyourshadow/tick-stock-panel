"""Paper 页面全部账户级功能的 HTTP 验收测试。

这些测试把原持仓账户的配置、现金流水、批次退出和手动确认能力，
统一按现在的 Paper API 走一遍，避免只测域函数而漏掉页面实际调用的路由。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import paper as paper_api
from app.strategy import paper, paper_auto, paper_exit


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(paper_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda symbol: "etf" if symbol.startswith(("51", "56", "58", "15")) else "stock",
    )
    return TestClient(app)


def test_account_configuration_and_strategy_buy_http(tmp_path, monkeypatch):
    client = _client(tmp_path)

    created = client.post("/api/paper/account", json={
        "initial_cash": 200_000,
        "account_id": "strategy_a",
        "name": "策略账户",
        "auto_follow_enabled": False,
        "auto_exit_enabled": True,
        "entry_fill": "close_t",
        "exit_fill": "open_t+1",
        "position_sizing": "score_weight",
        "max_positions": 4,
        "max_total_position": 0.75,
    })
    assert created.status_code == 200
    account = created.json()["account"]
    assert account["auto_follow_enabled"] is False
    assert account["auto_exit_enabled"] is True
    assert account["entry_fill"] == "close_t"
    assert account["exit_fill"] == "open_t+1"
    assert account["position_sizing"] == "score_weight"
    assert account["max_positions"] == 4
    assert account["max_total_position"] == 0.75

    got = client.get("/api/paper/account?account=strategy_a")
    assert got.status_code == 200
    assert got.json()["account"]["id"] == "strategy_a"

    updated = client.post("/api/paper/settings?account=strategy_a", json={
        "auto_follow_enabled": True,
        "auto_exit_enabled": False,
        "position_sizing": "score_weight",
            "max_positions": 2,
        "max_total_position": 0.9,
    })
    assert updated.status_code == 200
    assert updated.json()["account"]["auto_follow_enabled"] is True
    assert updated.json()["account"]["auto_exit_enabled"] is False
    assert updated.json()["account"]["position_sizing"] == "score_weight"

    monkeypatch.setattr(
        paper_api,
        "_strategy_snapshot",
        lambda request, strategy_id, buy_score: {
            "strategy_id": strategy_id,
            "stop_loss": 0.08,
            "buy_score": None if buy_score is None else str(buy_score),
        },
    )
    preview = client.post("/api/paper/buys/preview?account=strategy_a", json={
        "items": [
            {"symbol": "600000.SH", "strategy_id": "s1", "score": 3, "price": 10},
            {"symbol": "600001.SH", "strategy_id": "s1", "score": 1, "price": 10},
        ],
    })
    assert preview.status_code == 200
    assert preview.json()["items"][0]["suggested_qty"] > preview.json()["items"][1]["suggested_qty"]

    buy = client.post("/api/paper/buys?account=strategy_a", json={
        "symbol": "600000.SH",
        "strategy_id": "s1",
        "buy_score": 3,
        "price": 10,
        "quantity": 100,
    })
    assert buy.status_code == 200
    assert buy.json()["order"]["source"] == "manual"
    assert buy.json()["order"]["strategy_id"] is None
    assert buy.json()["order"]["strategy_snapshot"] == {}
    assert buy.json()["order"]["order_type"] == "market"


def test_cash_transactions_and_reversal_http(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/paper/account", json={"initial_cash": 10_000}).status_code == 200

    deposit = client.post("/api/paper/cash", json={"type": "deposit", "amount": 5_000, "note": "测试入金"})
    withdrawal = client.post("/api/paper/cash", json={"type": "withdrawal", "amount": 1_000})
    assert deposit.status_code == 200 and withdrawal.status_code == 200

    tx = client.get("/api/paper/transactions")
    assert tx.status_code == 200
    assert len(tx.json()["cash_flows"]) == 2
    assert tx.json()["cash_flows"][0]["type"] == "deposit"

    reverse = client.post(
        f"/api/paper/cash/{withdrawal.json()['cash_flow']['id']}/reverse",
        json={"note": "撤销出金"},
    )
    assert reverse.status_code == 200
    assert reverse.json()["cash_flow"]["type"] == "reversal"
    assert client.get("/api/paper/overview").json()["cash"] == 15_000
    assert client.post(
        f"/api/paper/cash/{withdrawal.json()['cash_flow']['id']}/reverse",
        json={},
    ).status_code == 400


def test_intent_confirm_reject_and_revalidation_http(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/paper/account", json={
        "initial_cash": 100_000,
        "auto_follow_enabled": False,
    }).status_code == 200
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "HTTP 跟单",
        "match_kind": "strategy",
        "match_id": "s1",
        "side": "buy",
        "size_mode": "fixed_amount",
        "size_value": 10_000,
        "order_type": "next_open",
        "cooldown_days": 0,
    })
    intents = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy",
        "strategy_id": "s1",
        "symbol": "600000.SH",
        "price": 10,
        "score": 2,
        "strategy_snapshot": {"stop_loss": 0.05},
    }])
    assert intents and intents[0]["kind"] == "intent"
    intent_id = intents[0]["id"]

    listed = client.get("/api/paper/intents")
    assert listed.status_code == 200
    assert listed.json()["intents"][0]["source"] == f"auto:{rule['id']}"
    assert client.get("/api/paper/orders").json()["orders"] == []

    confirmed = client.post(f"/api/paper/intents/{intent_id}/confirm", json={
        "qty": 500,
        "order_type": "market",
        "ref_price": 10,
    })
    assert confirmed.status_code == 200
    assert confirmed.json()["order"]["qty"] == 500
    assert confirmed.json()["order"]["intent_id"] == intent_id
    assert confirmed.json()["intent"]["status"] == "confirmed"

    paper.update_settings(tmp_path, auto_follow_enabled=False)
    second = paper_auto.on_rule_events(tmp_path, [{
        "source": "strategy",
        "strategy_id": "s1",
        "symbol": "600001.SH",
        "price": 10,
        "score": 1,
    }])
    second_id = second[0]["id"]
    rejected = client.post(f"/api/paper/intents/{second_id}/reject", json={"reason": "人工跳过"})
    assert rejected.status_code == 200
    assert rejected.json()["intent"]["status"] == "rejected"


def test_positions_continue_holding_and_read_models_http(tmp_path, monkeypatch):
    day = date(2026, 9, 24)
    monkeypatch.setattr(paper, "cn_today", lambda: day)
    client = _client(tmp_path)
    assert client.post("/api/paper/account", json={
        "initial_cash": 100_000,
        "auto_exit_enabled": False,
    }).status_code == 200
    paper._append_fill(tmp_path, {
        "seq": 1,
        "ts": "",
        "date": "2026-09-23",
        "order_id": "buy_http",
        "symbol": "600000.SH",
        "asset_type": "stock",
        "side": "buy",
        "qty": 100,
        "price": 10.0,
        "fee": 5.0,
        "kind": "fill",
        "strategy_id": "s1",
        "strategy_snapshot": {"stop_loss": 0.05},
    })
    paper._materialize(tmp_path)
    events = paper_exit.evaluate(tmp_path, [{
        "symbol": "600000.SH",
        "raw_close": 9.0,
        "raw_low": 8.9,
        "raw_high": 9.1,
    }], trade_date=day.isoformat())
    assert len(events) == 1
    lot_id = events[0]["position_id"]

    positions = client.get("/api/paper/positions")
    assert positions.status_code == 200
    assert positions.json()["holdings"][0]["lots"][0]["pending_fingerprint"]
    continued = client.post(f"/api/paper/positions/{lot_id}/continue-holding")
    assert continued.status_code == 200
    overview = client.get("/api/paper/overview").json()
    assert overview["holdings"][0]["lots"][0]["pending_fingerprint"] is None

    assert client.get("/api/paper/trades").status_code == 200
    assert client.get("/api/paper/nav").status_code == 200
    assert client.get("/api/paper/stats").status_code == 200
    assert client.post("/api/paper/rebuild").status_code == 200
    assert client.get("/api/paper/compare").json()["accounts"]


def test_strategy_intent_confirmation_rechecks_consumed_target_lot(tmp_path, monkeypatch):
    client = _client(tmp_path)
    assert client.post("/api/paper/account", json={"initial_cash": 100_000, "auto_exit_enabled": False}).status_code == 200
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 24))
    paper._append_fill(tmp_path, {
        "seq": 1, "ts": "", "date": "2026-09-23", "order_id": "buy1",
        "symbol": "600000.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 10.0, "fee": 5.0, "kind": "fill", "strategy_id": "s1",
        "strategy_snapshot": {"stop_loss": 0.05},
    })
    paper._materialize(tmp_path)
    paper_exit.evaluate(tmp_path, [{"symbol": "600000.SH", "raw_close": 9.0, "raw_low": 8.9, "raw_high": 9.1}], trade_date="2026-09-24")
    intent = client.get("/api/paper/intents").json()["intents"][0]

    # 模拟确认前另一条已存在的卖出成交，确认接口必须重新检查目标批次。
    paper._append_fill(tmp_path, {
        "seq": 2, "ts": "", "date": "2026-09-24", "order_id": "manual1",
        "symbol": "600000.SH", "asset_type": "stock", "side": "sell", "qty": 100,
        "price": 9.0, "fee": 5.0, "kind": "fill",
    })
    paper._materialize(tmp_path)

    failed = client.post(f"/api/paper/intents/{intent['id']}/confirm", json={})
    assert failed.status_code == 400
    refreshed = client.get("/api/paper/intents").json()["intents"][0]
    assert refreshed["last_validation_error"]


def test_auto_rule_crud_and_freeze_http(tmp_path):
    client = _client(tmp_path)
    assert client.post("/api/paper/account", json={"initial_cash": 100_000}).status_code == 200
    created = client.post("/api/paper/auto_rules", json={
        "name": "价格跟单",
        "match_kind": "rule",
        "match_id": "rule_source",
        "side": "sell",
        "size_mode": "pct_equity",
        "size_value": 10,
        "order_type": "close",
        "cooldown_days": 3,
    })
    assert created.status_code == 200
    rule_id = created.json()["rule"]["id"]
    assert len(client.get("/api/paper/auto_rules").json()["rules"]) == 1
    disabled = client.post(f"/api/paper/auto_rules/{rule_id}/enabled?enabled=false")
    assert disabled.status_code == 200 and disabled.json()["rule"]["enabled"] is False
    assert client.delete(f"/api/paper/auto_rules/{rule_id}").json() == {"ok": True}
    assert client.get("/api/paper/auto_rules").json()["rules"] == []

    assert client.post("/api/paper/freeze?frozen=true").status_code == 200
    blocked = client.post("/api/paper/orders", json={
        "symbol": "600000.SH", "side": "buy", "qty": 100, "ref_price": 10,
    })
    assert blocked.status_code == 400 and "冻结" in blocked.json()["detail"]
    assert client.post("/api/paper/freeze?frozen=false").status_code == 200
