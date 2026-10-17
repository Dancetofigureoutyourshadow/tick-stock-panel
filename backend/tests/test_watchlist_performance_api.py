from datetime import datetime
from types import SimpleNamespace

import polars as pl
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import watchlist as watchlist_api


def test_performance_returns_minute_baseline_and_keeps_missing_symbols(monkeypatch):
    entries = [
        {"symbol": symbol, "added_at": "2026-10-16T05:30:00"}
        for symbol in ("603995.SH", "600000.SH", "000001.SH")
    ]
    monkeypatch.setattr(watchlist_api.watchlist, "list_symbols", lambda: entries)
    monkeypatch.setattr(watchlist_api.watchlist, "revision", lambda: 123)

    class Repo:
        def get_etf_symbol_set(self):
            return set()

        def get_index_symbol_set(self):
            return {"000001.SH"}

        def get_minute_by_dates(self, symbols, dates, *, asset_type):
            assert symbols == ["603995.SH", "600000.SH"]
            assert asset_type == "stock"
            assert dates == [datetime(2026, 10, 16).date()]
            return pl.DataFrame({
                "symbol": ["603995.SH"] * 2,
                "datetime": [datetime(2026, 10, 16, 13, 29), datetime(2026, 10, 16, 13, 31)],
                "close": [25.4, 25.8],
            })

    app = FastAPI()
    app.state.repo = Repo()
    app.include_router(watchlist_api.router)
    response = TestClient(app).get("/api/watchlist/performance")
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 3
    assert rows[0] == {
        **entries[0], "base_price": 25.4, "base_time": "2026-10-16T13:29:00",
    }
    assert all(row["base_price"] is None and row["base_time"] is None for row in rows[1:])


def test_empty_watchlist_does_not_read_repository(monkeypatch):
    monkeypatch.setattr(watchlist_api.watchlist, "list_symbols", lambda: [])
    request = SimpleNamespace()
    assert watchlist_api.watchlist_performance(request) == {"rows": [], "elapsed_ms": 0}
