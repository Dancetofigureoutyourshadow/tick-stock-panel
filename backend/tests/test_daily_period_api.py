from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import kline as kline_api
from app.services.kline_period import aggregate_daily_period, full_period_bounds


def _daily_rows(start: date, count: int = 140) -> pl.DataFrame:
    rows = []
    current = start
    index = 0
    while len(rows) < count:
        if current.weekday() < 5:
            rows.append({
                "symbol": "002541.SZ",
                "date": current,
                "open": float(index),
                "high": float(index + 2),
                "low": float(index - 1),
                "close": float(index + 1),
                "volume": 100.0,
                "amount": 1000.0,
            })
            index += 1
        current += timedelta(days=1)
    return pl.DataFrame(rows)


def _request(frame: pl.DataFrame):
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    repo.get_instruments.return_value = pl.DataFrame({
        "symbol": ["002541.SZ"],
        "name": ["测试"],
    })
    repo.get_daily_asset.return_value = frame
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_week_and_month_dates_use_first_actual_trading_day():
    frame = pl.DataFrame({
        "symbol": ["002541.SZ"] * 4,
        "date": [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5)],
        "open": [1.0, 2.0, 3.0, 4.0],
        "high": [2.0, 3.0, 4.0, 5.0],
        "low": [0.0, 1.0, 2.0, 3.0],
        "close": [1.5, 2.5, 3.5, 4.5],
        "volume": [10.0] * 4,
        "amount": [100.0] * 4,
    })

    weekly = aggregate_daily_period(frame, "week")
    monthly = aggregate_daily_period(frame, "month")

    assert weekly["date"].to_list() == [date(2025, 12, 30), date(2026, 1, 5)]
    assert monthly["date"].to_list() == [date(2025, 12, 30), date(2026, 1, 2)]


def test_daily_period_aggregation_uses_ohlcv_and_keeps_full_boundary_period():
    frame = _daily_rows(date(2026, 1, 1), 20)
    result = aggregate_daily_period(frame, "week")

    first = result.row(0, named=True)
    assert first["date"] == date(2026, 1, 1)
    assert first["open"] == 0
    assert first["high"] == 3
    assert first["low"] == -1
    assert first["close"] == 2
    assert first["volume"] == 200
    assert first["amount"] == 2000
    assert full_period_bounds(date(2026, 1, 7), date(2026, 1, 20), "week") == (
        date(2026, 1, 5), date(2026, 1, 25)
    )


def test_daily_api_recomputes_indicators_for_weekly_period():
    result = kline_api.get_daily(
        _request(_daily_rows(date(2025, 1, 1), 400)),
        "002541.SZ",
        start_date="2026-01-01",
        end_date="2026-03-31",
        period="week",
    )

    assert result["period"] == "week"
    assert result["rows"]
    assert "ma5" in result["rows"][-1]
    assert result["rows"][-1]["date"].weekday() < 5


def test_daily_api_default_period_keeps_daily_path(monkeypatch):
    called = MagicMock(return_value={"rows": [], "period": "day"})
    monkeypatch.setattr(kline_api, "_get_daily_impl", called)
    request = SimpleNamespace()

    result = kline_api.get_daily(request, "002541.SZ")

    assert result["period"] == "day"
    called.assert_called_once()


def test_daily_api_rejects_unknown_period():
    with pytest.raises(HTTPException) as error:
        kline_api.get_daily(SimpleNamespace(), "002541.SZ", period="quarter")

    assert error.value.status_code == 400


def test_periodic_daily_does_not_fallback_to_third_party(monkeypatch):
    request = _request(pl.DataFrame())
    fallback = MagicMock(side_effect=AssertionError("third-party daily fetch must not run"))
    monkeypatch.setattr(kline_api.kline_sync, "sync_daily_batch", fallback)

    result = kline_api.get_daily(request, "002541.SZ", period="month")

    assert result["rows"] == []
    assert result["source"] == "none"
    fallback.assert_not_called()
