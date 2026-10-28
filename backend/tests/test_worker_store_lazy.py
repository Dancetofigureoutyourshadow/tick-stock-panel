import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import MagicMock

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


def test_worker_store_reads_instruments_without_binding_history_and_preserves_sql(tmp_path, monkeypatch):
    daily = tmp_path / "kline_daily" / "date=2026-10-16"
    daily.mkdir(parents=True)
    pl.DataFrame({"symbol": ["603995.SH"], "date": [date(2026, 10, 16)], "close": [25.4]}).write_parquet(daily / "part.parquet")
    instruments = tmp_path / "instruments"
    instruments.mkdir()
    pl.DataFrame({"symbol": ["603995.SH"], "name": ["甬金股份"]}).write_parquet(instruments / "part.parquet")
    original = DataStore._register_views
    registered = []

    def register(store, con=None):
        registered.append(con)
        original(store, con)

    monkeypatch.setattr(DataStore, "_register_views", register)
    store = DataStore(tmp_path, register_views=False)
    try:
        repo = KlineRepository(store)
        assert repo.get_instruments()["symbol"].to_list() == ["603995.SH"]
        assert registered == []
        assert repo.execute_one("SELECT count(*) FROM kline_daily") == (1,)
        assert repo.execute_all("SELECT symbol FROM kline_daily") == [("603995.SH",)]
        assert len(registered) == 1
        assert registered[0] is not None and registered[0] is not store.db
    finally:
        store.db.close()


@pytest.mark.parametrize("method", ["execute_all", "execute_one"])
def test_concurrent_first_reads_register_all_views_once_on_query_cursor(
    tmp_path, monkeypatch, method,
):
    monkeypatch.setenv("TICK_DUCKDB_READ_CONCURRENCY", "2")
    store = DataStore(tmp_path, register_views=False)
    root = store.db
    try:
        pl.DataFrame({
            "symbol": ["603995.SH"], "date": [date(2026, 10, 16)],
            "open": [25.0], "high": [26.0], "low": [24.0], "close": [25.4],
            "volume": [100.0], "amount": [2540.0],
        }).write_parquet(tmp_path / "kline_daily" / "part.parquet")
        # 这些视图不在 rebuild_views 的集合内，首次 SQL 必须完成完整注册。
        for directory in ("depth5", "financials/metrics", "instruments_ext", "kline_ext"):
            pl.DataFrame({"value": [1]}).write_parquet(tmp_path / directory / "part.parquet")

        guarded_root = MagicMock(wraps=root)
        guarded_root.execute.side_effect = AssertionError("runtime SQL must use a cursor")
        monkeypatch.setattr(store, "db", guarded_root)
        repo = KlineRepository(store)
        register = MagicMock(wraps=store._register_views)
        monkeypatch.setattr(store, "_register_views", register)

        # 两个读者先同时观察 False，再竞争 DDL 锁，确定性覆盖锁内双检查。
        ddl_barrier = threading.Barrier(2)
        ddl_lock = repo._ddl_lock
        tracked_lock = MagicMock()

        def enter_ddl():
            ddl_barrier.wait(timeout=10)
            return ddl_lock.__enter__()

        tracked_lock.__enter__.side_effect = enter_ddl
        tracked_lock.__exit__.side_effect = ddl_lock.__exit__
        monkeypatch.setattr(repo, "_ddl_lock", tracked_lock)

        sql = "SELECT symbol, asset_type FROM kline_daily_all WHERE symbol = ?"
        query_barrier = threading.Barrier(2)
        new_cursor = repo._new_cursor
        cursors = []

        def tracked_cursor():
            raw = new_cursor()
            cursor = MagicMock(wraps=raw)

            def execute(statement, params=None):
                if statement == sql:
                    # 若普通查询仍持 DDL 锁，另一个读者无法抵达此处。
                    query_barrier.wait(timeout=10)
                return raw.execute(statement, params or [])

            cursor.execute.side_effect = execute
            cursors.append(cursor)
            return cursor

        monkeypatch.setattr(repo, "_new_cursor", tracked_cursor)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(getattr(repo, method), sql, ["603995.SH"]) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]

        row = ("603995.SH", "stock")
        assert results == ([row] if method == "execute_one" else [[row]]) * 2
        assert store._views_registered is True
        register.assert_called_once()
        assert any(register.call_args.args[0] is cursor for cursor in cursors)
        assert tracked_lock.__enter__.call_count == 2
        guarded_root.execute.assert_not_called()
        for cursor in cursors:
            cursor.close.assert_called_once_with()
        views = {row[0] for row in root.execute("SELECT view_name FROM duckdb_views()").fetchall()}
        assert {"depth5", "financials_metrics", "instruments_ext", "kline_ext"} <= views
    finally:
        root.close()
