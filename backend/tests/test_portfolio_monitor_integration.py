"""持仓退出事件复用现有告警落盘、SSE 与系统通知链路。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import polars as pl

from app.services import alert_store, preferences
from app.services.portfolio import PortfolioAccount
from app.services.quote_service import QuoteService


def test_portfolio_exit_is_delivered_without_monitor_rules(tmp_path, monkeypatch) -> None:
    account = PortfolioAccount(tmp_path)
    account.record_cash("deposit", "100000")
    account.buy(
        symbol="600001.SH",
        name="测试股票",
        price="10",
        quantity=100,
        strategy_snapshot={
            "strategy_id": "s1",
            "strategy_name": "测试策略",
            "exit_signals": [],
            "stop_loss": "0.05",
            "webhook_channels": [],
        },
        trade_date="2026-09-07",
    )

    persisted: list[dict] = []
    notified: list[dict] = []
    monkeypatch.setattr(
        alert_store,
        "append_many",
        lambda _data_dir, events: persisted.extend(events),
    )
    monkeypatch.setattr(preferences, "get_system_notify_enabled", lambda: True)

    class _Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        @staticmethod
        def get_instruments():
            return pl.DataFrame({"symbol": ["600001.SH"], "name": ["测试股票"]})

    service = QuoteService()
    subscriber = service.subscribe()
    service.set_app_state(SimpleNamespace(
        monitor_engine=SimpleNamespace(rule_count=0, rules={}),
        portfolio_account=account,
        repo=_Repo(),
    ))
    service._repo = _Repo()
    service.get_enriched_today = lambda: (
        pl.DataFrame({
            "symbol": ["600001.SH"],
            "raw_close": [9.4],
            "raw_high": [10.0],
            "raw_low": [9.4],
            "change_pct": [-0.06],
        }),
        date(2026, 9, 8),
    )
    service._maybe_send_system_notifications = lambda alerts: notified.extend(alerts)

    with (
        patch.object(QuoteService, "_is_continuous_trading", return_value=True),
        patch("app.services.quote_service.cn_today", return_value=date(2026, 9, 8)),
    ):
        service._evaluate_monitors(pl.DataFrame(), None)

    pushed = subscriber.pop()["alerts"]
    assert persisted[0]["source"] == "portfolio"
    assert pushed[0]["position_id"] == persisted[0]["position_id"]
    assert pushed[0]["strategy_id"] == "s1"
    assert notified[0]["exit_reason"] == "stop_loss"
    assert account.positions({}, as_of="2026-09-08")[0]["status"] == "pending_sell"
