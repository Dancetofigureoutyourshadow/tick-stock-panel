"""Market-data helpers isolated for Chan multi-level training."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from math import isfinite
from typing import Any

SUPPORTED_MINUTE_INTERVALS = (5, 15, 30, 60)
_SESSIONS = ((570, 690), (780, 900))  # 09:30-11:30, 13:00-15:00


def _timestamp(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row.get("datetime") or row["date"]).replace("Z", "+00:00"))


def _session_bucket(stamp: datetime, interval: int) -> tuple[int, int] | None:
    minute = stamp.hour * 60 + stamp.minute
    for session_id, (start, end) in enumerate(_SESSIONS):
        if start <= minute < end:
            return session_id, (minute - start) // interval
    return None


def _period_end(stamp: datetime, interval: int | str) -> datetime | None:
    """Return the stable close timestamp used to identify one aggregate bar."""
    if interval == "1d":
        return stamp.replace(hour=14, minute=59, second=0, microsecond=0)
    slot = _session_bucket(stamp, int(interval))
    if slot is None:
        return None
    session_id, bucket = slot
    start, end = _SESSIONS[session_id]
    minute = min(start + (bucket + 1) * int(interval), end) - 1
    return stamp.replace(
        hour=minute // 60,
        minute=minute % 60,
        second=0,
        microsecond=0,
    )


class ChanBarGenerator:
    """Stream one canonical minute bar into all configured Chan levels."""

    def __init__(
        self,
        intervals: Iterable[int | str] = (*SUPPORTED_MINUTE_INTERVALS, "1d"),
    ) -> None:
        self.intervals = tuple(intervals)
        for interval in self.intervals:
            if interval != "1d" and int(interval) not in SUPPORTED_MINUTE_INTERVALS:
                raise ValueError(f"unsupported Chan interval: {interval}")
        self.bars: dict[str, list[dict[str, Any]]] = {
            str(interval): [] for interval in self.intervals
        }
        self._last_keys: dict[str, tuple[Any, ...]] = {}

    def update(self, row: dict[str, Any]) -> set[str]:
        try:
            stamp = _timestamp(row)
            date_text = stamp.date().isoformat()
            open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
            if not all(isfinite(value) and value > 0 for value in (open_, high, low, close)):
                return set()
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
        except (KeyError, TypeError, ValueError):
            return set()
        updated: set[str] = set()
        for interval in self.intervals:
            name = str(interval)
            if interval == "1d":
                key: tuple[Any, ...] = (date_text,)
            else:
                slot = _session_bucket(stamp, int(interval))
                if slot is None:
                    continue
                key = (date_text, *slot)
            close_stamp = _period_end(stamp, interval)
            if close_stamp is None:
                continue
            current_bars = self.bars[name]
            if self._last_keys.get(name) != key:
                current_bars.append({
                    "date": date_text,
                    "datetime": close_stamp.isoformat(),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": amount,
                })
                self._last_keys[name] = key
                updated.add(name)
                continue
            current = current_bars[-1]
            current["high"] = max(float(current["high"]), high)
            current["low"] = min(float(current["low"]), low)
            current["close"] = close
            current["volume"] += volume
            current["amount"] += amount
            updated.add(name)
        return updated


def aggregate_minute_bars(rows: list[dict[str, Any]], interval: int | str) -> list[dict[str, Any]]:
    """Aggregate canonical minute rows with the A-share lunch break respected."""
    if interval == "1d":
        interval_minutes: int | None = None
    else:
        interval_minutes = int(interval)
        if interval_minutes not in SUPPORTED_MINUTE_INTERVALS:
            raise ValueError(f"unsupported Chan interval: {interval}")

    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        try:
            stamp = _timestamp(row)
            date_text = stamp.date().isoformat()
            if interval_minutes is None:
                key: tuple[Any, ...] = (date_text,)
            else:
                slot = _session_bucket(stamp, interval_minutes)
                if slot is None:
                    continue
                key = (date_text, *slot)
            close_stamp = _period_end(stamp, interval)
            if close_stamp is None:
                continue
            values = [float(row[name]) for name in ("open", "high", "low", "close")]
            if not all(isfinite(value) and value > 0 for value in values):
                continue
            volume = float(row.get("volume") or 0)
            amount = float(row.get("amount") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        current = buckets.get(key)
        if current is None:
            buckets[key] = {
                "date": date_text,
                "datetime": close_stamp.isoformat(),
                "open": values[0],
                "high": values[1],
                "low": values[2],
                "close": values[3],
                "volume": volume,
                "amount": amount,
            }
        else:
            current["high"] = max(float(current["high"]), values[1])
            current["low"] = min(float(current["low"]), values[2])
            current["close"] = values[3]
            current["volume"] += volume
            current["amount"] += amount
    return [buckets[key] for key in sorted(buckets)]


def build_multi_level_bars(
    rows: list[dict[str, Any]],
    intervals: Iterable[int | str] = (*SUPPORTED_MINUTE_INTERVALS, "1d"),
) -> dict[str, list[dict[str, Any]]]:
    generator = ChanBarGenerator(intervals)
    for row in rows:
        generator.update(row)
    return generator.bars


def validate_kline_quality(rows: list[dict[str, Any]], *, intraday: bool = False) -> dict[str, Any]:
    """Return auditable quality findings without mutating or silently repairing input."""
    findings: dict[str, list[str]] = {
        "duplicate_timestamp": [],
        "out_of_order": [],
        "invalid_ohlc": [],
        "negative_volume": [],
        "off_session": [],
        "large_calendar_gap": [],
        "price_jump_gt_40pct": [],
    }
    stamps: list[tuple[int, datetime]] = []
    previous_stamp: datetime | None = None
    previous_close: float | None = None
    stamp_texts: list[str] = []
    for index, row in enumerate(rows):
        label = str(row.get("datetime") or row.get("date") or index)
        try:
            stamp = _timestamp(row)
            stamps.append((index, stamp))
            stamp_texts.append(stamp.isoformat())
            if previous_stamp is not None:
                if stamp < previous_stamp:
                    findings["out_of_order"].append(label)
                if not intraday and (stamp.date() - previous_stamp.date()).days > 10:
                    findings["large_calendar_gap"].append(label)
            if intraday and _session_bucket(stamp, 5) is None:
                findings["off_session"].append(label)
            open_, high, low, close = (float(row[name]) for name in ("open", "high", "low", "close"))
            if (
                not all(isfinite(value) and value > 0 for value in (open_, high, low, close))
                or high < max(open_, close, low)
                or low > min(open_, close, high)
            ):
                findings["invalid_ohlc"].append(label)
            volume = float(row.get("volume") or 0)
            if not isfinite(volume) or volume < 0:
                findings["negative_volume"].append(label)
            if previous_close and abs(close / previous_close - 1) > 0.4:
                findings["price_jump_gt_40pct"].append(label)
            previous_close = close
            previous_stamp = stamp
        except (KeyError, TypeError, ValueError, OverflowError):
            findings["invalid_ohlc"].append(label)

    duplicates = Counter(stamp_texts)
    findings["duplicate_timestamp"] = sorted(key for key, count in duplicates.items() if count > 1)
    severities = {
        "duplicate_timestamp": "error",
        "out_of_order": "error",
        "invalid_ohlc": "error",
        "negative_volume": "error",
        "off_session": "warning",
        "large_calendar_gap": "warning",
        "price_jump_gt_40pct": "warning",
    }
    issues = [
        {
            "code": code,
            "severity": severities[code],
            "count": len(samples),
            "samples": samples[:10],
        }
        for code, samples in findings.items()
        if samples
    ]
    error_count = sum(item["count"] for item in issues if item["severity"] == "error")
    warning_count = sum(item["count"] for item in issues if item["severity"] == "warning")
    return {
        "valid": error_count == 0,
        "row_count": len(rows),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
