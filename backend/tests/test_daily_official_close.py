"""Post-close daily bars must be rebuilt from the official batch source."""
from __future__ import annotations

from datetime import date, datetime
from datetime import time as dt_time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.jobs import daily_pipeline
from app.market_time import CN_TZ


def test_post_close_does_not_choose_realtime_quote_snapshot(monkeypatch) -> None:
    del monkeypatch
    now = datetime.combine(date(2026, 10, 16), dt_time(15, 30), tzinfo=CN_TZ)

    assert daily_pipeline._should_refresh_today_from_quotes(now) is False


def test_intraday_still_uses_realtime_quote_snapshot(monkeypatch) -> None:
    del monkeypatch
    now = datetime.combine(date(2026, 10, 16), dt_time(14, 59), tzinfo=CN_TZ)

    assert daily_pipeline._should_refresh_today_from_quotes(now) is True


def test_enabled_minute_sync_reports_zero_row_failure() -> None:
    assert daily_pipeline._minute_sync_error(["600865.SH"], 0) == (
        "sync_minute: no rows written"
    )
    assert daily_pipeline._minute_sync_error(["600865.SH"], 240) is None
    assert daily_pipeline._minute_sync_error([], 0) is None


@pytest.mark.parametrize("hour,minute,expected", [(14, 59, "quotes"), (15, 30, "batch"), (18, 0, "batch")])
def test_actual_pipeline_selects_official_batch_after_close(monkeypatch, tmp_path, hour, minute, expected):
    """Exercise the branch, not just its otherwise-unused time helper."""
    from app.services import data_integrity, preferences

    class StopAfterFetchError(Exception):
        pass

    today = date(2026, 10, 16)
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: today)
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: datetime.combine(today, dt_time(hour, minute), tzinfo=CN_TZ))
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda _: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *args: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *args: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *args, **kwargs: [])
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(preferences, "get_pipeline_pull_a_share", lambda: True)
    quotes = Mock(side_effect=StopAfterFetchError)
    batch = Mock(side_effect=StopAfterFetchError)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_daily_by_quotes", quotes)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_daily_batch", batch)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: today)

    with pytest.raises(StopAfterFetchError):
        daily_pipeline.run_now(repo, SimpleNamespace(has=lambda cap: True))

    assert quotes.call_count == (expected == "quotes")
    assert batch.call_count == (expected == "batch")
