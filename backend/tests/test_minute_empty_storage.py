"""分钟 K 空存储的查询与视图刷新回归测试。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import polars as pl

from app.jobs.daily_pipeline import _refresh_single_view
from app.tickflow.repository import DataStore, KlineRepository


def _repo() -> KlineRepository:
    store = DataStore.__new__(DataStore)
    store.data_dir = Path(__file__).with_name("__empty_minute_storage__")
    store.db = duckdb.connect(":memory:")
    return KlineRepository(store)


def test_empty_minute_queries_do_not_scan_parquet(monkeypatch):
    repo = _repo()
    scan = MagicMock(side_effect=AssertionError("empty storage must not be scanned"))
    monkeypatch.setattr(pl, "scan_parquet", scan)

    assert repo.get_minute("600000.SH", date(2026, 8, 29)).is_empty()
    assert repo.get_minute_batch(["600000.SH"], date(2026, 8, 29)).is_empty()
    assert repo.get_minute_range(
        ["600000.SH"], date(2026, 8, 1), date(2026, 8, 29)
    ).is_empty()
    assert repo.get_minute("510300.SH", date(2026, 8, 29), asset_type="etf").is_empty()
    scan.assert_not_called()


def test_empty_minute_view_refresh_does_not_warn(caplog):
    repo = _repo()

    _refresh_single_view(repo, "kline_minute")
    repo.rebuild_views()

    assert not [
        record for record in caplog.records
        if "kline_minute" in record.getMessage() and record.levelname == "WARNING"
    ]


def test_minute_views_are_removed_when_storage_is_empty():
    repo = _repo()
    repo.db.execute("CREATE VIEW kline_minute AS SELECT 1 AS value")
    repo.db.execute("CREATE VIEW kline_minute_all AS SELECT 1 AS value")

    repo.rebuild_view("kline_minute")

    views = {row[0] for row in repo.execute_all("SELECT view_name FROM duckdb_views()")}
    assert "kline_minute" not in views
    assert "kline_minute_all" not in views
