"""个股详情 K 线传输压缩与分钟源能力门控回归测试。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, kline
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

_SYMBOL = "600000.SH"
_TRADE_DATE = date(2026, 1, 15)


@pytest.fixture(autouse=True)
def _isolated_settings_and_clock(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: True)
    monkeypatch.setattr("app.services.preferences.get_minute_batch_compress", lambda: True)
    monkeypatch.setattr(kline, "cn_today", lambda: _TRADE_DATE)
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 1, 15, 10, 30))
    monkeypatch.setattr(kline, "in_continuous_session", lambda: True)


def _minute_rows(count: int = 240) -> pl.DataFrame:
    start = datetime(2026, 1, 15, 9, 30)
    return pl.DataFrame(
        {
            "symbol": [_SYMBOL] * count,
            "datetime": [start + timedelta(minutes=i) for i in range(count)],
            "open": [10.0] * count,
            "high": [10.1] * count,
            "low": [9.9] * count,
            "close": [10.05] * count,
            "volume": [1_000.0] * count,
            "amount": [10_050.0] * count,
        }
    )


def _partial_close_rows() -> pl.DataFrame:
    morning = [datetime(2026, 1, 15, 9, 30) + timedelta(minutes=i) for i in range(120)]
    afternoon = [datetime(2026, 1, 15, 13, 0) + timedelta(minutes=i) for i in range(99)]
    rows = _minute_rows(219)
    return rows.with_columns(pl.Series("datetime", morning + afternoon))


def _full_close_rows() -> pl.DataFrame:
    morning = [datetime(2026, 1, 15, 9, 30) + timedelta(minutes=i) for i in range(120)]
    afternoon = [datetime(2026, 1, 15, 13, 0) + timedelta(minutes=i) for i in range(120)]
    rows = _minute_rows(240)
    return rows.with_columns(pl.Series("datetime", morning + afternoon))


class _DetailRepo:
    def __init__(self, minute: pl.DataFrame | None = None, data_dir: Path | None = None) -> None:
        self.minute = minute if minute is not None else _minute_rows()
        self.store = MagicMock()
        self.store.data_dir = data_dir
        self._write_lock = RLock()
        self.rebuild_view = MagicMock()

    def resolve_asset_type(self, symbol: str) -> str:
        return "stock"

    def get_instruments(self) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "symbol": [_SYMBOL],
                "name": ["浦发银行"],
                "total_shares": [1.0],
                "float_shares": [1.0],
            }
        )

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        frame = pl.DataFrame(
            {
                "symbol": [_SYMBOL] * 30,
                "date": [date(2025, 12, 15) + timedelta(days=i) for i in range(30)],
                "open": [10.0] * 30,
                "high": [10.2] * 30,
                "low": [9.8] * 30,
                "close": [10.1] * 30,
                "volume": [1_000.0] * 30,
                "amount": [10_100.0] * 30,
                "ma5": [10.0] * 30,
                "ma10": [10.0] * 30,
                "ma20": [10.0] * 30,
            }
        )
        return frame.select(columns) if columns else frame

    def get_minute(self, symbol, trade_date, asset_type="stock") -> pl.DataFrame:
        return self.minute

    def get_minute_range(self, symbols, start, end, asset_type="stock") -> pl.DataFrame:
        return self.minute


class _IndexRepo:
    def get_index_instruments(self) -> pl.DataFrame:
        return pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})


def _client(repo, capset: CapabilitySet | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(kline.router)
    app.include_router(indices.router)
    app.state.repo = repo
    app.state.capabilities = capset or CapabilitySet()
    app.state.quote_service = None
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "daily_compress", "minute_compress"),
    [
        (f"/api/kline/daily?symbol={_SYMBOL}&days=120", True, False),
        (f"/api/kline/minute?symbol={_SYMBOL}&date={_TRADE_DATE}", False, True),
        (f"/api/kline/minute-range?symbol={_SYMBOL}&days=10", False, True),
    ],
)
def test_detail_kline_responses_use_configured_gzip(
    monkeypatch,
    path,
    daily_compress,
    minute_compress,
):
    monkeypatch.setattr(
        "app.services.preferences.get_daily_batch_compress",
        lambda: daily_compress,
    )
    monkeypatch.setattr(
        "app.services.preferences.get_minute_batch_compress",
        lambda: minute_compress,
    )

    response = _client(_DetailRepo()).get(
        path,
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["symbol"] == _SYMBOL
    plain = _client(_DetailRepo()).get(path, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert response.json() == plain.json()
    assert int(response.headers["content-length"]) < len(plain.content)

    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    monkeypatch.setattr("app.services.preferences.get_minute_batch_compress", lambda: False)
    disabled = _client(_DetailRepo()).get(path, headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in disabled.headers
    assert disabled.json() == plain.json()


def test_minute_range_gzip_wraps_final_aggregated_payload():
    response = _client(_DetailRepo()).get(
        f"/api/kline/minute-range?symbol={_SYMBOL}&days=10&freq=5m",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    payload = response.json()
    assert payload["freq"] == "5m"
    assert len(payload["sessions"][0]["rows"]) == 30


@pytest.mark.parametrize("live", [False, True])
def test_free_tier_skips_tickflow_minute_fallback(monkeypatch, live):
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame())).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE), "live": live},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()


def test_custom_minute_source_remains_available_without_tickflow_capability(monkeypatch):
    provider = MagicMock()
    provider.get_minute.return_value = _minute_rows(1)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame())).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "live"
    provider.get_minute.assert_called_once()
    get_client.assert_not_called()


def test_post_close_partial_local_minutes_refetch_when_last_bar_is_early(monkeypatch):
    provider = MagicMock()
    provider.get_minute.return_value = _full_close_rows()
    write_minute = MagicMock(return_value=240)
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 1, 15, 15, 31))
    monkeypatch.setattr(kline, "in_continuous_session", lambda: False)
    monkeypatch.setattr(kline.kline_sync, "_write_minute_partition", write_minute)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)

    repo = _DetailRepo(_partial_close_rows(), Path("test-data"))
    response = _client(repo).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "live"
    assert len(response.json()["rows"]) == 240
    provider.get_minute.assert_called_once()
    stored = write_minute.call_args.args[0]
    assert stored.filter(pl.col("symbol") == _SYMBOL).height == 240
    assert stored["datetime"].max() >= datetime(2026, 1, 15, 14, 50)
    assert write_minute.call_args.args[1] == Path("test-data/kline_minute")
    repo.rebuild_view.assert_called_once_with("kline_minute")


def test_failed_post_close_refetch_keeps_partial_local_minutes(monkeypatch):
    provider = MagicMock()
    provider.get_minute.return_value = pl.DataFrame()
    monkeypatch.setattr(kline, "cn_now", lambda: datetime(2026, 1, 15, 15, 31))
    monkeypatch.setattr(kline, "in_continuous_session", lambda: False)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)

    response = _client(_DetailRepo(_partial_close_rows())).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "local"
    assert len(response.json()["rows"]) == 219


def test_failed_custom_source_does_not_fall_back_to_unsupported_tickflow(monkeypatch):
    provider = MagicMock()
    provider.get_minute.side_effect = RuntimeError("custom source unavailable")
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "custom")
    monkeypatch.setattr(
        "app.data_providers.custom.provider_has_dataset", lambda name, dataset: True
    )
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    response = _client(_DetailRepo(pl.DataFrame()), capset).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()


def test_pro_tier_keeps_tickflow_minute_fallback(monkeypatch):
    capset = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    tickflow = MagicMock()
    tickflow.klines.batch.side_effect = RuntimeError("upstream unavailable")
    get_client = MagicMock(return_value=tickflow)
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_DetailRepo(pl.DataFrame()), capset).get(
        "/api/kline/minute",
        params={"symbol": _SYMBOL, "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_called_once()


def test_free_tier_skips_tickflow_index_minute_fallback(monkeypatch):
    get_client = MagicMock(side_effect=AssertionError("must not call TickFlow"))
    monkeypatch.setattr("app.services.preferences.get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr("app.services.kline_sync.get_client", get_client)

    response = _client(_IndexRepo()).get(
        "/api/index/minute",
        params={"symbol": "000001.SH", "date": str(_TRADE_DATE)},
    )

    assert response.status_code == 200
    assert response.json()["source"] == "none"
    get_client.assert_not_called()
