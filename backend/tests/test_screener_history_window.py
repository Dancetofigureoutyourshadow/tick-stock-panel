"""历史日期筛选的回看窗口必须锚定在用户选择的交易日。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.tickflow.repository import KlineRepository


def _trading_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def test_enriched_history_window_is_anchored_to_target_date() -> None:
    dates = _trading_dates(date(2026, 1, 1), 180)
    target = dates[-2]  # 缓存已经包含下一个交易日时回选前一天
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_history_cache = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * len(dates),
            "date": dates,
            "close": [float(i) for i in range(len(dates))],
        }
    )

    history = repo.get_enriched_history(target, lookback_days=5)

    assert history is not None
    assert history["date"].unique().sort().to_list() == dates[-7:-1]
