"""Auditable validation for canonical K-line rows."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import isfinite
from typing import Any

_SESSIONS = ((570, 690), (780, 900))  # 09:30-11:30, 13:00-15:00


def _timestamp(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(
        str(row.get("datetime") or row["date"]).replace("Z", "+00:00")
    )


def _in_trading_session(stamp: datetime) -> bool:
    minute = stamp.hour * 60 + stamp.minute
    return any(start <= minute < end for start, end in _SESSIONS)


def validate_kline_quality(
    rows: list[dict[str, Any]],
    *,
    intraday: bool = False,
) -> dict[str, Any]:
    """Return quality findings without mutating or repairing the input."""
    findings: dict[str, list[str]] = {
        "duplicate_timestamp": [],
        "out_of_order": [],
        "invalid_ohlc": [],
        "negative_volume": [],
        "off_session": [],
        "large_calendar_gap": [],
        "price_jump_gt_40pct": [],
    }
    previous_stamp: datetime | None = None
    previous_close: float | None = None
    stamp_texts: list[str] = []
    for index, row in enumerate(rows):
        label = str(row.get("datetime") or row.get("date") or index)
        try:
            stamp = _timestamp(row)
            stamp_texts.append(stamp.isoformat())
            if previous_stamp is not None:
                if stamp < previous_stamp:
                    findings["out_of_order"].append(label)
                if not intraday and (stamp.date() - previous_stamp.date()).days > 10:
                    findings["large_calendar_gap"].append(label)
            if intraday and not _in_trading_session(stamp):
                findings["off_session"].append(label)
            open_, high, low, close = (
                float(row[name]) for name in ("open", "high", "low", "close")
            )
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
    findings["duplicate_timestamp"] = sorted(
        key for key, count in duplicates.items() if count > 1
    )
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
    warning_count = sum(
        item["count"] for item in issues if item["severity"] == "warning"
    )
    return {
        "valid": error_count == 0,
        "row_count": len(rows),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
