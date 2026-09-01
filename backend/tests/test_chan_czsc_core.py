from __future__ import annotations

import hashlib
import json
import random
import tracemalloc
from dataclasses import replace
from datetime import date, timedelta
from math import isclose, isfinite, isnan, sin
from time import perf_counter

import pytest

from app.services import chan_czsc_core as core
from app.services.chan_structure import IncrementalChanAnalyzer, analyze_chan


def test_algorithm_source_is_pinned_to_reviewed_czsc_commit() -> None:
    assert core.SOURCE_REPOSITORY == "https://github.com/waditu/czsc"
    assert core.SOURCE_REVISION == "701e480a545004f945bb1721e510ae610ad90c4c"
    expected_source = f"waditu/czsc@{core.SOURCE_REVISION}"
    assert expected_source == core.SOURCE_COMMIT


def _raw(index: int, high: float, low: float) -> core._RawBar:
    return core._RawBar(
        source_index=index,
        date=f"2024-01-{index + 1:02d}",
        open=(high + low) / 2,
        close=(high + low) / 2,
        high=high,
        low=low,
        volume=100,
        amount=1000,
    )


def _new(index: int, high: float, low: float) -> core._NewBar:
    return core._NewBar.from_raw(_raw(index, high, low))


def _fx(index: int, mark: str, price: float) -> core._FX:
    return core._FX(
        date=f"2024-01-{index + 1:02d}",
        mark=mark,
        high=price if mark == "top" else price + 1,
        low=price - 1 if mark == "top" else price,
        price=price,
        elements=(),
        source_index=index,
        confirmed_at=index,
    )


def _bi(begin: int, end: int, start: float, finish: float) -> core._BI:
    direction = "up" if finish > start else "down"
    start_mark = "bottom" if direction == "up" else "top"
    end_mark = "top" if direction == "up" else "bottom"
    return core._BI(
        fx_a=_fx(begin, start_mark, start),
        fx_b=_fx(end, end_mark, finish),
        fxs=(),
        direction=direction,
        bars=(),
    )


def test_remove_include_uses_previous_direction() -> None:
    k1 = _new(0, 10, 8)
    k2 = _new(1, 12, 9)
    included, merged = core._remove_include(k1, k2, _raw(2, 13, 8))

    assert included
    assert merged.high == 13
    assert merged.low == 9
    assert merged.date == "2024-01-03"
    assert [bar.source_index for bar in merged.elements] == [1, 2]


def test_repeated_fractal_marks_follow_latest_czsc_filter_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marks = iter(("top", "bottom", "bottom", "top"))

    def fake_check_fx(_k1: core._NewBar, k2: core._NewBar, _k3: core._NewBar) -> core._FX:
        mark = next(marks)
        return core._FX(k2.date, mark, k2.high, k2.low, k2.high, (), 0, 0)

    monkeypatch.setattr(core, "_check_fx", fake_check_fx)
    bars = [_new(index, 10 + index, 8 + index) for index in range(6)]

    fxs = core._check_fxs(bars)

    assert [item.mark for item in fxs] == ["top", "bottom", "top"]


def test_repeated_fractal_mark_is_filtered_after_one_prior_fractal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marks = iter(("top", "top", "bottom"))

    def fake_check_fx(_k1: core._NewBar, k2: core._NewBar, _k3: core._NewBar) -> core._FX:
        mark = next(marks)
        return core._FX(k2.date, mark, k2.high, k2.low, k2.high, (), 0, 0)

    monkeypatch.setattr(core, "_check_fx", fake_check_fx)
    bars = [_new(index, 10 + index, 8 + index) for index in range(5)]

    fxs = core._check_fxs(bars)

    assert [item.mark for item in fxs] == ["top", "bottom"]


def test_signal_macd_uses_talib_warmup_and_unscaled_histogram() -> None:
    bars = [
        _raw(index, 10 + index / 10, 9 + index / 10)
        for index in range(40)
    ]
    analyzer = core._Analyzer(bars)
    dif, hist = analyzer.macd_12_26_maps

    assert isnan(dif[24])
    assert isnan(hist[32])
    assert isfinite(dif[33]) and isfinite(hist[33])

    closes = [bar.close for bar in bars]
    ema12 = sum(closes[14:26]) / 12
    ema26 = sum(closes[:26]) / 26
    difs = [ema12 - ema26]
    for close in closes[26:34]:
        ema12 = (2 * close + ema12 * 11) / 13
        ema26 = (2 * close + ema26 * 25) / 27
        difs.append(ema12 - ema26)
    expected_hist = difs[-1] - sum(difs) / 9
    assert isclose(hist[33], expected_hist)


