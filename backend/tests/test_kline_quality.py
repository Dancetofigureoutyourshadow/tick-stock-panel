from __future__ import annotations

from app.services.kline_quality import validate_kline_quality


def test_valid_daily_rows_pass_quality_check() -> None:
    report = validate_kline_quality([
        {
            "date": "2024-01-02",
            "open": 10,
            "high": 11,
            "low": 9,
            "close": 10.5,
            "volume": 100,
        },
        {
            "date": "2024-01-03",
            "open": 10.5,
            "high": 11.5,
            "low": 10,
            "close": 11,
            "volume": 120,
        },
    ])

    assert report == {
        "valid": True,
        "row_count": 2,
        "error_count": 0,
        "warning_count": 0,
        "issues": [],
    }


def test_invalid_intraday_rows_report_auditable_issue_codes() -> None:
    rows = [
        {
            "datetime": "2024-01-02T12:00:00",
            "open": 11,
            "high": 10,
            "low": 12,
            "close": 11,
            "volume": -1,
        },
        {
            "datetime": "2024-01-02T12:00:00",
            "open": 11,
            "high": 12,
            "low": 10,
            "close": 11,
            "volume": 1,
        },
    ]

    report = validate_kline_quality(rows, intraday=True)
    codes = {item["code"] for item in report["issues"]}

    assert report["valid"] is False
    assert {"duplicate_timestamp", "invalid_ohlc", "negative_volume", "off_session"} <= codes
