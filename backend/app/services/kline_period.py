"""Period aggregation helpers for locally stored daily K lines."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

DAILY_PERIODS = ("day", "week", "month")

# Enough daily history for MA60/MACD/KDJ warm-up after resampling.
INDICATOR_LOOKBACK_DAYS = {
    "week": 60 * 7 + 60,
    "month": 60 * 31 + 180,
}


def full_period_bounds(start: date, end: date, period: str) -> tuple[date, date]:
    """Return calendar bounds for all periods touched by a date range."""
    if period == "week":
        return (
            start - timedelta(days=start.weekday()),
            end + timedelta(days=6 - end.weekday()),
        )
    if period == "month":
        next_month = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
        return start.replace(day=1), next_month - timedelta(days=1)
    if period == "day":
        return start, end
    raise ValueError(f"unsupported daily period: {period}")


def aggregate_daily_period(daily: pl.DataFrame, period: str) -> pl.DataFrame:
    """Aggregate daily OHLCV into weekly/monthly bars.

    The grouping key is the natural calendar week/month, while the returned
    date is the first actual trading date in that group.
    """
    if period not in DAILY_PERIODS:
        raise ValueError(f"unsupported daily period: {period}")
    if daily.is_empty() or period == "day":
        return daily.sort([column for column in ("symbol", "date") if column in daily.columns])

    required = {"date", "open", "high", "low", "close", "volume"}
    if not required <= set(daily.columns):
        return pl.DataFrame()

    frame = daily.with_columns(pl.col("date").cast(pl.Date, strict=False)).sort(
        [column for column in ("symbol", "date") if column in daily.columns]
    )
    if period == "week":
        period_key = (
            pl.col("date")
            - pl.duration(days=pl.col("date").dt.weekday().cast(pl.Int64) - 1)
        )
    else:
        period_key = pl.col("date").dt.month_start()

    frame = frame.with_columns(period_key.alias("_period_start"))
    group_keys = ["_period_start"]
    if "symbol" in frame.columns:
        group_keys.insert(0, "symbol")

    aggregations = [
        pl.col("date").first().alias("date"),
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    if "amount" in frame.columns:
        aggregations.append(pl.col("amount").sum().alias("amount"))

    return (
        frame.group_by(group_keys, maintain_order=True)
        .agg(aggregations)
        .sort([column for column in ("symbol", "date") if column in frame.columns])
        .select([
            column
            for column in ("symbol", "date", "open", "high", "low", "close", "volume", "amount")
            if column in frame.columns
        ])
    )
