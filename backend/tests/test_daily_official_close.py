"""Post-close daily bars must be rebuilt from the official batch source."""
from __future__ import annotations

from datetime import datetime
from datetime import time as dt_time

from app.jobs import daily_pipeline
from app.market_time import CN_TZ, cn_today


def test_post_close_does_not_choose_realtime_quote_snapshot(monkeypatch) -> None:
    del monkeypatch
    now = datetime.combine(cn_today(), dt_time(15, 30), tzinfo=CN_TZ)

    assert daily_pipeline._should_refresh_today_from_quotes(now) is False


def test_intraday_still_uses_realtime_quote_snapshot(monkeypatch) -> None:
    del monkeypatch
    now = datetime.combine(cn_today(), dt_time(14, 59), tzinfo=CN_TZ)

    assert daily_pipeline._should_refresh_today_from_quotes(now) is True


def test_enabled_minute_sync_reports_zero_row_failure() -> None:
    assert daily_pipeline._minute_sync_error(["600865.SH"], 0) == (
        "sync_minute: no rows written"
    )
    assert daily_pipeline._minute_sync_error(["600865.SH"], 240) is None
    assert daily_pipeline._minute_sync_error([], 0) is None
