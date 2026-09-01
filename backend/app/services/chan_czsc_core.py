# SPDX-License-Identifier: Apache-2.0
"""Self-contained Python port of the referenced CZSC structure core.

This file adapts the deterministic FX / BI / ZS rules from
https://github.com/waditu/czsc/tree/701e480a545004f945bb1721e510ae610ad90c4c.
It does not import CZSC or use
its data connectors, trader, backtest engine, or runtime configuration.

Copyright (c) 2025 zengbin93.  Licensed under Apache-2.0; see
``licenses/czsc-Apache-2.0.txt`` at the repository root.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite, nan
from typing import Any

SOURCE_REPOSITORY = "https://github.com/waditu/czsc"
SOURCE_REVISION = "701e480a545004f945bb1721e510ae610ad90c4c"
SOURCE_COMMIT = f"waditu/czsc@{SOURCE_REVISION}"
ENGINE_NAME = "czsc-python-port"
MIN_BI_LEN = 6
MAX_BI_NUM = 50


@dataclass(frozen=True)
class SignalSpec:
    point_class: str
    name: str
    version: str
    parameters: dict[str, Any]
    minimum_bars: int
    dependencies: tuple[str, ...]
    description: str

    @property
    def key(self) -> str:
        params = ",".join(f"{key}={value}" for key, value in sorted(self.parameters.items()))
        return f"{self.name}[{params}]"


SIGNAL_REGISTRY = {
    "first": SignalSpec(
        point_class="first",
        name="zdy_macd_bs1_V230422",
        version="V230422",
        parameters={
            "di": 1,
            "th": 50,
            "requested_fastperiod": 26,
            "requested_slowperiod": 12,
            "effective_short": 12,
            "effective_long": 26,
            "signalperiod": 9,
        },
        minimum_bars=7,
        dependencies=("BI", "ZS", "MACD"),
        description="MACD 面积与 DIF 辅助识别第一类买卖点",
    ),
    "second": SignalSpec(
        point_class="second",
        name="cxt_second_bs_V230320",
        version="V230320",
        parameters={"di": 1, "ma_type": "SMA", "timeperiod": 21},
        minimum_bars=7,
        dependencies=("BI", "SMA21"),
        description="五笔结构与 SMA21 辅助识别第二类买卖点",
    ),
    "third": SignalSpec(
        point_class="third",
        name="cxt_third_bs_V230318",
        version="V230318",
        parameters={"di": 1, "ma_type": "SMA", "timeperiod": 34},
        minimum_bars=7,
        dependencies=("BI", "ZS", "SMA34"),
        description="离开中枢后的五笔结构与 SMA34 辅助识别第三类买卖点",
    ),
}
SIGNAL_PROFILE = {key: spec.name for key, spec in SIGNAL_REGISTRY.items()}
SIGNAL_PROFILE_ID = "czsc-teaching"
SIGNAL_PROFILES = {SIGNAL_PROFILE_ID: tuple(SIGNAL_REGISTRY)}


def signal_catalog() -> list[dict[str, Any]]:
    return [
        {
            "class": spec.point_class,
            "name": spec.name,
            "version": spec.version,
            "parameters": dict(spec.parameters),
            "minimum_bars": spec.minimum_bars,
            "dependencies": list(spec.dependencies),
            "description": spec.description,
            "key": spec.key,
        }
        for spec in SIGNAL_REGISTRY.values()
    ]


def _finite_or_none(value: float | None) -> float | None:
    return value if value is not None and isfinite(value) else None


def _calc_sma_cache_style(series: list[float], period: int) -> list[float]:
    """Port of CZSC's TA-Lib-style SMA cache calculation."""
    result = [nan] * len(series)
    if period <= 0 or len(series) < period:
        return result
    total = sum(series[:period])
    result[period - 1] = total / period
    for index in range(period, len(series)):
        total -= series[index - period]
        total += series[index]
        result[index] = total / period
    return result


def _calc_macd_cache_style(
    series: list[float],
    short: int = 12,
    long: int = 26,
    signal: int = 9,
) -> tuple[list[float], list[float]]:
    """Port of CZSC's TA-Lib-style DIF and unscaled MACD histogram."""
    size = len(series)
    dif_result = [nan] * size
    hist_result = [nan] * size
    if min(short, long, signal) <= 0 or size < long:
        return dif_result, hist_result

    fast_offset = long - short
    ema_short = sum(series[fast_offset:long]) / short
    ema_long = sum(series[:long]) / long
    raw_dif = [ema_short - ema_long]
    for index in range(long, size):
        ema_short = (2 * series[index] + ema_short * (short - 1)) / (short + 1)
        ema_long = (2 * series[index] + ema_long * (long - 1)) / (long + 1)
        raw_dif.append(ema_short - ema_long)

    if len(raw_dif) < signal:
        return dif_result, hist_result
    dea = sum(raw_dif[:signal]) / signal
    first_valid = long + signal - 2
    dif_result[first_valid] = raw_dif[signal - 1]
    hist_result[first_valid] = raw_dif[signal - 1] - dea
    for raw_index in range(signal, len(raw_dif)):
        dea = (2 * raw_dif[raw_index] + dea * (signal - 1)) / (signal + 1)
        output_index = long - 1 + raw_index
        dif_result[output_index] = raw_dif[raw_index]
        hist_result[output_index] = raw_dif[raw_index] - dea
    return dif_result, hist_result


def _rust_f64_max(values: list[float]) -> float:
    """Match Rust f64::max, which ignores a single NaN operand."""
    result = float("-inf")
    for value in values:
        if value == value:
            result = max(result, value)
    return result


def _rust_f64_min(values: list[float]) -> float:
    """Match Rust f64::min, which ignores a single NaN operand."""
    result = float("inf")
    for value in values:
        if value == value:
            result = min(result, value)
    return result


@dataclass(frozen=True)
class _RawBar:
    source_index: int
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float


@dataclass(frozen=True)
class _NewBar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float
    elements: tuple[_RawBar, ...]

    @classmethod
    def from_raw(cls, bar: _RawBar) -> _NewBar:
        return cls(
            date=bar.date,
            open=bar.open,
            close=bar.close,
            high=bar.high,
            low=bar.low,
            volume=bar.volume,
            amount=bar.amount,
            elements=(bar,),
        )


