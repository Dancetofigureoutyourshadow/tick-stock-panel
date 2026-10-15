from datetime import date, timedelta

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.indicators.pipeline import compute_indicators
from app.tickflow.repository import DataStore, KlineRepository, _last_available_rows


@pytest.mark.parametrize("cutoff", [date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 9)])
def test_last_available_rows_preserves_order_nulls_and_cutoff(cutoff):
    frame = pl.DataFrame({
        "date": [date(2026, 1, 2), date(2026, 1, 4), date(2026, 1, 2),
                 date(2026, 1, 3), None],
        "symbol": ["B", "B", "A", "A", "C"],
        "value": [1.0, None, 3.0, None, 5.0],
        "_row_index": [10, 11, 12, 13, 14],
    })
    expected = frame.filter(pl.col("date") <= cutoff).group_by(
        "symbol", maintain_order=True,
    ).last()
    assert_frame_equal(_last_available_rows(frame, cutoff), expected, check_exact=True)
    assert_frame_equal(_last_available_rows(frame.head(0), cutoff), frame.head(0))


def test_live_agg_does_not_filter_full_history_state_columns(tmp_path, monkeypatch):
    rows = []
    for stock in range(3):
        for day in range(220 - stock * 3):
            close = 10.0 + stock + day * 0.02 + (day % 7) * 0.03
            rows.append({
                "symbol": f"{600000 + stock}.SH",
                "date": date(2025, 1, 1) + timedelta(days=day),
                "open": close - 0.1, "high": close + 0.2, "low": close - 0.2,
                "close": close, "volume": 1000.0 + day, "amount": close * 1000,
                "raw_close": close, "raw_high": close + 0.2, "raw_low": close - 0.2,
            })
    history = compute_indicators(pl.DataFrame(rows).sort("symbol", "date")).with_columns(
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_ups"),
        pl.lit(0, dtype=pl.UInt32).alias("consecutive_limit_downs"),
    )
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = history
    cutoff = history["date"].max() - timedelta(days=1)
    repo._build_live_agg(cutoff)
    expected = repo._live_agg_cache.clone()
    original_filter = pl.DataFrame.filter
    def bounded_filter(frame, *args, **kwargs):
        if "ema5" in frame.columns and "atr_14" in frame.columns:
            assert frame.height <= 3, "live state extraction copies full historical state columns"
        return original_filter(frame, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "filter", bounded_filter)
    repo._build_live_agg(cutoff)
    assert_frame_equal(
        repo._live_agg_cache.sort("symbol"), expected.sort("symbol"), check_exact=True,
    )


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_refresh_releases_history_while_latest_and_live_cache_remain_available(
    tmp_path, monkeypatch, background, fails,
):
    repo = KlineRepository(DataStore(tmp_path))
    latest = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 1, 3)]})
    repo._enriched_cache = latest
    repo._enriched_cache_date = date(2026, 1, 3)
    repo._live_agg_cache = latest
    repo._enriched_history_cache = latest
    repo._enriched_history_start = date(2026, 1, 3)
    repo._enriched_history_generation = "old"
    repo._enriched_warming = background

    def rebuild():
        assert repo._enriched_history_cache is None, "refresh retains the old wide history"
        assert repo._enriched_history_start is None
        assert repo._enriched_history_generation is None
        assert repo._enriched_warming
        assert repo.get_enriched_latest()[0] is latest
        assert repo._live_agg_cache is latest
        assert repo.get_enriched_range(date(2026, 1, 1), date(2026, 1, 3)) is None
        if fails:
            raise RuntimeError("rebuild failed")

    monkeypatch.setattr(repo, "_refresh_enriched_impl", rebuild)
    if fails:
        with pytest.raises(RuntimeError, match="rebuild failed"):
            repo._refresh_enriched()
    else:
        repo._refresh_enriched()
    assert repo._enriched_warming is background
    assert repo.get_enriched_latest()[0] is latest