def test_signal_helpers_match_czsc_nan_and_sma_semantics() -> None:
    values = core._calc_sma_cache_style([1.0, 2.0, 3.0, 4.0], 3)

    assert isnan(values[0]) and isnan(values[1])
    assert values[2:] == [2.0, 3.0]
    assert core._rust_f64_max([float("nan"), 1.0, 3.0]) == 3.0
    assert core._rust_f64_min([float("nan"), 1.0, -2.0]) == -2.0


def test_same_datetime_update_reprocesses_latest_ubi_elements() -> None:
    analyzer = core._Analyzer([
        _raw(0, 10, 8),
        _raw(1, 12, 9),
        _raw(2, 13, 8),
    ])
    replacement = core._RawBar(
        source_index=2,
        date="2024-01-03",
        open=10.5,
        close=10.5,
        high=11,
        low=10,
        volume=200,
        amount=2200,
    )

    analyzer.update(replacement)

    assert analyzer.raw_bars[-1] == replacement
    assert [item.source_index for item in analyzer.bars_ubi[-1].elements] == [1, 2]
    assert analyzer.bars_ubi[-1].elements[-1] == replacement
    assert analyzer.bars_ubi[-1].high == 12
    assert analyzer.bars_ubi[-1].low == 10


def test_same_datetime_update_patches_historical_bi_snapshots_only() -> None:
    old = _raw(2, 13, 8)
    target = core._NewBar.from_raw(old)
    fx = core._FX(
        date=target.date,
        mark="top",
        high=target.high,
        low=target.low,
        price=target.high,
        elements=(target,),
        source_index=old.source_index,
        confirmed_at=old.source_index,
    )
    bi = core._BI(fx_a=fx, fx_b=fx, fxs=(fx,), direction="up", bars=(target,))
    analyzer = core._Analyzer([])
    analyzer.bi_list = [bi]
    replacement = replace(old, close=10.5, high=14, volume=200)

    analyzer._patch_same_datetime_snapshots(target, replacement)

    patched = analyzer.bi_list[0]
    assert patched.bars[0].elements[-1] == replacement
    assert patched.fx_a.elements[0].elements[-1] == replacement
    assert patched.fxs[0].elements[0].elements[-1] == replacement
    assert patched.bars[0].high == target.high
    assert patched.fx_a.high == fx.high


def test_ma_snapshot_uses_historical_close_for_same_bar_id() -> None:
    bars = [_raw(index, 10 + index, 8 + index) for index in range(40)]
    analyzer = core._Analyzer(bars)
    historical = replace(bars[-1], close=bars[-1].close - 7)
    fx_bar = core._NewBar.from_raw(historical)
    fx = core._FX(
        date=historical.date,
        mark="top",
        high=historical.high,
        low=historical.low,
        price=historical.high,
        elements=(fx_bar,),
        source_index=historical.source_index,
        confirmed_at=historical.source_index,
    )
    ma_map = analyzer.sma_map(34)

    value = core._ma_snapshot_at(
        analyzer,
        fx,
        ma_map,
        period=34,
        overrides={},
    )

    expected_closes = [item.close for item in analyzer._signal_raw_bars()]
    expected_closes[-1] = historical.close
    expected = core._calc_sma_cache_style(expected_closes, 34)[-1]
    assert value == expected
    assert value != ma_map[historical.source_index]


def test_raw_bars_follow_upstream_retained_window_after_bi_limit() -> None:
    rng = random.Random(7)
    price = 20.0
    bars: list[core._RawBar] = []
    for index in range(500):
        price = max(1.0, price + rng.choice((-1, 1)) * rng.uniform(0.1, 2.5))
        bars.append(core._RawBar(
            source_index=index,
            date=(date(2020, 1, 1) + timedelta(days=index)).isoformat(),
            open=price,
            close=price,
            high=price + rng.uniform(0.05, 0.8),
            low=max(0.1, price - rng.uniform(0.05, 0.8)),
            volume=100,
            amount=1000,
        ))
    analyzer = core._Analyzer(bars, max_bi_num=1)

    assert len(analyzer.bi_list) == 1
    retained_start = analyzer.bi_list[0].fx_a.elements[0].date
    assert analyzer.raw_bars[0].date == retained_start
    assert analyzer.raw_bars[0].source_index > 0


