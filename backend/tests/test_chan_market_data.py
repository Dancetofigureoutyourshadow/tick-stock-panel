from __future__ import annotations

from datetime import datetime, timedelta

from app.services.chan_market_data import (
    ChanBarGenerator,
    build_multi_level_bars,
    validate_kline_quality,
)


def _minute_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for start in (datetime(2024, 1, 2, 9, 30), datetime(2024, 1, 2, 13, 0)):
        for offset in range(120):
            stamp = start + timedelta(minutes=offset)
            rows.append({
                "date": "2024-01-02",
                "datetime": stamp.isoformat(),
                "open": 10 + offset / 100,
                "high": 10.2 + offset / 100,
                "low": 9.8 + offset / 100,
                "close": 10.1 + offset / 100,
                "volume": 1,
            })
    return rows


def test_multi_level_builder_respects_a_share_sessions() -> None:
    levels = build_multi_level_bars(_minute_rows())

    assert {key: len(value) for key, value in levels.items()} == {
        "5": 48,
        "15": 16,
        "30": 8,
        "60": 4,
        "1d": 1,
    }
    assert levels["30"][0]["volume"] == 30
    assert levels["30"][4]["datetime"].startswith("2024-01-02T13:29")


def test_streaming_bar_generator_matches_batch_builder() -> None:
    rows = _minute_rows()
    streaming = ChanBarGenerator()
    for row in rows:
        streaming.update(row)

    assert streaming.bars == build_multi_level_bars(rows)


def test_streaming_bar_keeps_stable_period_identity_while_ohlcv_changes() -> None:
    rows = _minute_rows()[:3]
    streaming = ChanBarGenerator((30, "1d"))

    streaming.update(rows[0])
    minute_identity = streaming.bars["30"][0]["datetime"]
    daily_identity = streaming.bars["1d"][0]["datetime"]
    first_close = streaming.bars["30"][0]["close"]
    streaming.update(rows[1])
    streaming.update(rows[2])

    assert len(streaming.bars["30"]) == 1
    assert minute_identity == "2024-01-02T09:59:00"
    assert daily_identity == "2024-01-02T14:59:00"
    assert streaming.bars["30"][0]["datetime"] == minute_identity
    assert streaming.bars["1d"][0]["datetime"] == daily_identity
    assert streaming.bars["30"][0]["close"] != first_close


def test_quality_report_exposes_errors_and_warnings() -> None:
    rows = _minute_rows()[:2]
    rows.append(dict(rows[-1]))
    rows.append({
        "date": "2024-01-02",
        "datetime": "2024-01-02T12:00:00",
        "open": 11,
        "high": 10,
        "low": 12,
        "close": 11,
        "volume": -1,
    })

    report = validate_kline_quality(rows, intraday=True)
    codes = {item["code"] for item in report["issues"]}

    assert not report["valid"]
    assert {"duplicate_timestamp", "invalid_ohlc", "negative_volume", "off_session"} <= codes
