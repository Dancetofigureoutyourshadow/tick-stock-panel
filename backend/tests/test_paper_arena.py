"""对比批量创建 + compare 对比增量字段 契约测试。

镜像 test_paper_api.py: 只挂 paper 路由的裸 FastAPI app; data_dir 用 tmp_path。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.paper import router


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    return TestClient(app)


def test_arena_batch_create_accounts_and_rules(tmp_path: Path):
    """批量创建: 同本金/同费率 N 个账户, 各绑一条启用的自动跟单规则。"""
    c = _client(tmp_path)
    r = c.post("/api/paper/arena/batch_create", json={
        "initial_cash": 200000,
        "name_prefix": "对比-",
        "sources": [
            {"name": "均线多头", "match_kind": "strategy", "match_id": "sg_macd_golden"},
            {"match_kind": "rule", "match_id": "mrule_high_volume", "side": "sell", "size_value": 20},
        ],
    })
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 2
    assert created[0]["name"] == "对比-均线多头"
    # 未提供 name 的来源回退 match_id
    assert created[1]["name"] == "对比-mrule_high_volume"
    # 账户 id 服务端生成, 合法且互不相同
    ids = [row["account"] for row in created]
    assert all(i.startswith("arena_") for i in ids) and len(set(ids)) == 2

    # 账户同规格: 本金与费用一致
    for row in created:
        acc = c.get(f"/api/paper/account?account={row['account']}").json()["account"]
        assert acc["initial_cash"] == 200000 and acc["status"] == "active"
        assert acc["commission_pct"] > 0  # 默认费用

    # 各账户绑定的规则: 来源1 用默认 (买/10%权益/次日开盘), 来源2 覆盖 side/size
    r0 = c.get(f"/api/paper/auto_rules?account={ids[0]}").json()["rules"]
    assert len(r0) == 1 and r0[0]["enabled"] is True
    assert r0[0]["match_kind"] == "strategy" and r0[0]["match_id"] == "sg_macd_golden"
    assert r0[0]["side"] == "auto"
    assert r0[0]["position_sizing"] == "equal"
    assert r0[0]["max_total_position"] == 100
    assert r0[0]["auto_follow_enabled"] is True and r0[0]["auto_exit_enabled"] is True
    r1 = c.get(f"/api/paper/auto_rules?account={ids[1]}").json()["rules"]
    assert r1[0]["side"] == "sell" and r1[0]["size_value"] == 20
    assert "position_sizing" not in r1[0]

    # compare 聚合出新账户与对比字段
    rows = {r["account"]: r for r in c.get("/api/paper/compare").json()["accounts"]}
    assert set(ids) <= set(rows)
    row = rows[ids[0]]
    assert row["holdings_count"] == 0 and row["last_nav_date"] is None and row["day_change_pct"] is None
    assert row["auto_enabled"] == 1
    assert row["auto_rules"][0]["match_id"] == "sg_macd_golden" and row["auto_rules"][0]["enabled"] is True


def test_single_and_batch_account_config_are_identical(tmp_path: Path):
    c = _client(tmp_path)
    settings = {
        "initial_cash": 250000,
        "commission_pct": 0.0003,
        "stamp_tax_pct": 0.0008,
        "slippage_bps": 7,
        "queue_limit_orders": True,
    }
    single = c.post("/api/paper/account", json={**settings, "account_id": "single"})
    batch = c.post("/api/paper/arena/batch_create", json={
        **settings,
        "sources": [{"match_kind": "strategy", "match_id": "sg_macd_golden"}],
    })
    assert single.status_code == 200, single.text
    assert batch.status_code == 200, batch.text

    single_account = c.get("/api/paper/account?account=single").json()["account"]
    batch_id = batch.json()["created"][0]["account"]
    batch_account = c.get(f"/api/paper/account?account={batch_id}").json()["account"]
    for key in ("initial_cash", "commission_pct", "stamp_tax_pct", "slippage_bps", "queue_limit_orders"):
        assert single_account[key] == batch_account[key] == settings[key]
    assert single_account["queue_limit_orders"] is True
    assert batch_account["queue_limit_orders"] is True
    assert single_account["status"] == batch_account["status"] == "active"


def test_arena_strategy_uses_same_follow_configuration_as_single_rule(tmp_path: Path):
    c = _client(tmp_path)
    response = c.post("/api/paper/arena/batch_create", json={
        "initial_cash": 150000,
        "commission_pct": 0.0003,
        "stamp_tax_pct": 0.0005,
        "slippage_bps": 8,
        "sources": [{
            "match_kind": "strategy", "match_id": "demo", "position_sizing": "score_weight",
            "max_positions": 3, "max_total_position": 60,
            "entry_fill": "close_t", "exit_fill": "open_t+1",
        }],
    })
    assert response.status_code == 200, response.text
    account_id = response.json()["created"][0]["account"]
    rule = c.get(f"/api/paper/auto_rules?account={account_id}").json()["rules"][0]
    assert rule["side"] == "auto"
    assert rule["position_sizing"] == "score_weight"
    assert rule["max_positions"] == 3
    assert rule["max_total_position"] == 60
    assert rule["entry_fill"] == "close_t" and rule["exit_fill"] == "open_t+1"
    assert rule["auto_follow_enabled"] is True and rule["auto_exit_enabled"] is True
    account = c.get(f"/api/paper/account?account={account_id}").json()["account"]
    assert account["initial_cash"] == 150000
    assert account["commission_pct"] == 0.0003
    assert account["stamp_tax_pct"] == 0.0005
    assert account["slippage_bps"] == 8


def test_single_and_batch_account_config_reject_same_invalid_fee(tmp_path: Path):
    c = _client(tmp_path)
    settings = {"initial_cash": 100000, "commission_pct": 0.02}
    single = c.post("/api/paper/account", json={**settings, "account_id": "single"})
    batch = c.post("/api/paper/arena/batch_create", json={
        **settings,
        "sources": [{"match_kind": "strategy", "match_id": "sg_macd_golden"}],
    })
    assert single.status_code == batch.status_code == 400
    assert single.json()["detail"] == batch.json()["detail"]
    assert c.get("/api/paper/compare").json()["accounts"] == []


def test_arena_batch_create_validates_before_writing(tmp_path: Path):
    """任一来源非法 → 400 且不产生任何账户/规则 (fail-closed, 无半创建)。"""
    c = _client(tmp_path)
    for bad_sources in (
        [{"match_kind": "strategy", "match_id": ""}],                      # match_id 为空
        [{"match_kind": "nope", "match_id": "x"}],                         # match_kind 非法
        [{"match_kind": "strategy", "match_id": "x", "max_total_position": 110}],   # 总仓位超限
        [{"match_kind": "strategy", "match_id": "x", "max_positions": 0}],   # 不能回退默认值
        [{"match_kind": "strategy", "match_id": "x", "max_total_position": 0}],
    ):
        r = c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": bad_sources})
        assert r.status_code == 400, bad_sources
    # 非法请求不应留下任何账户 (compare 只看已初始化账户)
    assert c.get("/api/paper/compare").json()["accounts"] == []

    # 合法来源 + 非法本金同样整体拒绝
    r = c.post("/api/paper/arena/batch_create", json={
        "initial_cash": 0,
        "sources": [{"match_kind": "strategy", "match_id": "x"}],
    })
    assert r.status_code == 400
    assert c.get("/api/paper/compare").json()["accounts"] == []


def test_arena_batch_create_bounds(tmp_path: Path):
    """空来源 / 超过 20 个来源 → 400。"""
    c = _client(tmp_path)
    assert c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": []}).status_code == 400
    many = [{"match_kind": "strategy", "match_id": f"s{i}"} for i in range(21)]
    assert c.post("/api/paper/arena/batch_create", json={"initial_cash": 100000, "sources": many}).status_code == 400


def test_compare_arena_fields_with_nav(tmp_path: Path):
    """day_change_pct / last_nav_date 按定版净值序列末两点计算。"""
    c = _client(tmp_path)
    c.post("/api/paper/account", json={"initial_cash": 100000, "name": "甲"})
    nav_dir = tmp_path / "paper" / "accounts" / "default" / "nav"
    nav_dir.mkdir(parents=True, exist_ok=True)
    with (nav_dir / "daily.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"date": "2026-09-30", "nav": 100000.0}) + "\n")
        f.write(json.dumps({"date": "2026-10-04", "nav": 101500.0}) + "\n")
    row = c.get("/api/paper/compare").json()["accounts"][0]
    assert row["last_nav_date"] == "2026-10-04"
    assert row["day_change_pct"] == 1.5  # (101500/100000 - 1) * 100