def test_incremental_auto_source_index_stays_monotonic_after_raw_bar_trim() -> None:
    rng = random.Random(7)
    price = 20.0
    runtime = core.IncrementalStructureAnalyzer(level="1d")
    runtime.analyzer.max_bi_num = 1

    for index in range(500):
        price = max(1.0, price + rng.choice((-1, 1)) * rng.uniform(0.1, 2.5))
        assert runtime.update({
            "date": (date(2020, 1, 1) + timedelta(days=index)).isoformat(),
            "open": price,
            "close": price,
            "high": price + rng.uniform(0.05, 0.8),
            "low": max(0.1, price - rng.uniform(0.05, 0.8)),
            "volume": 100,
            "amount": 1000,
        })

    source_indices = [bar.source_index for bar in runtime.analyzer.raw_bars]
    assert source_indices == sorted(set(source_indices))
    assert source_indices[-1] == 499


def test_incremental_auto_source_index_reuses_same_datetime() -> None:
    runtime = core.IncrementalStructureAnalyzer(level="1d")
    first = {"date": "2024-01-02", "open": 10, "high": 11, "low": 9, "close": 10}
    replacement = {**first, "high": 12, "close": 11}

    assert runtime.update(first)
    assert runtime.update(replacement)
    assert runtime.update({
        "date": "2024-01-03",
        "open": 11,
        "high": 13,
        "low": 10,
        "close": 12,
    })

    assert [bar.source_index for bar in runtime.analyzer.raw_bars] == [0, 1]
    assert runtime.analyzer.raw_bars[0].high == 12


def test_same_datetime_update_replaces_signals_for_that_bar() -> None:
    rng = random.Random(0)
    price = 20.0
    start = date(2020, 1, 1)
    rows: list[dict[str, object]] = []
    for index in range(77):
        price = max(1.0, price + rng.choice((-1, 1)) * rng.uniform(0.05, 2.8))
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": price,
            "high": price + rng.uniform(0.05, 0.8),
            "low": max(0.1, price - rng.uniform(0.05, 0.8)),
            "close": price,
            "volume": 100,
        })

    original = rows[-1]
    replacement = dict(original)
    replacement["high"] = float(original["high"]) * 0.88
    replacement["low"] = min(
        float(original["low"]),
        float(replacement["high"]) - 0.1,
    )
    replacement["close"] = (
        float(replacement["high"]) + float(replacement["low"])
    ) / 2

    runtime = core.IncrementalStructureAnalyzer(level="1d")
    for index, row in enumerate(rows[:-1]):
        assert runtime.update(row, index)
    assert runtime.update(original, 76)
    assert any(
        point["date"] == original["date"] and point["class"] == "second"
        for point in runtime.points
    )
    assert runtime.update(replacement, 76)

    expected = core.analyze_structure([*rows[:-1], replacement], level="1d")
    assert runtime.snapshot(include_replay=True)["points"] == expected["points"]
    assert sum(item["source_index"] == 76 for item in runtime._replay) <= 1


def test_bi_requires_six_normalized_bars() -> None:
    bars = [
        _new(0, 11, 9),
        _new(1, 10, 8),
        _new(2, 11, 9),
        _new(3, 12, 10),
        _new(4, 13, 11),
        _new(5, 12, 10),
    ]

    bi, remainder = core._check_bi(bars, min_bi_len=6)
    too_short, _ = core._check_bi(bars, min_bi_len=7)

    assert bi is not None
    assert bi.direction == "up"
    assert len(bi.bars) == 6
    assert [bar.date for bar in remainder] == [bar.date for bar in bars[3:]]
    assert too_short is None


def test_center_uses_first_three_bis_and_extends_without_expanding_zone() -> None:
    bis = [
        _bi(0, 1, 12, 9),
        _bi(1, 2, 9, 11),
        _bi(2, 3, 11, 9.5),
        _bi(3, 4, 9.5, 10.5),
    ]

    centers = core._build_centers(bis, level="1d")

    assert len(centers) == 1
    assert centers[0]["low"] == 9.5
    assert centers[0]["high"] == 11
    assert centers[0]["formation_end_date"] == "2024-01-04"
    assert centers[0]["end_date"] == "2024-01-05"
    assert centers[0]["kind"] == "extension"
    assert centers[0]["range_low"] == 9
    assert centers[0]["range_high"] == 12


