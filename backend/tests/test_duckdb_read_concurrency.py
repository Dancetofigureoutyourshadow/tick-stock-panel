"""DuckDB 读并发与视图重建串行 — repository 连接模型的回归锁。

背景: 原实现单连接全局互斥, 同步期任一长查询(全视图扫描)把所有交互读
堵在门外直到前端 30s 超时(/status 事故)。改为:
- execute_all/execute_one 走独立 cursor(=同一内存库实例的独立连接,
  MVCC 快照隔离), 用信号量限并发数而非全局互斥;
- 运行期视图重建一律走 refresh_view(_ddl_lock 串行)。

duckdb 1.5.3 实测依据: 并发读 + 换视图零异常(两种视图版本都被观察到);
并发 CREATE OR REPLACE VIEW 同名视图抛 Catalog write-write conflict,
因此 DDL 必须串行 —— 旧代码两处同步路径同时刷视图会静默失败。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.tickflow.repository import (
    DataStore,
    KlineRepository,
    _read_concurrency_env,
)


def _write_partition(root: Path, name: str, rows: int) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "part.parquet").unlink(missing_ok=True)
    pl.DataFrame({"x": list(range(rows))}).write_parquet(d / "part.parquet")
    return d


class _SlowCursor:
    """记录并发数并模拟耗时查询的假 cursor (避免依赖真实查询时长, 不抖动)。"""

    def __init__(self, tracker: dict) -> None:
        self._tracker = tracker

    def execute(self, sql: str, params: list | None = None) -> _SlowCursor:
        with self._tracker["lock"]:
            self._tracker["cur"] += 1
            self._tracker["max"] = max(self._tracker["max"], self._tracker["cur"])
        time.sleep(0.12)
        with self._tracker["lock"]:
            self._tracker["cur"] -= 1
        return self

    def fetchall(self) -> list[tuple]:
        return [(1,)]

    def fetchone(self) -> tuple:
        return (1,)

    def close(self) -> None:
        pass


def _read_overlap(
    tmp_path: Path,
    monkeypatch,
    env_value: str,
    n_threads: int,
) -> int:
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", env_value)
    repo = KlineRepository(DataStore(tmp_path))
    tracker: dict = {"lock": threading.Lock(), "cur": 0, "max": 0}
    monkeypatch.setattr(repo, "_new_cursor", lambda: _SlowCursor(tracker))

    threads = [
        threading.Thread(target=repo.execute_all, args=("SELECT 1",))
        for _ in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return tracker["max"]


def test_reads_overlap_up_to_limiter(tmp_path: Path, monkeypatch) -> None:
    """并发读 = 信号量容量 (默认 3), 而非旧的全局串行 (1)。"""
    assert _read_overlap(tmp_path, monkeypatch, "3", 5) == 3


def test_reads_serialize_when_concurrency_one(tmp_path: Path, monkeypatch) -> None:
    """TICK_DUCKDB_READ_CONCURRENCY=1 恢复完全串行的旧行为。"""
    assert _read_overlap(tmp_path, monkeypatch, "1", 4) == 1


def test_read_concurrency_env_clamped(monkeypatch) -> None:
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", "0")
    assert _read_concurrency_env() == 1
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", "99")
    assert _read_concurrency_env() == 16
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", "abc")
    assert _read_concurrency_env() == 3
    monkeypatch.delenv("TICK_DUCKDB_READ_CONCURRENCY", raising=False)
    assert _read_concurrency_env() == 3


def test_refresh_view_updates_definition(tmp_path: Path) -> None:
    """视图重建生效: 指向新 glob 后查询立即取新数据。"""
    repo = KlineRepository(DataStore(tmp_path))
    a = _write_partition(tmp_path, "tbl_a", 2)
    b = _write_partition(tmp_path, "tbl_b", 5)

    assert repo.refresh_view("v_switch", f"{a}/**/*.parquet") is True
    assert repo.execute_one("SELECT count(*) FROM v_switch")[0] == 2
    assert repo.refresh_view("v_switch", f"{b}/**/*.parquet") is True
    assert repo.execute_one("SELECT count(*) FROM v_switch")[0] == 5


def test_refresh_view_rejects_invalid_name(tmp_path: Path) -> None:
    """非标识符视图名直接拒绝, 不拼进 DDL。"""
    repo = KlineRepository(DataStore(tmp_path))
    a = _write_partition(tmp_path, "tbl_a", 2)
    assert repo.refresh_view("evil; DROP VIEW x", f"{a}/**/*.parquet") is False


def test_concurrent_refresh_view_never_conflicts(tmp_path: Path) -> None:
    """DDL 串行: 并发重建同名视图全部成功 (无 write-write conflict),
    且换视图期间并发读者始终读到某个一致版本 (MVCC)。"""
    repo = KlineRepository(DataStore(tmp_path))
    a = _write_partition(tmp_path, "tbl_a", 10)
    b = _write_partition(tmp_path, "tbl_b", 20)
    assert repo.refresh_view("v_swap", f"{a}/**/*.parquet") is True

    results: list[bool] = []
    seen_counts: set[int] = set()
    errors: list[BaseException] = []

    def ddl_worker(src: Path, rounds: int) -> None:
        try:
            results.extend(
                repo.refresh_view("v_swap", f"{src}/**/*.parquet")
                for _ in range(rounds)
            )
        except BaseException as exc:  # noqa: BLE001 — 测试内收集任意失败
            errors.append(exc)

    def reader(rounds: int) -> None:
        try:
            for _ in range(rounds):
                seen_counts.add(repo.execute_one("SELECT count(*) FROM v_swap")[0])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=ddl_worker, args=(a, 40)),
        threading.Thread(target=ddl_worker, args=(b, 40)),
        threading.Thread(target=reader, args=(120,)),
        threading.Thread(target=reader, args=(120,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert all(results), "并发视图重建出现 write-write conflict (DDL 未串行)"
    assert seen_counts == {10, 20}, "读者应看到两个一致版本, 不应有撕裂值"


@pytest.mark.parametrize("entry", ["rebuild_views", "rebuild_view", "extend_history"])
@pytest.mark.parametrize("keep_etf", [False, True])
def test_rebuild_drops_empty_views_on_independent_cursors(
    tmp_path: Path, monkeypatch, caplog, entry: str, keep_etf: bool,
) -> None:
    store = DataStore(tmp_path)
    root = store.db
    try:
        repo = KlineRepository(store)
        for directory, symbol in (("kline_minute", "600000.SH"), ("kline_etf_minute", "510300.SH")):
            pl.DataFrame({
                "symbol": [symbol], "datetime": [datetime(2026, 10, 16, 9, 35)],
                "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
                "volume": [100.0], "amount": [1050.0],
            }).write_parquet(tmp_path / directory / "part.parquet")
        repo.rebuild_views()
        assert repo.execute_one("SELECT count(*) FROM kline_minute_all") == (2,)
        (tmp_path / "kline_minute" / "part.parquet").unlink()
        if not keep_etf:
            (tmp_path / "kline_etf_minute" / "part.parquet").unlink()

        cursors = []

        def tracked_cursor():
            cursor = MagicMock(wraps=root.cursor())
            cursors.append(cursor)
            return cursor

        guarded_root = MagicMock(wraps=root)
        guarded_root.execute.side_effect = AssertionError("runtime DDL must use a cursor")
        guarded_root.cursor.side_effect = tracked_cursor
        monkeypatch.setattr(store, "db", guarded_root)
        monkeypatch.setattr(repo, "db", guarded_root)
        refresh = MagicMock(wraps=repo.refresh_view)
        monkeypatch.setattr(repo, "refresh_view", refresh)
        caplog.clear()

        if entry == "extend_history":
            from app.services.extend_history import _refresh_single_view
            _refresh_single_view(repo, "kline_minute")
        elif entry == "rebuild_view":
            repo.rebuild_view("kline_minute")
        else:
            repo.rebuild_views()

        views = {row[0] for row in repo.execute_all("SELECT view_name FROM duckdb_views()")}
        assert "kline_minute" not in views
        assert ("kline_etf_minute" in views) is keep_etf
        assert ("kline_minute_all" in views) is keep_etf
        if keep_etf:
            assert repo.execute_all("SELECT symbol, asset_type FROM kline_minute_all") == [
                ("510300.SH", "etf"),
            ]
        assert [call.args[0] for call in refresh.call_args_list] == (
            ["kline_etf_minute"] if keep_etf else []
        )
        assert not [record for record in caplog.records if record.levelname == "WARNING"]
        guarded_root.execute.assert_not_called()
        assert cursors
        for cursor in cursors:
            cursor.close.assert_called_once_with()
    finally:
        root.close()


@pytest.mark.parametrize("method", ["execute_all", "execute_one"])
@pytest.mark.parametrize("failure_stage", ["registration", "query"])
def test_lazy_read_failure_closes_cursor_and_releases_limiter(
    tmp_path: Path, monkeypatch, method: str, failure_stage: str,
) -> None:
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", "1")
    store = DataStore(tmp_path, register_views=False)
    try:
        repo = KlineRepository(store)
        cursor = MagicMock()
        monkeypatch.setattr(repo, "_new_cursor", lambda: cursor)
        failure = RuntimeError("injected failure")
        if failure_stage == "registration":
            monkeypatch.setattr(store, "_register_views", MagicMock(side_effect=failure))
        else:
            def execute(sql, params=None):
                if sql == "SELECT 1":
                    raise failure
                return cursor

            cursor.execute.side_effect = execute
        with pytest.raises(RuntimeError, match="injected failure"):
            getattr(repo, method)("SELECT 1")
        cursor.close.assert_called_once_with()
        assert store._views_registered is (failure_stage == "query")
        assert repo._read_limiter.acquire(blocking=False)
        repo._read_limiter.release()
    finally:
        store.db.close()
