"""storage 统计求和不变量测试 — total_size_mb 必须等于各部分之和, 不得重复累加。

历史 bug: other_dirs 循环被复制了两遍, 且 financials 既在 other_dirs 里又有专属
统计块 —— financials 被计入 3 次、pools/backtest_results/screener_results/ai_cache
各计入 2 次, total_size_mb 虚高。此测试用精确整 MB 文件锁定总和不变量。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.api import data as data_api
from app.api.data import _compute_storage, _safe_aggregate_adj_factor, _safe_aggregate_enriched
from app.services.data_statistics import parquet_kline_stats

MB = 1024 * 1024


def _write_mb(path: Path, mb: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * int(mb * MB))


def _build_tree(data_dir: Path) -> None:
    _write_mb(data_dir / "kline_daily" / "part.parquet", 1.0)
    _write_mb(data_dir / "financials" / "metrics" / "part.parquet", 2.0)
    _write_mb(data_dir / "pools" / "p.json", 1.0)
    _write_mb(data_dir / "backtest_results" / "r.json", 1.0)
    _write_mb(data_dir / "screener_results" / "s.json", 1.0)
    _write_mb(data_dir / "ai_cache" / "c.json", 1.0)
    _write_mb(data_dir / "capabilities.json", 0.5)  # 根目录散文件


def test_total_equals_sum_of_parts(tmp_path):
    _build_tree(tmp_path)
    stats = _compute_storage(tmp_path)

    parts_mb = (
        stats["daily_size_mb"]            # 1.0  (subdirs 表)
        + stats["financials_size_mb"]     # 2.0  (专属统计块)
        + 1.0 + 1.0 + 1.0 + 1.0           # pools/backtest/screener/ai_cache (单次循环)
        + 0.5                             # 根目录散文件
    )
    assert stats["total_size_mb"] == parts_mb == 7.5


def test_financials_counted_once_not_three_times(tmp_path):
    _build_tree(tmp_path)
    stats = _compute_storage(tmp_path)

    assert stats["financials_files"] == 1
    assert stats["financials_size_mb"] == 2.0
    # 旧 bug: financials 计 3 次 (6MB) + 其余 4 目录各计 2 次 (8MB) → 15.5
    assert stats["total_size_mb"] != 15.5


def test_missing_dirs_contribute_zero(tmp_path):
    _write_mb(tmp_path / "kline_daily" / "part.parquet", 1.0)
    stats = _compute_storage(tmp_path)

    # financials 目录不存在时不含其明细键 (既有行为), 且不贡献任何体积
    assert stats.get("financials_files", 0) == 0
    assert stats.get("financials_size_mb", 0.0) == 0.0
    assert stats["total_size_mb"] == 1.0


def test_enriched_schema_does_not_bind_all_historical_parquet(tmp_path, monkeypatch):
    folder = tmp_path / "kline_daily_enriched" / "date=2026-10-16"
    folder.mkdir(parents=True)
    pl.DataFrame({"symbol": ["603995.SH"], "close": [25.4]}).write_parquet(folder / "part.parquet")

    def no_polars_schema(*args, **kwargs):
        raise AssertionError("status must not enter the shared Polars runtime")

    monkeypatch.setattr(pl, "read_parquet_schema", no_polars_schema)

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def execute_all(self, sql):
            raise AssertionError("status must not DESCRIBE the full historical view")

        def execute_one(self, sql):
            assert sql == "SELECT count(DISTINCT symbol) FROM instruments"
            return (1,)

    stats = _safe_aggregate_enriched(Repo())
    assert stats["fields"] == 3  # 文件列 + Hive date 分区列
    assert stats["latest_date"] == "2026-10-16"


def test_factor_stats_use_daily_partition_dates_without_scanning_daily_history(tmp_path):
    for day in ("2026-10-15", "2026-10-16"):
        (tmp_path / "kline_daily" / f"date={day}").mkdir(parents=True)

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def execute_one(self, sql, params=None):
            assert "FROM kline_daily" not in sql
            assert params == ["2026-10-15", "2026-10-16"]
            return (4, 2, 2)

    stats = _safe_aggregate_adj_factor(Repo())
    assert stats == {
        "rows": 4, "symbols_covered": 2, "earliest_date": "2026-10-15",
        "latest_date": "2026-10-16", "trading_days": 2,
    }


def test_concurrent_status_reads_share_one_refresh_and_cache_empty_result(monkeypatch):
    monkeypatch.setitem(data_api._table_cache, "minute", None)
    monkeypatch.setitem(data_api._table_cache_ts, "minute", 0)
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(data_api._get_table_stats, "minute", fetch) for _ in range(4)]
        assert started.wait(5)
        release.set()
        assert [f.result(timeout=5) for f in pending] == [None] * 4
    assert calls == [True]


def test_invalidation_during_status_refresh_does_not_republish_stale_cache(monkeypatch):
    monkeypatch.setitem(data_api._table_cache, "minute", None)
    monkeypatch.setitem(data_api._table_cache_ts, "minute", 0)

    def fetch():
        data_api.invalidate_data_cache("minute")
        return {"rows": 1}

    assert data_api._get_table_stats("minute", fetch) == {"rows": 1}
    assert data_api._table_cache["minute"] is None
    assert data_api._get_table_stats("minute", lambda: {"rows": 2}) == {"rows": 2}


def test_index_stats_are_exact_without_a_database_query(tmp_path):
    root = tmp_path / "kline_index_daily"
    for day, symbols in [("2026-10-15", ["000001.SH", "399001.SZ"]), ("2026-10-16", ["000001.SH", "399006.SZ", None])]:
        folder = root / f"date={day}"
        folder.mkdir(parents=True)
        pl.DataFrame({"symbol": symbols, "close": [25.4] * len(symbols)}).write_parquet(folder / "part.parquet")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert data_api._safe_aggregate_index_daily(repo) == {
        "rows": 5, "earliest_date": "2026-10-15", "latest_date": "2026-10-16",
        "symbols_covered": 3, "trading_days": 2,
    }


def test_flat_parquet_statistics_count_distinct_dates_and_empty_directory(tmp_path):
    assert parquet_kline_stats(tmp_path) is None
    pl.DataFrame({
        "symbol": ["000001.SH"] * 3,
        "date": ["2026-10-15", "2026-10-16", "2026-10-16"],
    }).write_parquet(tmp_path / "part.parquet")
    assert parquet_kline_stats(tmp_path)["trading_days"] == 2


def test_parquet_statistics_cache_survives_restart_and_detects_file_changes(tmp_path, monkeypatch):
    from app.services import data_statistics

    root = tmp_path / "kline_index_daily"
    root.mkdir()
    path = root / "part.parquet"
    pl.DataFrame({"symbol": ["000001.SH"], "date": ["2026-10-15"]}).write_parquet(path)
    first = parquet_kline_stats(root)
    original = data_statistics.pq.ParquetFile

    def no_scan(*args, **kwargs):
        raise AssertionError("unchanged files must use the persisted statistics")

    monkeypatch.setattr(data_statistics.pq, "ParquetFile", no_scan)
    assert parquet_kline_stats(root) == first
    monkeypatch.setattr(data_statistics.pq, "ParquetFile", original)
    pl.DataFrame({"symbol": ["000001.SH", "399001.SZ"], "date": ["2026-10-16"] * 2}).write_parquet(path)
    updated = parquet_kline_stats(root)
    assert updated["rows"] == 2
    assert updated["latest_date"] == "2026-10-16"
    path.unlink()
    assert parquet_kline_stats(root) is None


def test_invalid_statistics_cache_and_unwritable_cache_do_not_hide_data(tmp_path, monkeypatch):
    from app.services import data_statistics

    root = tmp_path / "kline_index_daily"
    root.mkdir()
    pl.DataFrame({"symbol": ["000001.SH"], "date": ["2026-10-16"]}).write_parquet(root / "part.parquet")
    cache = tmp_path / ".data_statistics" / "kline_index_daily.json"
    cache.parent.mkdir()
    cache.write_text("[]")

    def readonly(*args, **kwargs):
        raise OSError("cache is read-only")

    monkeypatch.setattr(data_statistics.tempfile, "NamedTemporaryFile", readonly)
    assert parquet_kline_stats(root)["rows"] == 1


def test_financial_status_does_not_use_polars_collect(tmp_path, monkeypatch):
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["603995.SH", "603995.SH", None]}).write_parquet(path)

    def no_polars_read(*args, **kwargs):
        raise AssertionError("status must not enter the shared Polars runtime")

    monkeypatch.setattr(pl, "read_parquet", no_polars_read)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert data_api._safe_aggregate_financials(repo)["tables"]["shares"] == {"rows": 3, "symbols": 2}


def test_changed_partition_does_not_rescan_unchanged_history(tmp_path, monkeypatch):
    from app.services import data_statistics

    root = tmp_path / "kline_index_daily"
    root.mkdir()
    old, latest = root / "old.parquet", root / "latest.parquet"
    pl.DataFrame({"symbol": ["000001.SH"], "date": ["2026-10-15"]}).write_parquet(old)
    pl.DataFrame({"symbol": ["000001.SH"], "date": ["2026-10-16"]}).write_parquet(latest)
    assert parquet_kline_stats(root)["rows"] == 2
    pl.DataFrame({"symbol": ["000001.SH", "399001.SZ"], "date": ["2026-10-16"] * 2}).write_parquet(latest)
    original = data_statistics.pq.ParquetFile
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(data_statistics.pq, "ParquetFile", read)
    stats = parquet_kline_stats(root)
    assert stats["rows"] == 3 and stats["symbols_covered"] == 2
    assert reads == [latest]