def test_incremental_snapshot_matches_batch_result() -> None:
    prices = [10, 12, 9, 13, 8, 14, 9, 15, 10, 16, 11, 17, 10, 18, 9, 16, 8, 15]
    rows = [
        {
            "date": f"2024-02-{index + 1:02d}",
            "open": price,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 100,
        }
        for index, price in enumerate(prices)
    ]
    runtime = IncrementalChanAnalyzer()
    for index, row in enumerate(rows):
        runtime.update_primary(row, index)

    incremental = runtime.snapshot()
    batch = analyze_chan(rows)

    for key in ("fractals", "strokes", "centers", "points", "summary"):
        assert incremental[key] == batch[key]


def test_incremental_training_performance_budget_for_two_thousand_bars() -> None:
    rows = []
    previous = 100.0
    for index in range(2_000):
        close = 100 + index * 0.002 + sin(index / 7) * 8
        rows.append({
            "date": (date(2018, 1, 1) + timedelta(days=index)).isoformat(),
            "open": previous,
            "high": max(previous, close) + 0.8,
            "low": min(previous, close) - 0.8,
            "close": close,
            "volume": 100_000 + index,
        })
        previous = close

    build_started = perf_counter()
    runtime = IncrementalChanAnalyzer()
    for source_index, row in enumerate(rows[:-60]):
        runtime.update_primary(row, source_index)
    build_elapsed = perf_counter() - build_started

    started = perf_counter()
    longest_update = 0.0
    for source_index, row in enumerate(rows[-60:], start=len(rows) - 60):
        update_started = perf_counter()
        runtime.update_primary(row, source_index)
        runtime.snapshot()
        longest_update = max(longest_update, perf_counter() - update_started)
    elapsed = perf_counter() - started

    # tracemalloc substantially slows allocation-heavy Python code, so memory
    # and wall-clock budgets must be measured in separate complete runs.
    tracemalloc.start()
    memory_runtime = IncrementalChanAnalyzer()
    for source_index, row in enumerate(rows):
        memory_runtime.update_primary(row, source_index)
    memory_runtime.snapshot()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert build_elapsed < 10.0
    assert elapsed < 5.0
    assert longest_update < 0.5
    assert peak < 64 * 1024 * 1024


def test_intraday_rows_keep_each_full_timestamp() -> None:
    rows = [
        {"date": "2024-01-02", "datetime": f"2024-01-02T{hour}:00:00", "open": 10, "high": 11, "low": 9, "close": 10}
        for hour in (10, 11, 14, 15)
    ]

    bars = core._clean_raw_bars(rows)

    assert len(bars) == 4
    assert [bar.date for bar in bars] == [row["datetime"] for row in rows]


def test_signal_catalog_and_replay_expose_source_metadata() -> None:
    rows = [
        {"date": f"2024-03-{index + 1:02d}", "open": price, "high": price + 1, "low": price - 1, "close": price}
        for index, price in enumerate([10, 12, 9, 13, 8, 14, 9, 15, 10, 16])
    ]

    result = core.analyze_structure(rows, level="1d", include_replay=True)

    assert {item["name"] for item in result["signal_catalog"]} == set(core.SIGNAL_PROFILE.values())
    assert all(item["version"].startswith("V") for item in result["signal_catalog"])
    assert result["event_profile"]
    assert result["replay"]
    assert all(item["source_index"] < len(rows) for item in result["replay"])


def test_same_bar_multiple_signal_classes_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = core._Analyzer([])
    analyzer.bi_list = [_bi(0, 1, 12, 9)]

    def result_for(point_class: str):
        return lambda _analyzer: (
            "buy",
            {
                "rule": core.SIGNAL_REGISTRY[point_class].name,
                "stroke_count": 5,
                "strokes": [],
                "indicators": {},
                "checks": [],
            },
        )

    monkeypatch.setattr(core, "_first_bs_signal_result", result_for("first"))
    monkeypatch.setattr(core, "_second_bs_signal_result", result_for("second"))
    monkeypatch.setattr(core, "_third_bs_signal_result", result_for("third"))

    points = core._current_signal_points(analyzer, _raw(2, 11, 8), "1d")

    assert [point["class"] for point in points] == ["first", "second", "third"]
    assert len({point["signal_key"] for point in points}) == 3
    assert all(point["date"] == "2024-01-03" for point in points)


