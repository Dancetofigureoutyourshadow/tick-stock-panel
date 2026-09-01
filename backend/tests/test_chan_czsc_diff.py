from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import chan_czsc_diff


def test_optional_upstream_requirement_uses_the_pinned_git_revision() -> None:
    assert chan_czsc_diff.UPSTREAM_PACKAGE == (
        "czsc @ git+https://github.com/waditu/czsc.git@"
        "701e480a545004f945bb1721e510ae610ad90c4c"
    )


def _edge_rows() -> list[dict[str, object]]:
    rows = [
        {
            "date": "2024-01-01",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.0,
            "raw_close": 10.0,
        },
        {
            "date": "2024-01-02",
            "open": 5.0,
            "high": 5.0,
            "low": 5.0,
            "close": 5.0,
            "raw_close": 10.0,
            "signal_limit_up": True,
        },
        {
            "date": "2024-01-20",
            "open": 5.1,
            "high": 5.5,
            "low": 4.8,
            "close": 5.0,
            "raw_close": 10.0,
            "signal_limit_down": True,
        },
    ]
    rows.extend({
        "date": (date(2024, 1, 20) + timedelta(days=index)).isoformat(),
        "open": 5.0,
        "high": 5.2,
        "low": 4.8,
        "close": 5.0,
        "raw_close": 10.0,
    } for index in range(1, 121))
    return rows


def test_edge_case_detector_uses_adjusted_and_raw_market_fields() -> None:
    detected = chan_czsc_diff.detect_a_share_edge_cases(_edge_rows())

    assert detected["adjustment"][0]["date"] == "2024-01-02"
    assert detected["suspension_gap"][0]["calendar_days"] == 18
    assert detected["limit_up"][0]["date"] == "2024-01-02"
    assert detected["limit_down"][0]["date"] == "2024-01-20"
    assert detected["one_price"][0]["date"] == "2024-01-02"
    assert detected["price_gap"][0]["direction"] == "down"


def test_edge_case_audit_requires_every_category_and_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repo:
        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]})

    rows = _edge_rows()
    monkeypatch.setattr(chan_czsc_diff, "_load_edge_rows", lambda _repo, _symbol: rows)
    monkeypatch.setattr(
        chan_czsc_diff,
        "compare_rows_with_upstream",
        lambda window, symbol, _czsc: {
            "symbol": symbol,
            "passed": True,
            "bars": len(window),
        },
    )

    result = chan_czsc_diff.audit_local_edge_cases(
        Repo(),
        max_symbols=1,
        czsc=SimpleNamespace(),
    )

    assert result["passed"] is True
    assert result["missing_categories"] == []
    assert result["persisted"] is False
    assert set(result["samples"]) == set(chan_czsc_diff.EDGE_CASES)


def test_random_local_audit_is_read_only_and_reports_sample_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repo:
        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]})

        def get_daily_asset(
            self,
            asset_type: str,
            symbol: str,
            _start: date,
            _end: date,
            columns: list[str] | None = None,
        ) -> pl.DataFrame:
            assert asset_type == "stock"
            assert symbol == "000001.SZ"
            assert columns is not None
            return pl.DataFrame([
                {
                    "symbol": symbol,
                    "date": date(2024, 1, 1) + timedelta(days=index),
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "close": 10.0,
                    "volume": 100.0,
                }
                for index in range(130)
            ])

    monkeypatch.setattr(
        chan_czsc_diff,
        "compare_rows_with_upstream",
        lambda rows, symbol, _czsc: {
            "symbol": symbol,
            "passed": True,
            "bars": len(rows),
        },
    )

    result = chan_czsc_diff.compare_random_local_stocks(
        Repo(),
        count=1,
        seed=7,
        czsc=SimpleNamespace(),
    )

    assert result["checked"] == result["passed"] == 1
    assert result["persisted"] is False
    assert result["results"][0]["bars"] == 130


def test_optional_diff_tool_matches_upstream_runtime() -> None:
    czsc = pytest.importorskip("czsc")
    mock = pytest.importorskip("wbt.mock")
    frame = mock.mock_symbol_kline(
        "000001",
        "30分钟",
        sdt="20240101",
        edt="20240115",
        seed=9,
    )
    rows = [{
        "date": item.dt.isoformat(),
        "datetime": item.dt.isoformat(),
        "open": item.open,
        "close": item.close,
        "high": item.high,
        "low": item.low,
        "volume": item.vol,
        "amount": item.amount,
    } for item in frame.itertuples()]

    result = chan_czsc_diff.compare_rows_with_upstream(rows, "000001", czsc)

    assert result["passed"] is True
    assert result["prefixes_checked"] == len(rows)
