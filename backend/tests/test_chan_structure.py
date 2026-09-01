from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from app.services.chan_market_data import build_multi_level_bars
from app.services.chan_structure import (
    IncrementalChanAnalyzer,
    IncrementalMinuteChanAnalyzer,
    analyze_chan,
)


def _rows(prices: list[float]) -> list[dict[str, object]]:
    start = date(2024, 1, 1)
    return [
        {
            "date": (start + timedelta(days=index)).isoformat(),
            "open": price,
            "high": price + 0.4,
            "low": price - 0.4,
            "close": price,
            "volume": 100,
        }
        for index, price in enumerate(prices)
    ]


def _rich_rows() -> list[dict[str, object]]:
    rng = random.Random(0)
    price = 10.0
    prices: list[float] = []
    for _ in range(320):
        price = max(1.0, price + rng.choice((-1, 1)) * rng.uniform(0.2, 1.8))
        prices.append(round(price, 2))
    return _rows(prices)


def _rich_minute_rows(days: int = 16) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rng = random.Random(42)
    price = 10.0
    start_day = date(2024, 1, 2)
    for day_offset in range(days):
        current_day = start_day + timedelta(days=day_offset)
        for session_start in (
            datetime.combine(current_day, datetime.min.time()).replace(hour=9, minute=30),
            datetime.combine(current_day, datetime.min.time()).replace(hour=13),
        ):
            for minute in range(120):
                stamp = session_start + timedelta(minutes=minute)
                open_ = price
                price = max(1.0, price + rng.uniform(-0.12, 0.12))
                rows.append({
                    "date": current_day.isoformat(),
                    "datetime": stamp.isoformat(),
                    "open": round(open_, 4),
                    "high": round(max(open_, price) + 0.03, 4),
                    "low": round(min(open_, price) - 0.03, 4),
                    "close": round(price, 4),
                    "volume": 100,
                })
    return rows


def test_adapter_exposes_only_upstream_czsc_structures() -> None:
    result = analyze_chan(_rich_rows())

    assert result["engine"] == "czsc-python-port"
    assert result["algorithm_source"] == "waditu/czsc@701e480a545004f945bb1721e510ae610ad90c4c"
    assert result["segments_source"] == "unsupported-by-upstream-czsc"
    assert result["segments"] == []
    assert result["summary"]["segments"] == 0
    assert result["summary"]["fractals"] >= 4
    assert result["summary"]["strokes"] >= 3
    assert result["centers"]
    assert {item["type"] for item in result["fractals"]} == {"top", "bottom"}


def test_centers_are_direct_zs_results_from_first_three_bis() -> None:
    result = analyze_chan(_rich_rows())

    for center in result["centers"]:
        start = center["stroke_start"]
        first_three = result["strokes"][start:start + 3]
        assert len(first_three) == 3
        assert center["low"] == round(max(
            min(item["start_price"], item["end_price"])
            for item in first_three
        ), 4)
        assert center["high"] == round(min(
            max(item["start_price"], item["end_price"])
            for item in first_three
        ), 4)


def test_points_are_only_registered_czsc_signals() -> None:
    result = analyze_chan(_rich_rows())
    catalog = {item["class"]: item["name"] for item in result["signal_catalog"]}

    assert result["points"]
    assert all(point["status"] == "confirmed" for point in result["points"])
    assert all(point["signal_name"] == catalog[point["class"]] for point in result["points"])
    assert all(point["confirmed_at"] == point["source_index"] for point in result["points"])
    assert all(point["signal_evidence"]["checks"] for point in result["points"])


def test_annotations_only_use_the_visible_prefix() -> None:
    rows = _rich_rows()
    prefix = rows[:180]
    result = analyze_chan(prefix)
    last_date = str(prefix[-1]["date"])

    for key in ("fractals", "points"):
        assert all(str(item["date"]) <= last_date for item in result[key])
    for key in ("strokes", "centers"):
        assert all(str(item["end_date"]) <= last_date for item in result[key])
    for key in ("fractals", "strokes", "centers", "points"):
        assert all(int(item["confirmed_at"]) < len(prefix) for item in result[key])


def test_incremental_and_batch_adapters_match() -> None:
    rows = _rich_rows()[:220]
    runtime = IncrementalChanAnalyzer()
    for index, row in enumerate(rows):
        assert runtime.update_primary(row, index)

    incremental = runtime.snapshot()
    batch = analyze_chan(rows)

    for key in ("fractals", "strokes", "segments", "centers", "points", "summary"):
        assert incremental[key] == batch[key]


def test_lower_level_is_an_independent_czsc_snapshot() -> None:
    primary = _rich_rows()[:220]
    lower = [
        {**row, "datetime": f"{row['date']}T10:00:00"}
        for row in _rich_rows()[:220]
    ]

    result = analyze_chan(primary, lower)

    assert result["lower_level"] is not None
    assert result["lower_level"]["level"] == "30m"
    assert result["lower_level"]["algorithm_source"] == result["algorithm_source"]
    assert result["lower_level"]["segments"] == []


def test_one_minute_stream_updates_30m_and_daily_like_batch_aggregation() -> None:
    rows = _rich_minute_rows()
    runtime = IncrementalMinuteChanAnalyzer()
    for row in rows:
        assert runtime.update_minute(row)

    actual = runtime.snapshot()
    levels = build_multi_level_bars(rows, (30, "1d"))
    expected_30m = analyze_chan(levels["30"], level="30m")
    expected_daily = analyze_chan(levels["1d"])

    for key in ("fractals", "strokes", "centers", "points", "summary"):
        assert actual["30"] is not None
        assert actual["1d"] is not None
        assert actual["30"][key] == expected_30m[key]
        assert actual["1d"][key] == expected_daily[key]


def test_one_minute_prefix_never_reads_later_minute_values() -> None:
    rows = _rich_minute_rows(days=3)
    prefix_size = 247
    runtime = IncrementalMinuteChanAnalyzer()
    for row in rows[:prefix_size]:
        runtime.update_minute(row)

    actual = runtime.snapshot()
    levels = build_multi_level_bars(rows[:prefix_size], (30, "1d"))

    for level, interval in (("30", "30m"), ("1d", "1d")):
        expected = analyze_chan(levels[level], level=interval)
        assert actual[level] is not None
        assert actual[level]["indicator_cache"] == expected["indicator_cache"]
        assert actual[level]["fractals"] == expected["fractals"]
        assert actual[level]["strokes"] == expected["strokes"]
        assert actual[level]["points"] == expected["points"]
