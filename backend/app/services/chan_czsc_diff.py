"""Development-only differential checks against the pinned CZSC runtime.

The production application never imports CZSC. Callers must explicitly supply
or install the optional upstream package when running these checks.
"""
from __future__ import annotations

import random
import time
from datetime import date, datetime, timedelta
from typing import Any

from app.services import chan_czsc_core as local_core

UPSTREAM_PACKAGE = (
    f"czsc @ git+{local_core.SOURCE_REPOSITORY}.git@{local_core.SOURCE_REVISION}"
)
EDGE_CASES = (
    "adjustment",
    "suspension_gap",
    "limit_up",
    "limit_down",
    "one_price",
    "price_gap",
)


def _date_key(value: Any) -> str:
    text = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return datetime.fromisoformat(text).isoformat()


def _difference(local: list[Any], upstream: list[Any]) -> dict[str, Any] | None:
    if local == upstream:
        return None
    limit = min(len(local), len(upstream))
    index = next((i for i in range(limit) if local[i] != upstream[i]), limit)
    return {
        "item_index": index,
        "local_count": len(local),
        "upstream_count": len(upstream),
        "local": local[index] if index < len(local) else None,
        "upstream": upstream[index] if index < len(upstream) else None,
    }


def detect_a_share_edge_cases(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Locate auditable market edge cases without changing the price series."""
    found: dict[str, list[dict[str, Any]]] = {key: [] for key in EDGE_CASES}
    previous: dict[str, Any] | None = None
    previous_factor: float | None = None
    for index, row in enumerate(rows):
        try:
            current_date = date.fromisoformat(str(row["date"])[:10])
            open_, high, low, close = (float(row[key]) for key in ("open", "high", "low", "close"))
            raw_close = float(row.get("raw_close") or close)
            factor = close / raw_close if raw_close > 0 else 1.0
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            previous = None
            previous_factor = None
            continue
        base = {"index": index, "date": current_date.isoformat()}
        if previous_factor is not None and abs(factor - previous_factor) > max(1e-8, abs(previous_factor) * 1e-8):
            found["adjustment"].append({
                **base,
                "previous_factor": previous_factor,
                "factor": factor,
            })
        if bool(row.get("signal_limit_up")):
            found["limit_up"].append(base)
        if bool(row.get("signal_limit_down")):
            found["limit_down"].append(base)
        tolerance = max(0.005, abs(close) * 1e-7)
        if max(open_, high, low, close) - min(open_, high, low, close) <= tolerance:
            found["one_price"].append(base)
        if previous is not None:
            previous_date = date.fromisoformat(str(previous["date"])[:10])
            calendar_gap = (current_date - previous_date).days
            if calendar_gap > 10:
                found["suspension_gap"].append({**base, "calendar_days": calendar_gap})
            previous_high = float(previous["high"])
            previous_low = float(previous["low"])
            if low > previous_high + tolerance:
                found["price_gap"].append({
                    **base,
                    "direction": "up",
                    "previous_high": previous_high,
                    "current_low": low,
                })
            elif high < previous_low - tolerance:
                found["price_gap"].append({
                    **base,
                    "direction": "down",
                    "previous_low": previous_low,
                    "current_high": high,
                })
        previous = row
        previous_factor = factor
    return found


def _load_edge_rows(repo: Any, symbol: str) -> list[dict[str, Any]]:
    frame = repo.get_daily_asset(
        "stock",
        symbol,
        date.today() - timedelta(days=365 * 15),
        date.today(),
        columns=[
            "symbol",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
            "raw_close",
            "raw_high",
            "raw_low",
            "signal_limit_up",
            "signal_limit_down",
        ],
    )
    return frame.sort("date").to_dicts() if not frame.is_empty() else []


def _structure_state(analyzer: Any, *, upstream: bool) -> dict[str, list[Any]]:
    if upstream:
        return {
            "fractals": [
                (_date_key(item.dt), item.mark.name, item.high, item.low, item.fx)
                for item in analyzer.fx_list
            ],
            "strokes": [
                (
                    _date_key(item.sdt),
                    _date_key(item.edt),
                    item.direction.name,
                    item.high,
                    item.low,
                    item.fx_a.fx,
                    item.fx_b.fx,
                    item.length,
                )
                for item in analyzer.bi_list
            ],
            "centers": [
                (
                    _date_key(item.sdt),
                    _date_key(item.edt),
                    item.zg,
                    item.zd,
                    item.gg,
                    item.dd,
                    len(item.bis),
                    item.is_valid(),
                )
                for item in analyzer.zs_list
            ],
        }
    return {
        "fractals": [
            (
                _date_key(item.date),
                "G" if item.mark == "top" else "D",
                item.high,
                item.low,
                item.price,
            )
            for item in analyzer.fx_list
        ],
        "strokes": [
            (
                _date_key(item.start_date),
                _date_key(item.end_date),
                "Up" if item.direction == "up" else "Down",
                item.high,
                item.low,
                item.start_price,
                item.end_price,
                len(item.bars),
            )
            for item in analyzer.bi_list
        ],
        "centers": [
            (
                _date_key(item.bis[0].start_date),
                _date_key(item.bis[-1].end_date),
                item.zg,
                item.zd,
                item.gg,
                item.dd,
                len(item.bis),
                item.is_valid(),
            )
            for item in local_core._get_zs_seq(analyzer.finished_bis)
        ],
    }


def compare_rows_with_upstream(
    rows: list[dict[str, Any]],
    symbol: str,
    czsc: Any,
) -> dict[str, Any]:
    """Compare every daily prefix and return the first actionable mismatch."""
    local_bars = local_core._clean_raw_bars(rows)
    if not local_bars:
        return {"symbol": symbol, "passed": False, "error": "no valid K-line rows"}
    upstream_bars = [
        czsc.RawBar(
            symbol,
            datetime.fromisoformat(item.date),
            czsc.Freq.D,
            item.open,
            item.close,
            item.high,
            item.low,
            item.volume,
            item.amount,
            item.source_index,
        )
        for item in local_bars
    ]
    upstream = czsc.CZSC([upstream_bars[0]], 50, 6)
    local = local_core._Analyzer([local_bars[0]])
    signal_cases = (
        (local_core._first_bs_signal, "zdy_macd_bs1_V230422", {"di": 1, "th": 50}, {"看多": "buy", "看空": "sell"}),
        (local_core._second_bs_signal, "cxt_second_bs_V230320", {"di": 1, "ma_type": "SMA", "timeperiod": 21}, {"二买": "buy", "二卖": "sell"}),
        (local_core._third_bs_signal, "cxt_third_bs_V230318", {"di": 1, "ma_type": "SMA", "timeperiod": 34}, {"三买": "buy", "三卖": "sell"}),
    )

    for index in range(len(local_bars)):
        if index:
            upstream.update(upstream_bars[index])
            local.update(local_bars[index])
        local_state = _structure_state(local, upstream=False)
        upstream_state = _structure_state(upstream, upstream=True)
        for kind in ("fractals", "strokes", "centers"):
            mismatch = _difference(local_state[kind], upstream_state[kind])
            if mismatch:
                return {
                    "symbol": symbol,
                    "passed": False,
                    "prefix_index": index,
                    "date": local_bars[index].date,
                    "kind": kind,
                    "mismatch": mismatch,
                }
        for local_signal, name, parameters, value_map in signal_cases:
            upstream_value = czsc._native.call_signal(name, upstream, parameters)[0].v1
            local_value = local_signal(local)
            if local_value != value_map.get(upstream_value):
                return {
                    "symbol": symbol,
                    "passed": False,
                    "prefix_index": index,
                    "date": local_bars[index].date,
                    "kind": "signal",
                    "signal": name,
                    "local": local_value,
                    "upstream": upstream_value,
                }
    state = _structure_state(local, upstream=False)
    return {
        "symbol": symbol,
        "passed": True,
        "bars": len(local_bars),
        "prefixes_checked": len(local_bars),
        "fractals": len(state["fractals"]),
        "strokes": len(state["strokes"]),
        "centers": len(state["centers"]),
    }


def compare_random_local_stocks(
    repo: Any,
    *,
    count: int = 5,
    seed: int = 42,
    max_bars: int = 800,
    czsc: Any | None = None,
) -> dict[str, Any]:
    """Sample local A shares and run a read-only upstream differential audit."""
    if czsc is None:
        try:
            import czsc as czsc_runtime
        except ImportError as exc:
            raise RuntimeError(
                f"optional runtime missing; run with `uv run --with {UPSTREAM_PACKAGE} ...`"
            ) from exc
        czsc = czsc_runtime
    from app.services.chan_training import _is_eligible_name, _load_rows

    instruments = repo.get_instruments()
    columns = [column for column in ("symbol", "name") if column in instruments.columns]
    candidates = [
        (str(item.get("symbol") or "").strip(), str(item.get("name") or "").strip())
        for item in instruments.select(columns).to_dicts()
    ]
    candidates = [item for item in candidates if item[0] and _is_eligible_name(item[1])]
    random.Random(seed).shuffle(candidates)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for symbol, _name in candidates:
        if len(results) >= count:
            break
        rows = _load_rows(repo, symbol)
        if len(rows) < 120:
            continue
        sample_started = time.perf_counter()
        try:
            result = compare_rows_with_upstream(rows[-max_bars:], symbol, czsc)
        except Exception as exc:
            result = {"symbol": symbol, "passed": False, "error": str(exc)}
        result["elapsed_ms"] = round((time.perf_counter() - sample_started) * 1000, 2)
        results.append(result)
    return {
        "algorithm_source": local_core.SOURCE_COMMIT,
        "upstream_package": UPSTREAM_PACKAGE,
        "seed": seed,
        "requested": count,
        "checked": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "persisted": False,
        "results": results,
    }


def audit_local_edge_cases(
    repo: Any,
    *,
    seed: int = 42,
    max_symbols: int = 80,
    window_bars: int = 300,
    czsc: Any | None = None,
) -> dict[str, Any]:
    """Find real local edge cases and compare each containing window upstream."""
    if czsc is None:
        try:
            import czsc as czsc_runtime
        except ImportError as exc:
            raise RuntimeError(
                f"optional runtime missing; run with `uv run --with {UPSTREAM_PACKAGE} ...`"
            ) from exc
        czsc = czsc_runtime
    from app.services.chan_training import _is_eligible_name

    instruments = repo.get_instruments()
    columns = [column for column in ("symbol", "name") if column in instruments.columns]
    candidates = [
        (str(item.get("symbol") or "").strip(), str(item.get("name") or "").strip())
        for item in instruments.select(columns).to_dicts()
    ]
    candidates = [item for item in candidates if item[0] and _is_eligible_name(item[1])]
    random.Random(seed).shuffle(candidates)
    samples: dict[str, dict[str, Any]] = {}
    scanned = 0
    adjusted_price_rows = 0
    started = time.perf_counter()
    for symbol, name in candidates[:max_symbols]:
        if len(samples) == len(EDGE_CASES):
            break
        scanned += 1
        rows = _load_edge_rows(repo, symbol)
        if len(rows) < 120:
            continue
        adjusted_price_rows += sum(
            1
            for row in rows
            if row.get("raw_close")
            and abs(float(row["close"]) / float(row["raw_close"]) - 1) > 1e-8
        )
        detected = detect_a_share_edge_cases(rows)
        for category in EDGE_CASES:
            if category in samples or not detected[category]:
                continue
            event = detected[category][0]
            event_index = int(event["index"])
            start = max(0, event_index - window_bars * 2 // 3)
            end = min(len(rows), start + window_bars)
            start = max(0, end - window_bars)
            window = rows[start:end]
            result = compare_rows_with_upstream(window, symbol, czsc)
            samples[category] = {
                "category": category,
                "symbol": symbol,
                "name": name,
                "event": event,
                "window": {
                    "start": str(window[0]["date"])[:10],
                    "end": str(window[-1]["date"])[:10],
                    "bars": len(window),
                },
                "comparison": result,
            }
    return {
        "algorithm_source": local_core.SOURCE_COMMIT,
        "upstream_package": UPSTREAM_PACKAGE,
        "seed": seed,
        "symbols_scanned": scanned,
        "required_categories": list(EDGE_CASES),
        "covered_categories": list(samples),
        "missing_categories": [key for key in EDGE_CASES if key not in samples],
        "price_basis": (
            "forward_adjusted_observed"
            if adjusted_price_rows
            else "raw_fallback_no_adjustment_factor_observed"
        ),
        "adjusted_price_rows": adjusted_price_rows,
        "data_warnings": (
            []
            if adjusted_price_rows
            else ["本地未观察到复权价与原始价差异; 无法验证真实复权因子变化样本"]
        ),
        "passed": len(samples) == len(EDGE_CASES)
        and all(item["comparison"]["passed"] for item in samples.values()),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "persisted": False,
        "samples": samples,
    }
