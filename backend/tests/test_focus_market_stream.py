"""Focus market SSE polling contracts."""
from __future__ import annotations

from datetime import datetime

import polars as pl

from app.services.focus_market_stream import FocusMarketStreamService, _levels


def _minute_frame() -> pl.DataFrame:
    return pl.DataFrame({
        "datetime": [datetime(2026, 9, 1, 9, 31)],
        "open": [10.0],
        "high": [10.2],
        "low": [9.9],
        "close": [10.1],
        "volume": [100],
        "amount": [1010.0],
    })


def _depth_snapshot() -> dict:
    return {
        "bid_prices": [10.0, 9.9],
        "ask_prices": [10.1, 10.2, 10.3, 10.4, 10.5],
        "bid_volumes": [100, 90],
        "ask_volumes": [80, 70, 60, 50, 40],
        "timestamp": 1_757_000_000_000,
    }


def test_levels_are_normalized_to_five_levels():
    assert _levels([1, 2]) == [1, 2, None, None, None]
    assert _levels([1, 2, 3, 4, 5, 6]) == [1, 2, 3, 4, 5]


def test_poll_symbol_combines_latest_minute_and_full_depth(monkeypatch):
    class Repo:
        def resolve_asset_type(self, _symbol):
            return "stock"

    class Depth:
        def get_snapshot(self, symbol):
            assert symbol == "600519.SH"
            return _depth_snapshot()

    from app.services import kline_sync

    monkeypatch.setattr(kline_sync, "fetch_minute_single", lambda *args, **kwargs: _minute_frame())
    monkeypatch.setattr(
        "app.services.focus_market_stream.in_continuous_session",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.focus_market_stream.cn_now",
        lambda: datetime(2026, 9, 1, 10, 0),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider",
        staticmethod(lambda: None),
    )

    service = FocusMarketStreamService(Repo(), Depth())
    payload = service._poll_symbol("600519.SH")

    assert payload is not None
    assert payload["minute"]["ok"] is True
    assert payload["minute"]["row"]["close"] == 10.1
    assert payload["depth"]["ok"] is True
    assert payload["depth"]["snapshot"]["bid_prices"] == [10.0, 9.9, None, None, None]
    assert payload["depth"]["snapshot"]["ask_prices"] == [10.1, 10.2, 10.3, 10.4, 10.5]

    assert service._poll_symbol("600519.SH") is None


def test_poll_symbol_keeps_other_stream_when_one_source_fails(monkeypatch):
    class Repo:
        def resolve_asset_type(self, _symbol):
            raise RuntimeError("asset lookup failed")

    class Depth:
        def get_snapshot(self, _symbol):
            return _depth_snapshot()

    monkeypatch.setattr(
        "app.services.focus_market_stream.in_continuous_session",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.focus_market_stream.cn_now",
        lambda: datetime(2026, 9, 1, 10, 0),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider",
        staticmethod(lambda: None),
    )

    service = FocusMarketStreamService(Repo(), Depth())
    payload = service._poll_symbol("600519.SH")

    assert payload is not None
    assert payload["minute"]["ok"] is False
    assert payload["depth"]["ok"] is True


def test_initial_status_reuses_last_depth_snapshot():
    service = FocusMarketStreamService(object(), object())
    service._last_depth["600519.SH"] = _depth_snapshot()

    status = service.initial_status("600519.SH")

    assert status["depth"]["ok"] is True
    assert status["depth"]["snapshot"]["bid_prices"] == [10.0, 9.9]


def test_off_session_prime_publishes_last_available_depth(monkeypatch):
    class Depth:
        def get_snapshot(self, symbol):
            assert symbol == "600519.SH"
            return _depth_snapshot()

    monkeypatch.setattr(
        "app.services.focus_market_stream.cn_now",
        lambda: datetime(2026, 9, 1, 12, 0),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider",
        staticmethod(lambda: None),
    )

    service = FocusMarketStreamService(object(), Depth())
    payload = service._poll_depth_only("600519.SH")

    assert payload["market_open"] is False
    assert payload["depth"]["ok"] is True
    assert service.initial_status("600519.SH")["depth"]["snapshot"] is not None


def test_poll_symbol_includes_transactions(monkeypatch):
    class Repo:
        def resolve_asset_type(self, _symbol):
            raise RuntimeError("minute not needed")

    class Depth:
        def get_snapshot(self, _symbol):
            return _depth_snapshot()

    class Transactions:
        def get_transactions(self, symbol, limit):
            assert symbol == "600519.SH"
            assert limit == 800
            return [{"time": "10:01", "price": 10.1, "volume": 20, "direction": "buy"}]

    monkeypatch.setattr(
        "app.services.focus_market_stream.in_continuous_session",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.focus_market_stream.cn_now",
        lambda: datetime(2026, 9, 1, 10, 0),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider",
        staticmethod(lambda: Transactions()),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider_name",
        staticmethod(lambda: "mootdx"),
    )

    payload = FocusMarketStreamService(Repo(), Depth())._poll_symbol("600519.SH")

    assert payload["transactions"]["ok"] is True
    assert payload["transactions"]["rows"][0]["direction"] == "buy"


def test_empty_transactions_report_no_data(monkeypatch):
    class Repo:
        def resolve_asset_type(self, _symbol):
            raise RuntimeError("minute not needed")

    class Depth:
        def get_snapshot(self, _symbol):
            return _depth_snapshot()

    class Transactions:
        def get_transactions(self, _symbol, limit):
            assert limit == 800
            return []

    monkeypatch.setattr(
        "app.services.focus_market_stream.in_continuous_session",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.focus_market_stream.cn_now",
        lambda: datetime(2026, 9, 1, 10, 0),
    )
    monkeypatch.setattr(
        FocusMarketStreamService,
        "_transactions_provider",
        staticmethod(lambda: Transactions()),
    )

    payload = FocusMarketStreamService(Repo(), Depth())._poll_symbol("600519.SH")

    assert payload["transactions"]["ok"] is False
    assert payload["transactions"]["error"] == "暂无分时成交"


def test_subscribers_share_one_polling_task():
    import asyncio

    async def scenario():
        service = FocusMarketStreamService(object(), object())
        first = service.subscribe("600519.SH")
        second = service.subscribe("600519.SH")
        assert first is not second
        assert len(service._tasks) == 1
        service.unsubscribe("600519.SH", first)
        assert len(service._tasks) == 1
        service.unsubscribe("600519.SH", second)
        await service.close()
        assert not service._tasks

    asyncio.run(scenario())
