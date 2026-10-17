from datetime import date

import polars as pl

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

    def register(store):
        registered.append(True)
        original(store)

    monkeypatch.setattr(DataStore, "_register_views", register)
    store = DataStore(tmp_path, register_views=False)
    try:
        repo = KlineRepository(store)
        assert repo.get_instruments()["symbol"].to_list() == ["603995.SH"]
        assert registered == []
        assert repo.execute_one("SELECT count(*) FROM kline_daily") == (1,)
        assert repo.execute_all("SELECT symbol FROM kline_daily") == [("603995.SH",)]
        assert registered == [True]
    finally:
        store.db.close()
