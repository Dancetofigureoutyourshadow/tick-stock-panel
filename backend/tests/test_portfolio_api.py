"""持仓账户 HTTP 契约测试。"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import portfolio as portfolio_api
from app.api import watchlist as watchlist_api
from app.config import settings
from app.services.portfolio import PortfolioAccount


class _StrategyEngine:
    def get(self, strategy_id: str):
        if strategy_id != "s1":
            raise ValueError("not found")
        return SimpleNamespace(
            meta={
                "id": "s1",
                "name": "突破策略",
                "version": "2.0",
                "asset_types": ["stock"],
                "timeframes": ["1d"],
                "params": [{"id": "lookback", "default": 20}],
            },
            entry_signals=["signal_entry"],
            exit_signals=["signal_exit"],
            stop_loss=-0.05,
            take_profit=0.1,
            trailing_stop=None,
            trailing_take_profit_activate=None,
            trailing_take_profit_drawdown=None,
            max_hold_days=20,
        )


def test_normalise_portfolio_quote_computes_missing_today_change() -> None:
    quote = portfolio_api._normalise_quote(
        {"prev_close": "10"},
        Decimal("9.5"),
        "realtime",
    )

    assert quote == {
        "prev_close": "10",
        "change_amount": "-0.5",
        "change_pct": -0.05,
        "price_source": "realtime",
    }


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(
        portfolio_api.watchlist,
        "add",
        lambda symbol, _note="", _group_id=None: [{"symbol": symbol}],
    )
    app = FastAPI()
    app.include_router(portfolio_api.router)
    app.include_router(watchlist_api.router)
    app.state.portfolio_account = PortfolioAccount(tmp_path)
    app.state.strategy_engine = _StrategyEngine()
    app.state.quote_service = SimpleNamespace(
        get_quotes_compat=lambda: pl.DataFrame(
            {
                "symbol": ["600001.SH"],
                "last_price": [10.0],
                "prev_close": [9.5],
                "change_amount": [0.5],
                "change_pct": [0.5 / 9.5],
            }
        )
    )
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda _symbol: "stock",
        get_name_map=lambda symbols: {symbol: f"名称{symbol}" for symbol in symbols},
        get_enriched_latest=lambda: (
            pl.DataFrame(
                {
                    "symbol": ["600001.SH", "600002.SH"],
                    "raw_close": [9.8, 20.0],
                    "prev_close": [9.0, 19.0],
                    "change_amount": [0.8, 1.0],
                    "change_pct": [0.8 / 9.0, 1.0 / 19.0],
                }
            ),
            None,
        ),
    )
    return TestClient(app)


def test_portfolio_http_cash_preview_buy_and_positions(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    assert client.post(
        "/api/portfolio/cash", json={"type": "deposit", "amount": "100000"}
    ).status_code == 200
    assert client.put(
        "/api/portfolio/settings",
        json={"max_positions": 4, "commission_rate": "0.001"},
    ).json()["max_positions"] == 4

    preview = client.post(
        "/api/portfolio/buys/preview",
        json={
            "items": [
                {"symbol": "600001.SH", "strategy_id": "s1", "score": "3"},
                {"symbol": "600002.SH", "strategy_id": "s1", "score": "1"},
            ]
        },
    )
    assert preview.status_code == 200
    assert [item["price_source"] for item in preview.json()["items"]] == [
        "realtime",
        "latest_close",
    ]

    bought = client.post(
        "/api/portfolio/buys",
        json={
            "symbol": "600001.SH",
            "strategy_id": "s1",
            "buy_score": "88.5",
            "price": "10",
            "quantity": 1000,
            "trade_date": "2026-09-07",
        },
    )
    assert bought.status_code == 200, bought.text
    snapshot = bought.json()["position"]["strategy_snapshot"]
    assert snapshot["strategy_name"] == "突破策略"
    assert snapshot["params"] == {"lookback": 20}
    assert snapshot["exit_signals"] == ["signal_exit"]

    positions = client.get("/api/portfolio/positions?as_of=2026-09-08").json()["positions"]
    assert positions[0]["available_qty"] == 1000
    assert positions[0]["source_strategy_id"] == "s1"
    assert positions[0]["prev_close"] == "9.5"
    assert positions[0]["change_amount"] == "0.5"
    assert positions[0]["change_pct"] == 0.5 / 9.5
    assert positions[0]["price_source"] == "realtime"

    # 路由输入大小写不应绕过未平仓批次的二次确认保护。
    guarded = client.delete("/api/watchlist/600001.sh")
    assert guarded.status_code == 409
    assert "未平仓批次" in guarded.json()["detail"]
    confirmed = client.delete(
        "/api/watchlist/600001.SH?confirm_open_position=true"
    )
    assert confirmed.status_code == 200
    monkeypatch.setattr(
        watchlist_api.watchlist,
        "list_symbols",
        lambda: [{"symbol": "600002.SH"}],
    )
    assert client.delete("/api/watchlist").status_code == 200
    monkeypatch.setattr(
        watchlist_api.watchlist,
        "list_symbols",
        lambda: [{"symbol": "600001.SH"}],
    )
    assert client.delete("/api/watchlist").status_code == 409
    # 普通自选删除不影响账户批次; 省略 quantity 时默认卖出全部可卖数量。
    sold = client.post(
        f"/api/portfolio/positions/{bought.json()['position']['id']}/sell",
        json={"price": "11", "trade_date": "2026-09-08"},
    )
    assert sold.status_code == 200, sold.text
    assert sold.json()["trade"]["quantity"] == 1000
    assert sold.json()["position"]["status"] == "closed"


def test_portfolio_positions_fall_back_and_fail_closed_for_today_change(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.app.state.quote_service = SimpleNamespace(
        get_quotes_compat=lambda: pl.DataFrame(
            {
                "symbol": ["600001.SH"],
                "last_price": [10.0],
            }
        )
    )
    client.app.state.repo.get_enriched_latest = lambda: (
        pl.DataFrame(
            {
                "symbol": ["600001.SH", "600002.SH"],
                "raw_close": [10.5, 20.0],
                "prev_close": [10.0, None],
            }
        ),
        None,
    )
    client.post("/api/portfolio/cash", json={"type": "deposit", "amount": "100000"})
    bought = client.post(
        "/api/portfolio/buys",
        json={
            "symbol": "600002.SH",
            "strategy_id": "s1",
            "price": "20",
            "quantity": 300,
            "trade_date": "2026-09-07",
        },
    )
    assert bought.status_code == 200, bought.text

    # 600002 has a price but no previous close, so today's change is unknown.
    positions = client.get("/api/portfolio/positions?as_of=2026-09-08").json()["positions"]
    assert positions[0]["price_source"] == "latest_close"
    assert positions[0]["prev_close"] is None
    assert positions[0]["change_amount"] is None
    assert positions[0]["change_pct"] is None


def test_preview_rejects_missing_strategy_basis(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/portfolio/buys/preview",
        json={"items": [{"symbol": "600001.SH", "strategy_id": "missing"}]},
    )
    assert response.status_code == 400
    assert "来源策略" in response.json()["detail"]
