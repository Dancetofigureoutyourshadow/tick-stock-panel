"""Contract tests for the MooTDX Provider plugin."""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from types import ModuleType

import polars as pl
import pytest

from app.plugins.mootdx import bridge
from app.plugins.mootdx import provider as mootdx_provider
from app.plugins.mootdx.provider import MooTdxProvider


class FakeMooTdx:
    def __init__(self):
        self.calls = []

    def k(self, **kwargs):
        self.calls.append(("k", kwargs))
        return pl.DataFrame([{
            "datetime": datetime(2026, 8, 3), "open": 100, "high": 101,
            "low": 99, "close": 100.5, "vol": 10, "amount": 100500,
        }])

    def minutes(self, **kwargs):
        self.calls.append(("minutes", kwargs))
        return pl.DataFrame([{"price": 100.0, "vol": 10}, {"price": 100.5, "vol": 20}])

    def quotes(self, **kwargs):
        self.calls.append(("quotes", kwargs))
        return pl.DataFrame([{
            "last_price": 100.5, "last_close": 99.5, "symbol": "600519",
            "open": 100.0, "high": 101.0, "low": 99.0, "vol": 20,
            "bid1": 100.4, "bid2": 100.3, "bid3": 100.2, "bid4": 100.1, "bid5": 100.0,
            "ask1": 100.6, "ask2": 100.7, "ask3": 100.8, "ask4": 100.9, "ask5": 101.0,
            "bid_vol1": 10, "bid_vol2": 20, "bid_vol3": 30, "bid_vol4": 40, "bid_vol5": 50,
            "ask_vol1": 0, "ask_vol2": 2, "ask_vol3": 3, "ask_vol4": 4, "ask_vol5": 5,
        }])

    def transaction(self, **kwargs):
        self.calls.append(("transaction", kwargs))
        return pl.DataFrame([
            {"time": "10:01", "price": 100.5, "vol": 12, "num": 3, "buyorsell": 0},
            {"time": "10:02", "price": 100.4, "vol": 8, "num": 2, "buyorsell": 1},
        ])

    def transactions(self, **kwargs):
        self.calls.append(("transactions", kwargs))
        return pl.DataFrame([
            {"time": "10:01", "price": 100.5, "vol": 12, "num": 3, "buyorsell": 0},
            {"time": "10:02", "price": 100.4, "vol": 8, "num": 2, "buyorsell": 1},
        ])

    def stock_all(self):
        return pl.DataFrame([{"code": "600519", "name": "贵州茅台"}])

    def finance(self, **kwargs):
        return pl.DataFrame([{"eps": 10.0}])

    def close(self):
        pass


class FakeAdjFactorMooTdx:
    def __init__(self):
        self.calls = []

    def xdxr(self, **kwargs):
        self.calls.append(("xdxr", kwargs))
        return pl.DataFrame([
            {
                "year": 2026, "month": 6, "day": 10, "category": 1,
                "fenhong": 2.0, "peigu": 1.0, "peigujia": 5.0,
                "songzhuangu": 2.0,
            },
            {
                "year": 2026, "month": 7, "day": 1, "category": 5,
                "fenhong": 0.0, "peigu": 0.0, "peigujia": 0.0,
                "songzhuangu": 0.0,
            },
            {
                "year": 2026, "month": 8, "day": 3, "category": 1,
                "fenhong": 1.0, "peigu": 0.0, "peigujia": 0.0,
                "songzhuangu": 0.0,
            },
        ])

    def k(self, **kwargs):
        self.calls.append(("k", kwargs))
        return pl.DataFrame([
            {"datetime": datetime(2026, 6, 9), "close": 10.0},
            {"datetime": datetime(2026, 6, 10), "close": 8.0},
            {"datetime": datetime(2026, 7, 31), "close": 20.0},
            {"datetime": datetime(2026, 8, 3), "close": 19.9},
        ])

    def close(self):
        pass


def test_tdx_client_accepts_recent_trading_day_on_weekend(monkeypatch):
    """Weekend probes must not require a nonexistent same-day minute frame."""
    class WeekendClient:
        def __init__(self):
            self.minute_dates: list[str] = []
            self.closed = False

        def minute(self, **kwargs):
            raise AssertionError("same-day minute probe should be skipped on weekends")

        def bars(self, **kwargs):
            return pl.DataFrame([{"datetime": datetime(2026, 8, 28), "close": 100.0}])

        def minutes(self, **kwargs):
            self.minute_dates.append(kwargs["date"])
            if kwargs["date"] == "20260828":
                return pl.DataFrame([{"price": 100.0}])
            return pl.DataFrame()

        def close(self):
            self.closed = True

    client = WeekendClient()
    quotes_module = ModuleType("mootdx.quotes")

    class Quotes:
        factory = staticmethod(lambda *_args, **_kwargs: client)

    quotes_module.Quotes = Quotes
    mootdx_module = ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)
    monkeypatch.setattr(bridge, "_TDX_SERVERS", [("127.0.0.1", 7709)])
    monkeypatch.setattr(bridge, "_probe", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bridge, "cn_today", lambda: date(2026, 8, 29), raising=False)

    assert bridge.tdx_client() is client
    assert client.minute_dates == ["20260828"]
    assert client.closed is False


