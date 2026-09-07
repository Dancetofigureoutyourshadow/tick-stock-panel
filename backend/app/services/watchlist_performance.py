"""自选加入时间点的历史基准价查询。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import polars as pl

from app.market_time import CN_TZ
from app.services import watchlist

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 300.0
_CACHE_LOCK = threading.Lock()
_CACHE_KEY: tuple[int, int] | None = None
_CACHE_AT = 0.0
_CACHE_VALUE: tuple[dict[str, float], dict[str, str]] = ({}, {})


def _parse_added_at(value: object) -> datetime | None:
    """把历史 UTC 自选时间转换为无时区的北京时间墙钟。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # watchlist.add 历史上使用 datetime.utcnow()，但没有写入时区后缀。
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(CN_TZ).replace(tzinfo=None)


def reference_prices_from_minutes(
    repo: object,
    entries: list[dict],
    etf_set: set[str],
    index_set: set[str],
) -> tuple[dict[str, float], dict[str, str]]:
    """取每条自选时间点之前最近一根分钟 K 的收盘价。"""
    targets: dict[str, list[tuple[str, datetime]]] = {"stock": [], "etf": []}
    for entry in entries:
        symbol = str(entry.get("symbol") or "")
        target = _parse_added_at(entry.get("added_at"))
        if not symbol or target is None or symbol in index_set:
            continue
        asset_type = "etf" if symbol in etf_set else "stock"
        targets[asset_type].append((symbol, target))

    prices: dict[str, float] = {}
    price_times: dict[str, str] = {}
    for asset_type, asset_targets in targets.items():
        if not asset_targets:
            continue
        symbols = [symbol for symbol, _ in asset_targets]
        dates = sorted({target.date() for _, target in asset_targets})
        try:
            frame = repo.get_minute_by_dates(symbols, dates, asset_type=asset_type)
        except Exception:  # noqa: BLE001
            logger.debug(
                "read watchlist baseline minute data failed for %s",
                asset_type,
                exc_info=True,
            )
            continue
        if (
            not isinstance(frame, pl.DataFrame)
            or frame.is_empty()
            or not {"symbol", "datetime", "close"}.issubset(frame.columns)
        ):
            continue

        target_frame = pl.DataFrame(
            {
                "symbol": symbols,
                "_target_at": [target for _, target in asset_targets],
            },
            schema_overrides={"symbol": pl.Utf8, "_target_at": pl.Datetime},
        )
        selected = (
            frame
            .select([
                pl.col("symbol").cast(pl.Utf8),
                pl.col("datetime").cast(pl.Datetime, strict=False),
                pl.col("close").cast(pl.Float64, strict=False),
            ])
            .drop_nulls(["symbol", "datetime", "close"])
            .filter(pl.col("close").is_finite() & (pl.col("close") > 0))
            .join(target_frame, on="symbol", how="inner")
            .filter(pl.col("datetime") <= pl.col("_target_at"))
            .sort(["symbol", "datetime"])
            .group_by("symbol", maintain_order=True)
            .agg([
                pl.col("close").last().alias("price"),
                pl.col("datetime").last().alias("price_time"),
            ])
        )
        for row in selected.iter_rows(named=True):
            symbol = str(row["symbol"])
            prices[symbol] = float(row["price"])
            price_times[symbol] = row["price_time"].isoformat(timespec="seconds")
    return prices, price_times


def get_reference_prices(
    repo: object,
    entries: list[dict],
    etf_set: set[str],
    index_set: set[str],
) -> tuple[dict[str, float], dict[str, str]]:
    """缓存历史回溯，避免实时行情 SSE 每次刷新都扫描分钟分区。"""
    global _CACHE_AT, _CACHE_KEY, _CACHE_VALUE
    now = time.monotonic()
    key = (id(repo), watchlist.revision())
    with _CACHE_LOCK:
        if _CACHE_KEY == key and now - _CACHE_AT < _CACHE_TTL_S:
            prices, price_times = _CACHE_VALUE
            return dict(prices), dict(price_times)

    value = reference_prices_from_minutes(repo, entries, etf_set, index_set)
    with _CACHE_LOCK:
        _CACHE_KEY = key
        _CACHE_AT = now
        _CACHE_VALUE = value
    prices, price_times = value
    return dict(prices), dict(price_times)