def test_buy_sell_signal_profile_golden_sample() -> None:
    rng = random.Random(2)
    price = 20.0
    start = date(2020, 1, 1)
    rows: list[dict[str, object]] = []
    for index in range(650):
        price = max(1.0, price + rng.choice((-1, 1)) * rng.uniform(0.05, 2.8))
        rows.append({
            "date": (start + timedelta(days=index)).isoformat(),
            "open": price,
            "high": price + rng.uniform(0.05, 0.8),
            "low": max(0.1, price - rng.uniform(0.05, 0.8)),
            "close": price,
            "volume": 100,
        })

    result = core.analyze_structure(rows, level="1d")
    first_by_class = {
        point_class: next(point for point in result["points"] if point["class"] == point_class)
        for point_class in ("first", "second", "third")
    }

    assert (first_by_class["first"]["date"], first_by_class["first"]["type"]) == ("2020-08-10", "sell")
    assert (first_by_class["second"]["date"], first_by_class["second"]["type"]) == ("2020-03-12", "buy")
    assert (first_by_class["third"]["date"], first_by_class["third"]["type"]) == ("2020-04-30", "buy")
    assert {
        point["signal_name"] for point in first_by_class.values()
    } == set(core.SIGNAL_PROFILE.values())
    assert all(
        point["signal_evidence"]["rule"] == point["signal_name"]
        for point in first_by_class.values()
    )
    assert all(
        point["signal_evidence"]["strokes"]
        and point["signal_evidence"]["indicators"]
        and all(check["passed"] for check in point["signal_evidence"]["checks"])
        for point in first_by_class.values()
    )
    assert "center" in first_by_class["first"]["signal_evidence"]
    assert "center" not in first_by_class["second"]["signal_evidence"]
    assert "center" in first_by_class["third"]["signal_evidence"]
    first_index = next(index for index, row in enumerate(rows) if row["date"] == "2020-08-10")
    assert not any(
        point["class"] == "first"
        for point in core.analyze_structure(rows[:first_index], level="1d")["points"]
    )
    assert any(
        point["class"] == "first" and point["date"] == "2020-08-10"
        for point in core.analyze_structure(rows[:first_index + 1], level="1d")["points"]
    )