@dataclass(frozen=True)
class _FX:
    date: str
    mark: str
    high: float
    low: float
    price: float
    elements: tuple[_NewBar, ...]
    source_index: int
    confirmed_at: int


@dataclass(frozen=True)
class _BI:
    fx_a: _FX
    fx_b: _FX
    fxs: tuple[_FX, ...]
    direction: str
    bars: tuple[_NewBar, ...]

    @property
    def start_date(self) -> str:
        return self.fx_a.date

    @property
    def end_date(self) -> str:
        return self.fx_b.date

    @property
    def start_index(self) -> int:
        return self.fx_a.source_index

    @property
    def end_index(self) -> int:
        return self.fx_b.source_index

    @property
    def start_price(self) -> float:
        return self.fx_a.price

    @property
    def end_price(self) -> float:
        return self.fx_b.price

    @property
    def high(self) -> float:
        return max(self.fx_a.high, self.fx_b.high)

    @property
    def low(self) -> float:
        return min(self.fx_a.low, self.fx_b.low)

    @property
    def confirmed_at(self) -> int:
        return self.fx_b.confirmed_at

    @property
    def raw_bars(self) -> tuple[_RawBar, ...]:
        return tuple(raw for bar in self.bars[1:-1] for raw in bar.elements)


@dataclass(frozen=True)
class _ZS:
    bis: tuple[_BI, ...]
    stroke_start: int

    @property
    def zg(self) -> float:
        return min(item.high for item in self.bis[:3])

    @property
    def zd(self) -> float:
        return max(item.low for item in self.bis[:3])

    @property
    def gg(self) -> float:
        return max(item.high for item in self.bis)

    @property
    def dd(self) -> float:
        return min(item.low for item in self.bis)

    def is_valid(self) -> bool:
        # Match CZSC ZS::is_valid exactly.  A trailing one/two-BI group is a
        # valid ZS object upstream, but it is not emitted as a confirmed
        # center by _build_centers until three BI are present.
        if self.zg < self.zd:
            return False
        return all(
            self.zd <= item.high <= self.zg
            or self.zd <= item.low <= self.zg
            or (item.high >= self.zg and item.low <= self.zd)
            for item in self.bis
        )


