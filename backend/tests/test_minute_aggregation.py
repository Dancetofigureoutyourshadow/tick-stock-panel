from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import kline as kline_api
from app.services.kline_sync import aggregate_minute_kline


def _full_session_rows() -> pl.DataFrame:
    rows = []
    index = 0
    for start, count in ((time(9, 30), 120), (time(13, 0), 120)):
        current = datetime.combine(date(2026, 8, 28), start)
        for _ in range(count):
            rows.append({
                "symbol": "002541.SZ",
                "datetime": current,
                "open": float(index),
                "high": float(index) + 2,
                "low": float(index) - 1,
                "close": float(index) + 1,
                "volume": 1.0,
                "amount": 10.0,
            })
            current += timedelta(minutes=1)
            index += 1
    return pl.DataFrame(rows)


@pytest.mark.parametrize(
    ("freq", "expected_rows"),
    [("1m", 240), ("5m", 48), ("15m", 16), ("30m", 8), ("60m", 4), ("120m", 2)],
)
def test_aggregate_minute_kline_returns_expected_bar_count(freq, expected_rows):
    result = aggregate_minute_kline(_full_session_rows(), freq)

    assert result.height == expected_rows


def test_aggregate_minute_kline_uses_ohlcv_and_does_not_cross_lunch():
    result = aggregate_minute_kline(_full_session_rows(), "5m")

    morning = result.row(0, named=True)
    afternoon = result.filter(pl.col("datetime") == datetime(2026, 8, 28, 13, 0)).row(0, named=True)
    assert morning["datetime"] == datetime(2026, 8, 28, 9, 30)
    assert morning["open"] == 0
    assert morning["high"] == 6
    assert morning["low"] == -1
    assert morning["close"] == 5
    assert morning["volume"] == 5
    assert morning["amount"] == 50
    assert afternoon["open"] == 120
    assert afternoon["close"] == 125


def test_aggregate_minute_kline_filters_boundary_rows_and_keeps_partial_bars():
    frame = pl.DataFrame({
        "symbol": ["002541.SZ"] * 5,
        "datetime": [
            datetime(2026, 8, 28, 9, 25),
            datetime(2026, 8, 28, 9, 30),
            datetime(2026, 8, 28, 9, 32),
            datetime(2026, 8, 28, 15, 0),
            datetime(2026, 8, 28, 15, 5),
        ],
        "open": [1, 10, 12, 20, 30],
        "high": [1, 11, 13, 21, 31],
        "low": [1, 9, 11, 19, 29],
        "close": [1, 10, 12, 20, 30],
        "volume": [1, 2, 3, 4, 5],
        "amount": [10, 20, 30, 40, 50],
    })

    result = aggregate_minute_kline(frame, "5m")

    assert result.height == 1
    assert result["datetime"].to_list() == [datetime(2026, 8, 28, 9, 30)]
    assert result["volume"].to_list() == [5]


def _request_with_minute_data(frame: pl.DataFrame):
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    repo.get_instruments.return_value = pl.DataFrame({
        "symbol": ["002541.SZ"],
        "name": ["测试"],
    })
    repo.get_minute_range.return_value = frame
    repo.get_daily_asset.return_value = pl.DataFrame({
        "date": [date(2026, 8, 27)],
        "close": [9.0],
    })
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


def test_minute_range_accepts_frequency_and_returns_aggregated_rows():
    frame = _full_session_rows().filter(pl.col("datetime").dt.date() == date(2026, 8, 28))

    result = kline_api.get_minute_range(
        _request_with_minute_data(frame), "002541.SZ", 1, freq="60m"
    )

    assert result["freq"] == "60m"
    assert len(result["sessions"]) == 1
    assert len(result["sessions"][0]["rows"]) == 4


def test_minute_range_rejects_unknown_frequency():
    with pytest.raises(HTTPException) as error:
        kline_api.get_minute_range(SimpleNamespace(), "002541.SZ", 1, freq="2m")

    assert error.value.status_code == 400