def test_tdx_client_skips_minute_only_server(monkeypatch):
    class ProbeClient:
        def __init__(self, daily_rows):
            self.daily_rows = daily_rows
            self.closed = False

        def bars(self, **kwargs):
            return pl.DataFrame(self.daily_rows)

        def minutes(self, **kwargs):
            return pl.DataFrame([{"price": 100.0}])

        def close(self):
            self.closed = True

    minute_only = ProbeClient([])
    full = ProbeClient([{"datetime": datetime(2026, 8, 28), "close": 100.0}])
    clients = iter([minute_only, full])
    quotes_module = ModuleType("mootdx.quotes")

    class Quotes:
        factory = staticmethod(lambda *_args, **_kwargs: next(clients))

    quotes_module.Quotes = Quotes
    mootdx_module = ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)
    monkeypatch.setattr(bridge, "_TDX_SERVERS", [("minute-only", 7709), ("full", 7709)])
    monkeypatch.setattr(bridge, "_probe", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(bridge, "cn_today", lambda: date(2026, 8, 29), raising=False)

    assert bridge.tdx_client() is full
    assert minute_only.closed is True
    assert full.closed is False


def test_tdx_client_quote_validation_uses_only_quote_endpoint(monkeypatch):
    class QuoteClient:
        def __init__(self):
            self.closed = False

        def quotes(self, **kwargs):
            assert kwargs == {"symbol": ["600519"]}
            return pl.DataFrame([{"symbol": "600519", "price": 100.0}])

        def bars(self, **kwargs):
            raise AssertionError("quote validation must not probe daily data")

        def minute(self, **kwargs):
            raise AssertionError("quote validation must not probe same-day minutes")

        def minutes(self, **kwargs):
            raise AssertionError("quote validation must not probe historical minutes")

        def close(self):
            self.closed = True

    client = QuoteClient()
    factory_kwargs = []
    quotes_module = ModuleType("mootdx.quotes")

    class Quotes:
        @staticmethod
        def factory(*_args, **kwargs):
            factory_kwargs.append(kwargs)
            return client

    quotes_module.Quotes = Quotes
    mootdx_module = ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)
    monkeypatch.setattr(bridge, "_TDX_SERVERS", [("quote-node", 7709)])
    monkeypatch.setattr(bridge, "_probe", lambda *_args, **_kwargs: True)

    assert bridge.tdx_client(timeout=3, validation="quote") is client
    assert factory_kwargs == [{
        "market": "std", "server": ("quote-node", 7709), "timeout": 3,
    }]
    assert client.closed is False


def test_tdx_client_quote_validation_has_bounded_server_attempts(monkeypatch):
    class EmptyQuoteClient:
        def quotes(self, **_kwargs):
            return pl.DataFrame()

        def close(self):
            pass

    attempted = []
    quotes_module = ModuleType("mootdx.quotes")

    class Quotes:
        @staticmethod
        def factory(*_args, **kwargs):
            attempted.append(kwargs["server"])
            return EmptyQuoteClient()

    quotes_module.Quotes = Quotes
    mootdx_module = ModuleType("mootdx")
    mootdx_module.quotes = quotes_module
    monkeypatch.setitem(sys.modules, "mootdx", mootdx_module)
    monkeypatch.setitem(sys.modules, "mootdx.quotes", quotes_module)
    servers = [(f"quote-node-{i}", 7709) for i in range(20)]
    monkeypatch.setattr(bridge, "_TDX_SERVERS", servers)
    monkeypatch.setattr(bridge, "_probe", lambda *_args, **_kwargs: True)

    with pytest.raises(RuntimeError):
        bridge.tdx_client(timeout=3, validation="quote")

    assert attempted == servers[:bridge._QUOTE_SERVER_ATTEMPTS]