def test_upstream_seed42_core_golden_sample() -> None:
    mock = pytest.importorskip("wbt.mock")
    frame = mock.mock_symbol_kline(
        "000001",
        "30分钟",
        sdt="20240101",
        edt="20240301",
        seed=42,
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

    analyzer = core._Analyzer(core._clean_raw_bars(rows))

    assert len(rows) == 610
    assert len(analyzer.fx_list) == 175
    assert len(analyzer.bi_list) == 43
    assert [item.mark for item in analyzer.fx_list] == [
        "bottom" if index % 2 == 0 else "top" for index in range(175)
    ]
    assert [item.direction for item in analyzer.bi_list] == [
        "down" if index % 2 == 0 else "up" for index in range(43)
    ]
    assert [len(item.bars) for item in analyzer.bi_list] == [
        8, 8, 7, 15, 7, 19, 7, 7, 11, 8, 6, 17, 6, 9, 8, 21, 8, 12,
        13, 11, 7, 9, 7, 11, 7, 6, 6, 8, 9, 6, 10, 16, 16, 9, 7, 6,
        23, 20, 10, 7, 23, 9, 14,
    ]

    def digest(value: object) -> str:
        payload = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    fractals = [
        [
            item.date,
            "G" if item.mark == "top" else "D",
            item.high,
            item.low,
            item.price,
        ]
        for item in analyzer.fx_list
    ]
    strokes = [
        [
            item.start_date,
            item.end_date,
            "Up" if item.direction == "up" else "Down",
            item.high,
            item.low,
            item.start_price,
            item.end_price,
            len(item.bars),
        ]
        for item in analyzer.bi_list
    ]
    centers = [
        [
            item.bis[0].start_date,
            item.bis[-1].end_date,
            item.zg,
            item.zd,
            item.gg,
            item.dd,
            len(item.bis),
            item.is_valid(),
        ]
        for item in core._get_zs_seq(analyzer.finished_bis)
        if len(item.bis) >= 3
    ]

    assert digest(fractals) == "44efc5241842093aefd0988842061c23e1d65b102711a5c370b971c3f7ea87ff"
    assert digest(strokes) == "065ce196596055b95b9d85671ee251d7973ba8200c035bf7ceda0d74790852aa"
    assert digest(centers) == "8444e0cf40cc6cb895a9ecaeb9004a6731021e51571afba2b26ac81303f9a6ff"

    index_by_date = {str(row["date"]): index for index, row in enumerate(rows)}
    assert all(item.source_index == index_by_date[item.date] for item in analyzer.fx_list)
    assert all(item.confirmed_at >= item.source_index for item in analyzer.fx_list)
    assert all(item.start_index == index_by_date[item.start_date] for item in analyzer.bi_list)
    assert all(item.end_index == index_by_date[item.end_date] for item in analyzer.bi_list)
    assert all(item.confirmed_at >= item.end_index for item in analyzer.bi_list)


def test_optional_upstream_runtime_matches_every_prefix() -> None:
    czsc = pytest.importorskip("czsc")
    mock = pytest.importorskip("wbt.mock")
    frame = mock.mock_symbol_kline(
        "000001",
        "30分钟",
        sdt="20240101",
        edt="20240301",
        seed=42,
    )
    items = list(frame.itertuples())
    upstream_bars = [
        czsc.RawBar(
            "000001",
            item.dt,
            czsc.Freq.F30,
            item.open,
            item.close,
            item.high,
            item.low,
            item.vol,
            item.amount,
            index,
        )
        for index, item in enumerate(items)
    ]
    local_bars = [
        core._RawBar(
            source_index=index,
            date=item.dt.isoformat(),
            open=item.open,
            close=item.close,
            high=item.high,
            low=item.low,
            volume=item.vol,
            amount=item.amount,
        )
        for index, item in enumerate(items)
    ]
    upstream = czsc.CZSC([upstream_bars[0]], 50, 6)
    local = core._Analyzer([local_bars[0]])
    signal_cases = (
        (
            core._first_bs_signal,
            "zdy_macd_bs1_V230422",
            {"di": 1, "th": 50},
            {"看多": "buy", "看空": "sell"},
        ),
        (
            core._second_bs_signal,
            "cxt_second_bs_V230320",
            {"di": 1, "ma_type": "SMA", "timeperiod": 21},
            {"二买": "buy", "二卖": "sell"},
        ),
        (
            core._third_bs_signal,
            "cxt_third_bs_V230318",
            {"di": 1, "ma_type": "SMA", "timeperiod": 34},
            {"三买": "buy", "三卖": "sell"},
        ),
    )

    for index in range(1, len(upstream_bars)):
        upstream.update(upstream_bars[index])
        local.update(local_bars[index])

        upstream_fx = [
            (item.dt.isoformat(), item.mark.name, item.high, item.low, item.fx)
            for item in upstream.fx_list
        ]
        local_fx = [
            (
                item.date,
                "G" if item.mark == "top" else "D",
                item.high,
                item.low,
                item.price,
            )
            for item in local.fx_list
        ]
        assert local_fx == upstream_fx, index

        upstream_bis = [
            (
                item.sdt.isoformat(),
                item.edt.isoformat(),
                item.direction.name,
                item.high,
                item.low,
                item.fx_a.fx,
                item.fx_b.fx,
                item.length,
            )
            for item in upstream.bi_list
        ]
        local_bis = [
            (
                item.start_date,
                item.end_date,
                "Up" if item.direction == "up" else "Down",
                item.high,
                item.low,
                item.start_price,
                item.end_price,
                len(item.bars),
            )
            for item in local.bi_list
        ]
        assert local_bis == upstream_bis, index

        upstream_zs = [
            (
                item.sdt.isoformat(),
                item.edt.isoformat(),
                item.zg,
                item.zd,
                item.gg,
                item.dd,
                len(item.bis),
                item.is_valid(),
            )
            for item in upstream.zs_list
        ]
        local_zs = [
            (
                item.bis[0].start_date,
                item.bis[-1].end_date,
                item.zg,
                item.zd,
                item.gg,
                item.dd,
                len(item.bis),
                item.is_valid(),
            )
            for item in core._get_zs_seq(local.finished_bis)
        ]
        assert local_zs == upstream_zs, index

        for local_signal, name, parameters, value_map in signal_cases:
            upstream_value = czsc._native.call_signal(
                name,
                upstream,
                parameters,
            )[0].v1
            assert local_signal(local) == value_map.get(upstream_value), (index, name)