def _raw_bar(row: dict[str, Any], source_index: int) -> _RawBar | None:
    try:
        # Intraday rows must retain their full timestamp. Using only the
        # calendar date silently collapsed every 30-minute bar into one bar.
        date = str(row.get("datetime") or row["date"])
        open_ = float(row.get("open", row["close"]))
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        volume = float(row.get("volume", row.get("vol", 0)) or 0)
        amount = float(row.get("amount", 0) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    values = (open_, close, high, low, volume, amount)
    if not date or not all(isfinite(value) for value in values):
        return None
    if high <= 0 or low <= 0 or high < low or open_ <= 0 or close <= 0:
        return None
    return _RawBar(
        source_index=source_index,
        date=date,
        open=open_,
        close=close,
        high=high,
        low=low,
        volume=volume,
        amount=amount,
    )


def _clean_raw_bars(rows: list[dict[str, Any]]) -> list[_RawBar]:
    bars: list[_RawBar] = []
    for source_index, row in enumerate(rows):
        bar = _raw_bar(row, source_index)
        if bar is None:
            continue
        if bars and bars[-1].date == bar.date:
            bars[-1] = bar
        else:
            bars.append(bar)
    return bars


def _source_index(bar: _NewBar) -> int:
    exact = [item.source_index for item in bar.elements if item.date == bar.date]
    if exact:
        return exact[-1]
    return max(item.source_index for item in bar.elements)


def _confirmation_index(bar: _NewBar) -> int:
    return max(item.source_index for item in bar.elements)


def _remove_include(k1: _NewBar, k2: _NewBar, k3: _RawBar) -> tuple[bool, _NewBar]:
    """Port of CZSC ``remove_include`` for one incoming raw bar."""
    if k1.high < k2.high:
        direction = "up"
    elif k1.high > k2.high:
        direction = "down"
    else:
        return False, _NewBar.from_raw(k3)

    included = (
        (k2.high <= k3.high and k2.low >= k3.low)
        or (k2.high >= k3.high and k2.low <= k3.low)
    )
    if not included:
        return False, _NewBar.from_raw(k3)

    if direction == "up":
        high = max(k2.high, k3.high)
        low = max(k2.low, k3.low)
        date = k2.date if k2.high > k3.high else k3.date
    else:
        high = min(k2.high, k3.high)
        low = min(k2.low, k3.low)
        date = k2.date if k2.low < k3.low else k3.date
    open_, close = (high, low) if k3.open > k3.close else (low, high)
    elements = (*tuple(item for item in k2.elements[:100] if item.date != k3.date), k3)
    return True, _NewBar(
        date=date,
        open=open_,
        close=close,
        high=high,
        low=low,
        volume=k2.volume + k3.volume,
        amount=k2.amount + k3.amount,
        elements=elements,
    )


def _check_fx(k1: _NewBar, k2: _NewBar, k3: _NewBar) -> _FX | None:
    if k1.high < k2.high > k3.high and k1.low < k2.low > k3.low:
        mark = "top"
        price = k2.high
    elif k1.low > k2.low < k3.low and k1.high > k2.high < k3.high:
        mark = "bottom"
        price = k2.low
    else:
        return None
    return _FX(
        date=k2.date,
        mark=mark,
        high=k2.high,
        low=k2.low,
        price=price,
        elements=(k1, k2, k3),
        source_index=_source_index(k2),
        confirmed_at=_confirmation_index(k3),
    )


def _check_fxs(bars: list[_NewBar] | tuple[_NewBar, ...]) -> list[_FX]:
    fxs: list[_FX] = []
    for index in range(len(bars) - 2):
        fx = _check_fx(bars[index], bars[index + 1], bars[index + 2])
        if fx is None:
            continue
        # Current CZSC suppresses any repeated mark after the first fractal.
        if fxs and fx.mark == fxs[-1].mark:
            continue
        fxs.append(fx)
    return fxs


def _first_not_before(bars: list[_NewBar] | tuple[_NewBar, ...], date: str) -> int:
    for index, bar in enumerate(bars):
        if bar.date >= date:
            return index
    return len(bars)


def _first_after(bars: list[_NewBar] | tuple[_NewBar, ...], date: str) -> int:
    for index, bar in enumerate(bars):
        if bar.date > date:
            return index
    return len(bars)


def _check_bi(
    bars: list[_NewBar] | tuple[_NewBar, ...],
    min_bi_len: int = MIN_BI_LEN,
) -> tuple[_BI | None, list[_NewBar]]:
    fxs = _check_fxs(bars)
    if len(fxs) < 2:
        return None, list(bars)

    fx_a = fxs[0]
    candidates = [
        item
        for item in fxs
        if item.date > fx_a.date
        and item.mark != fx_a.mark
        and (item.price > fx_a.price if fx_a.mark == "bottom" else item.price < fx_a.price)
    ]
    if not candidates:
        return None, list(bars)

    fx_b = candidates[0]
    for item in candidates[1:]:
        if (
            (fx_a.mark == "bottom" and item.high > fx_b.high)
            or (fx_a.mark == "top" and item.low < fx_b.low)
        ):
            fx_b = item

    direction = "up" if fx_a.mark == "bottom" else "down"
    start_date = fx_a.elements[0].date
    end_date = fx_b.elements[2].date
    start_index = _first_not_before(bars, start_date)
    end_index = _first_after(bars, end_date)
    if start_index >= end_index:
        return None, list(bars)
    bi_bars = list(bars[start_index:end_index])
    remainder = list(bars[_first_not_before(bars, fx_b.elements[0].date):])
    endpoints_include = (
        (fx_a.high > fx_b.high and fx_a.low < fx_b.low)
        or (fx_a.high < fx_b.high and fx_a.low > fx_b.low)
    )
    if endpoints_include or len(bi_bars) < min_bi_len:
        return None, list(bars)

    internal_fxs = tuple(item for item in fxs if start_date <= item.date <= end_date)
    return (
        _BI(
            fx_a=fx_a,
            fx_b=fx_b,
            fxs=internal_fxs,
            direction=direction,
            bars=tuple(bi_bars),
        ),
        remainder,
    )


class _Analyzer:
    def __init__(
        self,
        bars: list[_RawBar],
        min_bi_len: int = MIN_BI_LEN,
        max_bi_num: int = MAX_BI_NUM,
    ) -> None:
        self.min_bi_len = min_bi_len
        self.max_bi_num = max_bi_num
        self.raw_bars: list[_RawBar] = []
        self.bars_ubi: list[_NewBar] = []
        self.bi_list: list[_BI] = []
        self._reset_indicator_state()
        for bar in bars:
            self.update(bar)

    def _reset_indicator_state(self) -> None:
        self._close_prefix: list[float] = [0.0]
        self._gain_prefix: list[float] = [0.0]
        self._loss_prefix: list[float] = [0.0]
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._ema_12_state: float | None = None
        self._ema_26_state: float | None = None
        self._dea_12_26_state: float | None = None
        self._dif_raw: list[float] = []
        self._dif_12_26: dict[int, float] = {}
        self._macd_12_26: dict[int, float] = {}
        self._sma: dict[int, dict[int, float]] = {5: {}, 20: {}, 21: {}, 34: {}}
        self._rsi: dict[int, dict[int, float]] = {6: {}, 14: {}, 24: {}}
        self._kdj: dict[str, dict[int, float]] = {"k": {}, "d": {}, "j": {}}
        self._k_state = 50.0
        self._d_state = 50.0
        self._signal_sma_cache: dict[int, tuple[list[int], list[float]]] = {}
        self._signal_macd_cache: tuple[list[int], list[float], list[float]] | None = None

    def _rebuild_indicator_state(self) -> None:
        bars = list(self.raw_bars)
        self.raw_bars = []
        self._reset_indicator_state()
        for item in bars:
            self.raw_bars.append(item)
            self._update_indicators(item)

    @staticmethod
    def _patch_extended_new_bar(
        item: _NewBar,
        target: _NewBar,
        bar: _RawBar,
    ) -> _NewBar:
        """Patch only the raw-bar snapshot, matching CZSC's same-dt update."""
        if item != target or not item.elements or item.elements[-1].date != bar.date:
            return item
        elements = (*item.elements[:-1], bar)
        return replace(item, elements=elements)

    @classmethod
    def _patch_extended_fx(cls, fx: _FX, target: _NewBar, bar: _RawBar) -> _FX:
        elements = tuple(cls._patch_extended_new_bar(item, target, bar) for item in fx.elements)
        return replace(fx, elements=elements) if elements != fx.elements else fx

    def _patch_same_datetime_snapshots(self, target: _NewBar, bar: _RawBar) -> None:
        """Keep historical BI/FX raw snapshots alive during a bar extension."""
        patched_bis: list[_BI] = []
        for bi in self.bi_list:
            bars = tuple(self._patch_extended_new_bar(item, target, bar) for item in bi.bars)
            fx_a = self._patch_extended_fx(bi.fx_a, target, bar)
            fx_b = self._patch_extended_fx(bi.fx_b, target, bar)
            fxs = tuple(self._patch_extended_fx(item, target, bar) for item in bi.fxs)
            patched_bis.append(replace(bi, bars=bars, fx_a=fx_a, fx_b=fx_b, fxs=fxs))
        self.bi_list = patched_bis

    def update(self, bar: _RawBar) -> None:
        if self.raw_bars and self.raw_bars[-1].date == bar.date:
            # CZSC replaces the last RawBar, pops the last UBI, patches any
            # mirrored historical snapshots, then reprocesses the popped UBI
            # elements.  This matters for signals that read FX snapshots.
            self.raw_bars[-1] = bar
            self._patch_same_datetime_snapshots(self.bars_ubi[-1], bar)
            last_ubi = self.bars_ubi.pop()
            last_elements = list(last_ubi.elements)
            if not last_elements or last_elements[-1].date != bar.date:
                raise ValueError("same-datetime bar does not extend the latest UBI")
            last_elements[-1] = bar
            replay_bars = last_elements
            self._rebuild_indicator_state()
        else:
            self.raw_bars.append(bar)
            self._update_indicators(bar)
            replay_bars = [bar]

        for raw_bar in replay_bars:
            if len(self.bars_ubi) < 2:
                self.bars_ubi.append(_NewBar.from_raw(raw_bar))
                continue
            included, merged = _remove_include(self.bars_ubi[-2], self.bars_ubi[-1], raw_bar)
            if included:
                self.bars_ubi[-1] = merged
            else:
                self.bars_ubi.append(merged)
        self._update_bi()
        if len(self.bi_list) > self.max_bi_num:
            self.bi_list = self.bi_list[-self.max_bi_num:]
        if self.bi_list:
            start_date = self.bi_list[0].fx_a.elements[0].date
            start = next(
                (
                    index
                    for index, item in enumerate(self.raw_bars)
                    if item.date >= start_date
                ),
                len(self.raw_bars),
            )
            if start:
                self.raw_bars = self.raw_bars[start:]
                self._rebuild_indicator_state()

    def _update_indicators(self, bar: _RawBar) -> None:
        index = len(self.raw_bars) - 1
        previous_close = self.raw_bars[-2].close if index > 0 else bar.close
        delta = bar.close - previous_close
        self._close_prefix.append(self._close_prefix[-1] + bar.close)
        self._gain_prefix.append(self._gain_prefix[-1] + max(delta, 0.0))
        self._loss_prefix.append(self._loss_prefix[-1] + max(-delta, 0.0))
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._dif_12_26[bar.source_index] = nan
        self._macd_12_26[bar.source_index] = nan
        # CZSC's signal cache follows TA-Lib's warm-up semantics: EMA(12)
        # starts at the 12-bar mean aligned with the EMA(26) start, EMA(26)
        # starts at the 26-bar mean, and MACD is DIF - DEA (no extra *2).
        if index == 25:
            self._ema_12_state = sum(item.close for item in self.raw_bars[14:26]) / 12
            self._ema_26_state = sum(item.close for item in self.raw_bars[:26]) / 26
        elif index > 25:
            assert self._ema_12_state is not None and self._ema_26_state is not None
            self._ema_12_state = (2 * bar.close + self._ema_12_state * 11) / 13
            self._ema_26_state = (2 * bar.close + self._ema_26_state * 25) / 27
        if index >= 25:
            assert self._ema_12_state is not None and self._ema_26_state is not None
            dif = self._ema_12_state - self._ema_26_state
            self._dif_raw.append(dif)
            if index == 33:
                self._dea_12_26_state = sum(self._dif_raw[:9]) / 9
            elif index > 33:
                assert self._dea_12_26_state is not None
                self._dea_12_26_state = (2 * dif + self._dea_12_26_state * 8) / 10
            if self._dea_12_26_state is not None:
                self._dif_12_26[bar.source_index] = dif
                self._macd_12_26[bar.source_index] = dif - self._dea_12_26_state
        for period, values in self._sma.items():
            values[bar.source_index] = nan
            if index + 1 >= period:
                start = index + 1 - period
                total = self._close_prefix[index + 1] - self._close_prefix[start]
                values[bar.source_index] = total / period
        for period, values in self._rsi.items():
            start = max(0, index + 1 - period)
            count = index + 1 - start
            gains = self._gain_prefix[index + 1] - self._gain_prefix[start]
            losses = self._loss_prefix[index + 1] - self._loss_prefix[start]
            average_gain = gains / count
            average_loss = losses / count
            values[bar.source_index] = round(
                100.0 if average_loss == 0 and average_gain > 0
                else 0.0 if average_gain == 0
                else 100 - 100 / (1 + average_gain / average_loss),
                4,
            )
        kdj_start = max(0, index - 8)
        high_9 = max(self._highs[kdj_start:index + 1])
        low_9 = min(self._lows[kdj_start:index + 1])
        rsv = 50.0 if high_9 == low_9 else (bar.close - low_9) / (high_9 - low_9) * 100
        self._k_state = (2 * self._k_state + rsv) / 3
        self._d_state = (2 * self._d_state + self._k_state) / 3
        self._kdj["k"][bar.source_index] = round(self._k_state, 4)
        self._kdj["d"][bar.source_index] = round(self._d_state, 4)
        self._kdj["j"][bar.source_index] = round(3 * self._k_state - 2 * self._d_state, 4)

    @property
    def macd_12_26_maps(self) -> tuple[dict[int, float], dict[int, float]]:
        bars = self._signal_raw_bars()
        ids = [bar.source_index for bar in bars]
        now_len = len(ids)
        minimum_count = 9 + 26 + 168
        cache = self._signal_macd_cache
        need_init = cache is None or now_len < minimum_count + 15
        if not need_init and cache is not None:
            old_ids = cache[0]
            need_init = now_len < 2 or not old_ids or ids[-2] not in old_ids

        closes = [bar.close for bar in bars]
        if need_init:
            dif, hist = _calc_macd_cache_style(closes)
        else:
            assert cache is not None
            old_map = {
                source_index: (cache[1][index], cache[2][index])
                for index, source_index in enumerate(cache[0])
            }
            dif = [old_map.get(source_index, (nan, nan))[0] for source_index in ids]
            hist = [old_map.get(source_index, (nan, nan))[1] for source_index in ids]
            window_size = min(minimum_count + 10, now_len)
            partial_dif, partial_hist = _calc_macd_cache_style(closes[-window_size:])
            for offset in range(1, min(5, window_size) + 1):
                dif[-offset] = partial_dif[-offset]
                hist[-offset] = partial_hist[-offset]
        self._signal_macd_cache = (ids, dif, hist)
        return dict(zip(ids, dif, strict=True)), dict(zip(ids, hist, strict=True))

    def sma_map(self, period: int) -> dict[int, float]:
        bars = self._signal_raw_bars()
        ids = [bar.source_index for bar in bars]
        now_len = len(ids)
        cache = self._signal_sma_cache.get(period)
        need_init = cache is None or now_len < period + 15
        if not need_init and cache is not None:
            need_init = now_len < 2 or not cache[0] or ids[-2] not in cache[0]

        closes = [bar.close for bar in bars]
        if need_init:
            values = _calc_sma_cache_style(closes, period)
        else:
            assert cache is not None
            old_map = dict(zip(cache[0], cache[1], strict=True))
            values = [old_map.get(source_index, nan) for source_index in ids]
            window_size = min(period + 10, now_len)
            partial = _calc_sma_cache_style(closes[-window_size:], period)
            for offset in range(1, min(5, window_size) + 1):
                values[-offset] = partial[-offset]
        self._signal_sma_cache[period] = (ids, values)
        return dict(zip(ids, values, strict=True))

    def _signal_raw_bars(self) -> list[_RawBar]:
        """Return the same retained RawBar window exposed by CZSC."""
        if not self.bi_list:
            return self.raw_bars
        start_date = self.bi_list[0].fx_a.elements[0].date
        start = next(
            (index for index, bar in enumerate(self.raw_bars) if bar.date >= start_date),
            len(self.raw_bars),
        )
        return self.raw_bars[start:]

    def indicator_snapshot(self) -> dict[str, Any]:
        if not self.raw_bars:
            return {"cached_bars": 0}
        signal_bars = self._signal_raw_bars()
        source_index = signal_bars[-1].source_index
        dif_map, hist_map = self.macd_12_26_maps
        return {
            "cached_bars": len(signal_bars),
            "macd": {
                "dif": _finite_or_none(dif_map.get(source_index)),
                "hist": _finite_or_none(hist_map.get(source_index)),
            },
            "sma": {
                str(period): _finite_or_none(self.sma_map(period).get(source_index))
                for period in self._sma
            },
            "rsi": {str(period): values.get(source_index) for period, values in self._rsi.items()},
            "kdj": {key: values.get(source_index) for key, values in self._kdj.items()},
        }

    def _update_bi(self) -> None:
        if len(self.bars_ubi) < 3:
            return
        if not self.bi_list:
            fxs = _check_fxs(self.bars_ubi)
            if not fxs:
                return
            first = fxs[0]
            fx_a = first
            for item in fxs:
                if item.mark != first.mark:
                    continue
                if (
                    (first.mark == "bottom" and item.low <= fx_a.low)
                    or (first.mark == "top" and item.high >= fx_a.high)
                ):
                    fx_a = item
            bars = self.bars_ubi[_first_not_before(self.bars_ubi, fx_a.elements[0].date):]
            bi, remainder = _check_bi(bars, self.min_bi_len)
            if bi is not None:
                self.bi_list.append(bi)
            self.bars_ubi = remainder
            return

        bi, remainder = _check_bi(self.bars_ubi, self.min_bi_len)
        if bi is not None:
            self.bi_list.append(bi)
        self.bars_ubi = remainder

        last_bi = self.bi_list[-1]
        if not self.bars_ubi:
            return
        latest = self.bars_ubi[-1]
        broken = (
            last_bi.direction == "up" and latest.high > last_bi.high
        ) or (
            last_bi.direction == "down" and latest.low < last_bi.low
        )
        if not broken:
            return
        merge_point = last_bi.bars[-2].date
        self.bars_ubi = list(last_bi.bars[:-2]) + [
            item for item in self.bars_ubi if item.date >= merge_point
        ]
        self.bi_list.pop()

    @property
    def finished_bis(self) -> list[_BI]:
        if not self.bi_list:
            return []
        if len(self.bars_ubi) < 5:
            return self.bi_list[:-1]
        return list(self.bi_list)

    @property
    def fx_list(self) -> list[_FX]:
        result: list[_FX] = []
        for bi in self.bi_list:
            for item in bi.fxs[1:]:
                if not result or item.date > result[-1].date:
                    result.append(item)
        for item in _check_fxs(self.bars_ubi):
            if not result or item.date > result[-1].date:
                result.append(item)
        return result


def _get_zs_seq(bis: list[_BI]) -> list[_ZS]:
    zones: list[_ZS] = []
    for index, bi in enumerate(bis):
        if not zones:
            zones.append(_ZS((bi,), index))
            continue
        last = zones[-1]
        leaves = (
            bi.direction == "up" and bi.high < last.zd
        ) or (
            bi.direction == "down" and bi.low > last.zg
        )
        if leaves:
            zones.append(_ZS((bi,), index))
        else:
            zones[-1] = _ZS((*last.bis, bi), last.stroke_start)
    return zones


def _build_centers(bis: list[_BI], *, level: str) -> list[dict[str, Any]]:
    centers: list[dict[str, Any]] = []
    for zone in _get_zs_seq(bis):
        if len(zone.bis) < 3 or not zone.is_valid():
            continue
        first, third, last = zone.bis[0], zone.bis[2], zone.bis[-1]
        centers.append({
            "start_date": first.start_date,
            "end_date": last.end_date,
            "formation_start_date": first.start_date,
            "formation_end_date": third.end_date,
            "low": round(zone.zd, 4),
            "high": round(zone.zg, 4),
            "range_low": round(zone.dd, 4),
            "range_high": round(zone.gg, 4),
            "start_index": first.start_index,
            "end_index": last.end_index,
            "formation_start_index": first.start_index,
            "formation_end_index": third.end_index,
            "confirmed_at": third.confirmed_at,
            "extension_confirmed_at": last.confirmed_at,
            "stroke_start": zone.stroke_start,
            "stroke_end": zone.stroke_start + len(zone.bis) - 1,
            "kind": "extension" if len(zone.bis) > 3 else "initial",
            "basis": "stroke",
            "level": level,
        })
    return centers


def _fx_raw_bars(fx: _FX) -> tuple[_RawBar, ...]:
    return tuple(raw for bar in fx.elements for raw in bar.elements)


def _ma_at(
    fx: _FX,
    values: dict[int, float],
    *,
    second_last: bool,
) -> float | None:
    bars = _fx_raw_bars(fx)
    offset = -2 if second_last else -1
    if len(bars) < abs(offset):
        return None
    return values.get(bars[offset].source_index)


def _ma_snapshot_at(
    analyzer: _Analyzer,
    fx: _FX,
    values: dict[int, float],
    *,
    period: int,
    overrides: dict[int, float],
) -> float | None:
    """Port CZSC ``ma_snapshot_value`` for an FX's last raw-bar snapshot."""
    bars = _fx_raw_bars(fx)
    if not bars:
        return None
    raw_bar = bars[-1]
    current_bars = analyzer._signal_raw_bars()
    positions = {
        item.source_index: index
        for index, item in enumerate(current_bars)
    }
    index = positions.get(raw_bar.source_index)
    if index is None:
        return None
    base = values.get(raw_bar.source_index)
    if base is None:
        return None
    if abs(current_bars[index].close - raw_bar.close) <= 2.220446049250313e-16:
        return base
    if raw_bar.source_index in overrides:
        return overrides[raw_bar.source_index]

    closes = [item.close for item in current_bars]
    closes[index] = raw_bar.close
    snapshot = _calc_sma_cache_style(closes, period)[index]
    overrides[raw_bar.source_index] = snapshot
    return snapshot


def _evidence_number(value: float) -> float:
    return round(float(value), 6)


def _stroke_evidence(item: _BI) -> dict[str, Any]:
    return {
        "start_date": item.start_date,
        "end_date": item.end_date,
        "direction": item.direction,
        "high": _evidence_number(item.high),
        "low": _evidence_number(item.low),
    }


def _evidence_check(
    label: str,
    left: str | float,
    operator: str,
    right: str | float,
) -> dict[str, Any]:
    return {
        "label": label,
        "left": _evidence_number(left) if isinstance(left, float) else left,
        "operator": operator,
        "right": _evidence_number(right) if isinstance(right, float) else right,
        "passed": True,
    }


def _first_bs_signal_result(
    analyzer: _Analyzer,
) -> tuple[str | None, dict[str, Any] | None]:
    """Port ``zdy_macd_bs1_V230422`` and retain its matched operands."""
    if len(analyzer.bi_list) < 7 or len(analyzer.bars_ubi) > 9:
        return None, None
    # The signal requests (26, 12, 9), while CZSC's macd_cache_maps helper
    # normalizes them with min/max before calculation: effective MACD(12,26,9).
    dif_map, macd_map = analyzer.macd_12_26_maps
    for count in (13, 11, 9, 7, 5):
        if len(analyzer.bi_list) < count:
            continue
        bis = analyzer.bi_list[-count:]
        inner = _ZS(tuple(bis[1:-1]), 0)
        if not inner.is_valid():
            continue
        first, last = bis[0], bis[-1]
        first_raw = first.raw_bars
        last_raw = last.raw_bars
        if len(first_raw) < 3 or len(last_raw) < 3:
            continue
        first_area = sum(abs(macd_map.get(item.source_index, 0)) for item in first_raw[1:-1])
        last_area = sum(abs(macd_map.get(item.source_index, 0)) for item in last_raw[1:-1])
        first_dif = dif_map.get(first_raw[-2].source_index, 0)
        last_dif = dif_map.get(last_raw[-2].source_index, 0)
        last_start_dif = dif_map.get(last_raw[1].source_index, 0)
        zone_values = [
            dif_map[raw.source_index]
            for item in inner.bis
            if item.direction == first.direction
            for raw in _fx_raw_bars(item.fx_b)
            if raw.source_index in dif_map
        ]
        if not zone_values or last_area > first_area * 0.5:
            continue
        zone_dif = (
            _rust_f64_max(zone_values)
            if first.direction == "up"
            else _rust_f64_min(zone_values)
        )
        min_low = min(item.low for item in bis)
        max_high = max(item.high for item in bis)
        if (
            first.direction == "up"
            and first.low == min_low
            and last.high == max_high
            and last_start_dif < abs(zone_dif) * 0.5
            and first_dif > last_dif > zone_dif > 0
        ):
            return "sell", {
                "rule": SIGNAL_REGISTRY["first"].name,
                "stroke_count": count,
                "strokes": [_stroke_evidence(first), _stroke_evidence(last)],
                "center": {
                    "zg": _evidence_number(inner.zg),
                    "zd": _evidence_number(inner.zd),
                    "gg": _evidence_number(inner.gg),
                    "dd": _evidence_number(inner.dd),
                },
                "indicators": {
                    "macd_first_area": _evidence_number(first_area),
                    "macd_last_area": _evidence_number(last_area),
                    "dif_first_end": _evidence_number(first_dif),
                    "dif_last_start": _evidence_number(last_start_dif),
                    "dif_last_end": _evidence_number(last_dif),
                    "dif_center_extreme": _evidence_number(zone_dif),
                },
                "checks": [
                    _evidence_check("首笔方向", first.direction, "=", "up"),
                    _evidence_check("首笔为窗口最低", first.low, "=", min_low),
                    _evidence_check("末笔为窗口最高", last.high, "=", max_high),
                    _evidence_check("末笔 MACD 面积", last_area, "<=", first_area * 0.5),
                    _evidence_check(
                        "末笔起点 DIF",
                        last_start_dif,
                        "<",
                        abs(zone_dif) * 0.5,
                    ),
                    _evidence_check("首末 DIF", first_dif, ">", last_dif),
                    _evidence_check("末笔与中枢 DIF", last_dif, ">", zone_dif),
                    _evidence_check("中枢 DIF", zone_dif, ">", 0.0),
                ],
            }
        if (
            first.direction == "down"
            and first.high == max_high
            and last.low == min_low
            and last_start_dif > abs(zone_dif) * 0.5
            and 0 > zone_dif > last_dif > first_dif
        ):
            return "buy", {
                "rule": SIGNAL_REGISTRY["first"].name,
                "stroke_count": count,
                "strokes": [_stroke_evidence(first), _stroke_evidence(last)],
                "center": {
                    "zg": _evidence_number(inner.zg),
                    "zd": _evidence_number(inner.zd),
                    "gg": _evidence_number(inner.gg),
                    "dd": _evidence_number(inner.dd),
                },
                "indicators": {
                    "macd_first_area": _evidence_number(first_area),
                    "macd_last_area": _evidence_number(last_area),
                    "dif_first_end": _evidence_number(first_dif),
                    "dif_last_start": _evidence_number(last_start_dif),
                    "dif_last_end": _evidence_number(last_dif),
                    "dif_center_extreme": _evidence_number(zone_dif),
                },
                "checks": [
                    _evidence_check("首笔方向", first.direction, "=", "down"),
                    _evidence_check("首笔为窗口最高", first.high, "=", max_high),
                    _evidence_check("末笔为窗口最低", last.low, "=", min_low),
                    _evidence_check("末笔 MACD 面积", last_area, "<=", first_area * 0.5),
                    _evidence_check(
                        "末笔起点 DIF",
                        last_start_dif,
                        ">",
                        abs(zone_dif) * 0.5,
                    ),
                    _evidence_check("中枢 DIF", 0.0, ">", zone_dif),
                    _evidence_check("中枢与末笔 DIF", zone_dif, ">", last_dif),
                    _evidence_check("末笔与首笔 DIF", last_dif, ">", first_dif),
                ],
            }
    return None, None


def _first_bs_signal(analyzer: _Analyzer) -> str | None:
    """Return only the upstream signal value for differential tests."""
    return _first_bs_signal_result(analyzer)[0]


def _second_bs_signal_result(
    analyzer: _Analyzer,
) -> tuple[str | None, dict[str, Any] | None]:
    """Port ``cxt_second_bs_V230320`` and retain its matched operands."""
    if len(analyzer.bi_list) < 7:
        return None, None
    bis = analyzer.bi_list[-5:]
    first, third, fifth = bis[0], bis[2], bis[4]
    ma_map = analyzer.sma_map(21)
    first_end = _ma_at(first.fx_b, ma_map, second_last=True)
    third_end = _ma_at(third.fx_b, ma_map, second_last=True)
    fifth_start = _ma_at(fifth.fx_a, ma_map, second_last=True)
    fifth_end = _ma_at(fifth.fx_b, ma_map, second_last=True)
    if None in (first_end, third_end, fifth_start, fifth_end):
        return None, None
    evidence = {
        "rule": SIGNAL_REGISTRY["second"].name,
        "stroke_count": 5,
        "strokes": [
            _stroke_evidence(first),
            _stroke_evidence(third),
            _stroke_evidence(fifth),
        ],
        "indicators": {
            "sma21_first_end": _evidence_number(first_end),
            "sma21_third_end": _evidence_number(third_end),
            "sma21_fifth_start": _evidence_number(fifth_start),
            "sma21_fifth_end": _evidence_number(fifth_end),
        },
    }
    if fifth.direction == "down" and first.low < first_end and third.low < third_end and fifth_start < fifth_end:
        return "buy", {
            **evidence,
            "checks": [
                _evidence_check("第五笔方向", fifth.direction, "=", "down"),
                _evidence_check("第一笔低点", first.low, "<", first_end),
                _evidence_check("第三笔低点", third.low, "<", third_end),
                _evidence_check("第五笔 SMA21", fifth_start, "<", fifth_end),
            ],
        }
    if fifth.direction == "up" and first.high > first_end and third.high > third_end and fifth_start > fifth_end:
        return "sell", {
            **evidence,
            "checks": [
                _evidence_check("第五笔方向", fifth.direction, "=", "up"),
                _evidence_check("第一笔高点", first.high, ">", first_end),
                _evidence_check("第三笔高点", third.high, ">", third_end),
                _evidence_check("第五笔 SMA21", fifth_start, ">", fifth_end),
            ],
        }
    return None, None


def _second_bs_signal(analyzer: _Analyzer) -> str | None:
    """Return only the upstream signal value for differential tests."""
    return _second_bs_signal_result(analyzer)[0]


def _third_bs_signal_result(
    analyzer: _Analyzer,
) -> tuple[str | None, dict[str, Any] | None]:
    """Port ``cxt_third_bs_V230318`` and retain its matched operands."""
    if len(analyzer.bi_list) < 7:
        return None, None
    bis = analyzer.bi_list[-5:]
    first, third, fifth = bis[0], bis[2], bis[4]
    zd = max(first.low, third.low)
    zg = min(first.high, third.high)
    if zd > zg:
        return None, None
    ma_map = analyzer.sma_map(34)
    overrides: dict[int, float] = {}
    ma_first = _ma_snapshot_at(
        analyzer,
        first.fx_b,
        ma_map,
        period=34,
        overrides=overrides,
    )
    ma_third = _ma_snapshot_at(
        analyzer,
        third.fx_b,
        ma_map,
        period=34,
        overrides=overrides,
    )
    ma_fifth = _ma_snapshot_at(
        analyzer,
        fifth.fx_b,
        ma_map,
        period=34,
        overrides=overrides,
    )
    if None in (ma_first, ma_third, ma_fifth):
        return None, None
    evidence = {
        "rule": SIGNAL_REGISTRY["third"].name,
        "stroke_count": 5,
        "strokes": [
            _stroke_evidence(first),
            _stroke_evidence(third),
            _stroke_evidence(fifth),
        ],
        "center": {
            "zg": _evidence_number(zg),
            "zd": _evidence_number(zd),
            "gg": _evidence_number(max(first.high, third.high)),
            "dd": _evidence_number(min(first.low, third.low)),
        },
        "indicators": {
            "sma34_first_end": _evidence_number(ma_first),
            "sma34_third_end": _evidence_number(ma_third),
            "sma34_fifth_end": _evidence_number(ma_fifth),
        },
    }
    if fifth.direction == "down" and fifth.low > zg and ma_fifth > ma_third > ma_first:
        return "buy", {
            **evidence,
            "checks": [
                _evidence_check("第五笔方向", fifth.direction, "=", "down"),
                _evidence_check("回抽低点", fifth.low, ">", zg),
                _evidence_check("第五与第三笔 SMA34", ma_fifth, ">", ma_third),
                _evidence_check("第三与第一笔 SMA34", ma_third, ">", ma_first),
            ],
        }
    if fifth.direction == "up" and fifth.high < zd and ma_fifth < ma_third < ma_first:
        return "sell", {
            **evidence,
            "checks": [
                _evidence_check("第五笔方向", fifth.direction, "=", "up"),
                _evidence_check("回抽高点", fifth.high, "<", zd),
                _evidence_check("第五与第三笔 SMA34", ma_fifth, "<", ma_third),
                _evidence_check("第三与第一笔 SMA34", ma_third, "<", ma_first),
            ],
        }
    return None, None


def _third_bs_signal(analyzer: _Analyzer) -> str | None:
    """Return only the upstream signal value for differential tests."""
    return _third_bs_signal_result(analyzer)[0]


def _current_signal_points(analyzer: _Analyzer, bar: _RawBar, level: str) -> list[dict[str, Any]]:
    if not analyzer.bi_list:
        return []
    last_bi = analyzer.bi_list[-1]
    points: list[dict[str, Any]] = []
    for point_class, signal_fn in (
        ("first", _first_bs_signal_result),
        ("second", _second_bs_signal_result),
        ("third", _third_bs_signal_result),
    ):
        side, evidence = signal_fn(analyzer)
        if side is None:
            continue
        spec = SIGNAL_REGISTRY[point_class]
        points.append({
            "date": bar.date,
            "type": side,
            "class": point_class,
            "price": round(last_bi.end_price, 4),
            "status": "confirmed",
            "reason": spec.description,
            "signal_name": spec.name,
            "signal_key": spec.key,
            "signal_version": spec.version,
            "signal_parameters": dict(spec.parameters),
            "signal_dependencies": list(spec.dependencies),
            "signal_evidence": evidence,
            "source_index": bar.source_index,
            "confirmed_at": bar.source_index,
            "structure_index": last_bi.end_index,
            "level": level,
        })
    return points


EVENT_PROFILE = (
    {
        "id": "chan.buy-observation",
        "name": "缠论买点观察",
        "signals_all": [],
        "signals_any": ["buy:first", "buy:second", "buy:third"],
        "signals_not": ["sell:first", "sell:second", "sell:third"],
    },
    {
        "id": "chan.sell-observation",
        "name": "缠论卖点观察",
        "signals_all": [],
        "signals_any": ["sell:first", "sell:second", "sell:third"],
        "signals_not": ["buy:first", "buy:second", "buy:third"],
    },
)


def _evaluate_events(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        grouped.setdefault(str(point["date"]), []).append(point)
    events: list[dict[str, Any]] = []
    for date, current in grouped.items():
        tokens = {f"{point['type']}:{point['class']}" for point in current}
        for rule in EVENT_PROFILE:
            signals_all = set(rule["signals_all"])
            signals_any = set(rule["signals_any"])
            signals_not = set(rule["signals_not"])
            matched = (
                signals_all.issubset(tokens)
                and (not signals_any or bool(signals_any & tokens))
                and not bool(signals_not & tokens)
            )
            if not matched:
                continue
            events.append({
                "date": date,
                "event_id": rule["id"],
                "name": rule["name"],
                "matched_signals": sorted(tokens),
                "signals_all": list(rule["signals_all"]),
                "signals_any": list(rule["signals_any"]),
                "signals_not": list(rule["signals_not"]),
                "teaching_only": True,
            })
    return events


class IncrementalStructureAnalyzer:
    """Stateful CZSC port used by live training and deterministic replay."""

    def __init__(self, *, level: str, signal_profile_id: str = SIGNAL_PROFILE_ID) -> None:
        if signal_profile_id not in SIGNAL_PROFILES:
            raise ValueError(f"unknown Chan signal profile: {signal_profile_id}")
        self.level = level
        self.signal_profile_id = signal_profile_id
        self.analyzer = _Analyzer([])
        self.points: list[dict[str, Any]] = []
        self.seen_points: set[tuple[str, str, int]] = set()
        self._replay: list[dict[str, Any]] = []
        self._last_signature: tuple[Any, ...] | None = None
        self._next_source_index = 0

    def update_raw(self, bar: _RawBar) -> None:
        replacing_latest = bool(
            self.analyzer.raw_bars
            and self.analyzer.raw_bars[-1].date == bar.date
        )
        if replacing_latest:
            self.points = [
                point
                for point in self.points
                if point["date"] != bar.date
                and point["source_index"] != bar.source_index
            ]
            self.seen_points = {
                (
                    str(point["type"]),
                    str(point["class"]),
                    int(point["structure_index"]),
                )
                for point in self.points
            }
            self._replay = [
                item
                for item in self._replay
                if item["date"] != bar.date
                and item["source_index"] != bar.source_index
            ]
            self._last_signature = None
        self.analyzer.update(bar)
        added: list[dict[str, Any]] = []
        enabled_classes = SIGNAL_PROFILES[self.signal_profile_id]
        for point in _current_signal_points(self.analyzer, bar, self.level):
            if point["class"] not in enabled_classes:
                continue
            key = (
                str(point["type"]),
                str(point["class"]),
                int(point["structure_index"]),
            )
            if key in self.seen_points:
                continue
            self.seen_points.add(key)
            self.points.append(point)
            added.append(point)
        bis = self.analyzer.finished_bis
        confirmed_fx_count = sum(max(0, len(bi.fxs) - 1) for bi in self.analyzer.bi_list)
        pending_fx_count = len(_check_fxs(self.analyzer.bars_ubi))
        signature = (
            confirmed_fx_count + pending_fx_count,
            len(bis),
            bis[-1].end_index if bis else None,
            tuple((point["type"], point["class"], point["structure_index"]) for point in added),
        )
        if signature != self._last_signature:
            self._replay.append({
                "date": bar.date,
                "source_index": bar.source_index,
                "fractals": signature[0],
                "strokes": signature[1],
                "last_stroke_end": signature[2],
                "added_signals": [point["signal_key"] for point in added],
            })
            self._last_signature = signature

    def update(self, row: dict[str, Any], source_index: int | None = None) -> bool:
        if source_index is None:
            date = str(row.get("datetime") or row["date"])
            if self.analyzer.raw_bars and self.analyzer.raw_bars[-1].date == date:
                index = self.analyzer.raw_bars[-1].source_index
            else:
                index = self._next_source_index
        else:
            index = source_index
        bar = _raw_bar(row, index)
        if bar is None:
            return False
        self._next_source_index = max(self._next_source_index, index + 1)
        self.update_raw(bar)
        return True

    def snapshot(self, *, include_replay: bool = False) -> dict[str, Any]:
        analyzer = self.analyzer
        bis = analyzer.finished_bis
        points = [dict(point) for point in self.points]
        result = {
            "engine": ENGINE_NAME,
            "algorithm_source": SOURCE_COMMIT,
            "signal_profile": SIGNAL_PROFILE,
            "signal_profile_id": self.signal_profile_id,
            "signal_catalog": signal_catalog(),
            "event_profile": [dict(rule) for rule in EVENT_PROFILE],
            "events": _evaluate_events(points),
            "indicator_cache": analyzer.indicator_snapshot(),
            "fractals": [{
                "date": item.date,
                "type": item.mark,
                "price": round(item.price, 4),
                "source_index": item.source_index,
                "confirmed_at": item.confirmed_at,
                "level": self.level,
            } for item in analyzer.fx_list],
            "strokes": [{
                "start_date": item.start_date,
                "end_date": item.end_date,
                "direction": item.direction,
                "start_price": round(item.start_price, 4),
                "end_price": round(item.end_price, 4),
                "start_index": item.start_index,
                "end_index": item.end_index,
                "confirmed_at": item.confirmed_at,
                "level": self.level,
            } for item in bis],
            "centers": _build_centers(bis, level=self.level),
            "points": points,
        }
        if include_replay:
            result["replay"] = list(self._replay)
        return result


def analyze_structure(
    rows: list[dict[str, Any]],
    *,
    level: str,
    include_replay: bool = False,
) -> dict[str, Any]:
    runtime = IncrementalStructureAnalyzer(level=level)
    for bar in _clean_raw_bars(rows):
        runtime.update_raw(bar)
    return runtime.snapshot(include_replay=include_replay)