def test_mootdx_provider_adapts_native_surface(monkeypatch):
    client = FakeMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    provider = MooTdxProvider()

    daily = provider.get_daily(["600519.SH"], datetime(2026, 8, 3), datetime(2026, 8, 3))
    minute = provider.get_minute(
        ["600519.SH"], datetime(2026, 8, 3, 9, 30), datetime(2026, 8, 3, 9, 31)
    )
    realtime = provider.get_realtime(symbols=["600519.SH"])
    instruments = provider.get_instruments()
    financial = provider.get_financials("income", ["600519.SH"])

    assert daily["close"].to_list() == [100.5]
    assert minute["datetime"].to_list() == [
        datetime(2026, 8, 3, 9, 30),
        datetime(2026, 8, 3, 9, 31),
    ]
    assert minute["amount"].to_list() == [100000.0, 201000.0]
    assert realtime[0]["symbol"] == "600519.SH"
    assert realtime[0]["last_price"] == 100.5
    assert realtime[0]["prev_close"] == 99.5
    assert realtime[0]["volume"] == 2000.0
    assert instruments["symbol"].to_list() == ["600519.SH"]
    assert financial["table"].to_list() == ["income"]
    assert [name for name, _ in client.calls] == ["k", "minutes", "quotes"]

    all_realtime = provider.get_realtime()
    assert all_realtime[0]["symbol"] == "600519.SH"


def test_mootdx_full_minute_batch_uses_current_day_and_canonical_rows(monkeypatch):
    from app.plugins.mootdx import batch_scheduler

    clients = []

    class BatchClient(FakeMooTdx):
        def __init__(self):
            super().__init__()
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: BatchClient())
    monkeypatch.setattr(
        batch_scheduler,
        "detect_resources",
        lambda: batch_scheduler.ResourceProfile(
            logical_cpus=1,
            available_memory_bytes=256 * 1024 * 1024,
            gpu_count=1,
        ),
    )
    monkeypatch.setattr(mootdx_provider, "cn_today", lambda: date(2026, 8, 3))

    frame = MooTdxProvider().get_intraday_batch(["600519.SH", "000001.SZ"])

    assert frame.columns == [
        "symbol", "datetime", "open", "high", "low", "close", "volume", "amount",
    ]
    assert frame["symbol"].n_unique() == 2
    assert frame["datetime"].min() == datetime(2026, 8, 3, 9, 30)
    assert frame["amount"].to_list() == [100000.0, 201000.0, 100000.0, 201000.0]
    minute_calls = [
        kwargs
        for client in clients
        for name, kwargs in client.calls
        if name == "minutes"
    ]
    assert sorted(call["symbol"] for call in minute_calls) == ["000001", "600519"]
    assert {call["date"] for call in minute_calls} == {"20260803"}
    assert clients and all(client.closed for client in clients)


def test_mootdx_manifest_and_provider_declare_full_minute():
    manifest = mootdx_provider.__file__.replace("provider.py", "plugin.yaml")

    assert "full_minute" in Path(manifest).read_text(encoding="utf-8")
    assert "full_minute" in MooTdxProvider().config.datasets


def test_mootdx_realtime_skips_codes_tdxpy_cannot_classify(monkeypatch):
    class MixedCodeClient(FakeMooTdx):
        def stock_all(self):
            return pl.DataFrame([
                {"code": "600519"},  # SH A stock
                {"code": "000001"},  # SZ A stock
                {"code": "159915"},  # SZ fund
                {"code": "510300"},  # SH fund
                {"code": "430001"},  # unsupported special security
                {"code": "830001"},  # unsupported special security
            ])

    client = MixedCodeClient()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)

    assert MooTdxProvider().get_realtime()
    quote_calls = [kwargs["symbol"] for name, kwargs in client.calls if name == "quotes"]
    assert quote_calls == [["600519", "000001", "159915", "510300"]]


def test_mootdx_depth5_adapter_maps_all_levels(monkeypatch):
    client = FakeMooTdx()
    client_requests = []

    def make_client(**kwargs):
        client_requests.append(kwargs)
        return client

    monkeypatch.setattr(bridge, "tdx_client", make_client)

    snapshots = MooTdxProvider().get_depth5(["600519.SH"])

    assert client_requests == [{"timeout": 3, "validation": "quote"}]
    assert snapshots["600519.SH"] == {
        "bid_prices": [100.4, 100.3, 100.2, 100.1, 100.0],
        "ask_prices": [100.6, 100.7, 100.8, 100.9, 101.0],
        "bid_volumes": [10.0, 20.0, 30.0, 40.0, 50.0],
        "ask_volumes": [0.0, 2.0, 3.0, 4.0, 5.0],
        "timestamp": None,
    }


def test_mootdx_transactions_adapter_normalizes_rows(monkeypatch):
    client = FakeMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    monkeypatch.setattr(mootdx_provider, "in_continuous_session", lambda: True)

    rows = MooTdxProvider().get_transactions("600519.SH", limit=20)

    assert rows == [
        {
            "time": "10:01", "price": 100.5, "volume": 12.0,
            "trade_count": 3.0, "direction": "buy", "direction_code": 0,
        },
        {
            "time": "10:02", "price": 100.4, "volume": 8.0,
            "trade_count": 2.0, "direction": "sell", "direction_code": 1,
        },
    ]


def test_mootdx_transactions_adapter_uses_today_history_off_session(monkeypatch):
    client = FakeMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    monkeypatch.setattr(mootdx_provider, "in_continuous_session", lambda: False)
    monkeypatch.setattr(mootdx_provider, "cn_today", lambda: date(2026, 9, 1))

    rows = MooTdxProvider().get_transactions("600519.SH", limit=20)

    assert rows[0]["time"] == "10:01"
    assert client.calls == [
        (
            "transactions",
            {"symbol": "600519", "start": 0, "offset": 20, "date": "20260901"},
        ),
    ]


def test_mootdx_transactions_adapter_returns_empty_when_today_history_is_empty(monkeypatch):
    class EmptyHistoryMooTdx(FakeMooTdx):
        def transactions(self, **kwargs):
            self.calls.append(("transactions", kwargs))
            return pl.DataFrame()

    client = EmptyHistoryMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    monkeypatch.setattr(mootdx_provider, "in_continuous_session", lambda: False)
    monkeypatch.setattr(mootdx_provider, "cn_today", lambda: date(2026, 9, 1))

    assert MooTdxProvider().get_transactions("600519.SH", limit=20) == []


def test_mootdx_provider_does_not_propagate_tdxpy_logs(monkeypatch):
    tdxpy_logger = logging.getLogger("tdxpy")
    monkeypatch.setattr(tdxpy_logger, "propagate", True)

    MooTdxProvider()

    assert tdxpy_logger.propagate is False


def test_mootdx_converts_xdxr_events_to_project_adj_factors(monkeypatch):
    client = FakeAdjFactorMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    provider = MooTdxProvider()
    progress = []

    factors = provider.get_adj_factors(
        ["600519.SH"],
        datetime(2026, 6, 1),
        datetime(2026, 7, 31),
        on_chunk_done=lambda current, total: progress.append((current, total)),
    )

    assert provider.capabilities.adj_factor is True
    assert "adj_factor" in provider.config.datasets
    assert factors.columns == ["symbol", "trade_date", "ex_factor"]
    assert factors["symbol"].to_list() == ["600519.SH"]
    assert factors["trade_date"].to_list() == [date(2026, 6, 10)]
    # 理论除权价=(10*10-2+1*5)/(10+1+2)=103/13; 事件因子=10/(103/13)。
    assert abs(factors["ex_factor"][0] - 130 / 103) < 1e-12
    assert progress == [(1, 1)]
    assert [name for name, _kwargs in client.calls] == ["xdxr", "k"]


def test_mootdx_adj_factor_empty_inputs_do_not_fetch(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "tdx_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be created")),
    )

    assert MooTdxProvider().get_adj_factors([], None, None).is_empty()


def test_mootdx_native_methods_are_exposed(monkeypatch):
    client = FakeMooTdx()
    monkeypatch.setattr(bridge, "tdx_client", lambda **_kwargs: client)
    provider = MooTdxProvider()
    for method in ("quotes", "bars", "stock_count", "stocks", "stock_all", "pool", "traffic",
                   "reconnect", "index_bars", "index",
                   "minute", "minutes", "transaction", "transactions", "F10C", "F10",
                   "xdxr", "finance", "k", "get_k_data", "ohlc", "block"):
        assert hasattr(provider, method)


def test_dataframe_instruments_are_accepted_by_instrument_sync(monkeypatch):
    from app.data_providers import custom as custom_sources
    from app.services import instrument_sync, preferences

    class DataFrameProvider:
        def get_instruments(self, asset_type):
            assert asset_type == "stock"
            return pl.DataFrame([{
                "symbol": "600519.SH", "name": "贵州茅台", "code": "600519", "exchange": "SH",
            }])

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "mootdx")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: name == "mootdx")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: DataFrameProvider())

    assert instrument_sync._fetch_instruments_via_provider() == [{
        "symbol": "600519.SH", "name": "贵州茅台", "code": "600519", "exchange": "SH",
        "region": None, "type": None, "listing_date": None, "total_shares": None,
        "float_shares": None, "tick_size": None, "limit_up": None, "limit_down": None,
    }]
