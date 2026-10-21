"""历史交易回放不能把今日实时K线拼接到请求窗口末尾。"""
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline

TODAY = date(2026, 10, 21)
HISTORY_END = date(2026, 7, 13)


def _frame(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * 4,
        "date": [date(2026, 7, 10), HISTORY_END, date(2026, 10, 20), TODAY],
        "open": [13.8, 13.8, 12.3, 12.3],
        "high": [14.0, 13.9, 12.5, 12.5],
        "low": [13.6, 13.4, 12.1, 12.1],
        "close": [13.85, 13.43, 12.3, 12.26],
        "volume": [1000.0] * 4,
        "amount": [13000.0] * 4,
        "ma5": [13.916, 13.752, 12.5, 12.432],
        "ma60": [14.34246, 14.35405, 13.5, 13.47067],
    })


class _Repo:
    def __init__(self, asset_type: str, frame: pl.DataFrame, fallback: bool):
        self.asset_type = asset_type
        self.frame = frame
        self.fallback = fallback
        self.live_calls = 0

    def resolve_asset_type(self, symbol):
        return self.asset_type

    def get_instruments(self):
        return pl.DataFrame()

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame()

    def get_daily_asset(self, asset_type, symbol, start, end):
        if self.fallback:
            return pl.DataFrame()
        return self.frame.filter((pl.col("date") >= start) & (pl.col("date") <= end))

    def get_enriched_latest_asset(self, asset_type, refresh=True):
        self.live_calls += 1
        return self.frame.tail(1), TODAY


@pytest.mark.parametrize("asset_type,symbol", [
    ("stock", "603317.SH"), ("etf", "510300.SH"), ("index", "000001.SH"),
])
@pytest.mark.parametrize("fallback", [False, True], ids=["local", "fetch"])
@pytest.mark.parametrize("start,end,expected_dates", [
    (date(2026, 7, 10), HISTORY_END, [date(2026, 7, 10), HISTORY_END]),
    (date(2026, 10, 20), TODAY, [date(2026, 10, 20), TODAY]),
    (TODAY, TODAY, [TODAY]),
    (date(2026, 10, 22), date(2026, 10, 23), []),
], ids=["historical", "including-today", "today-only", "future"])
def test_daily_respects_requested_window(
    monkeypatch, asset_type, symbol, fallback, start, end, expected_dates,
):
    frame = _frame(symbol)
    repo = _Repo(asset_type, frame, fallback)
    quote_calls = []

    def live_cache():
        quote_calls.append(True)
        return frame.tail(1), TODAY

    monkeypatch.setattr(kline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(kline.kline_sync, "sync_daily_batch", lambda *a, **kw: frame)
    # 已计算字段用于检查API窗口边界；指标公式由指标层测试负责。
    monkeypatch.setattr(kline, "compute_enriched", lambda raw, **kw: raw)
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    app.state.quote_service = SimpleNamespace(get_enriched_today=live_cache)
    app.state.capabilities = None

    response = TestClient(app).get("/api/kline/daily", params={
        "symbol": symbol, "start_date": start.isoformat(), "end_date": end.isoformat(),
    })

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["date"] for row in rows] == [day.isoformat() for day in expected_dates]
    if end == HISTORY_END:
        assert rows[-1]["close"] == 13.43
        assert rows[-1]["ma5"] == 13.752
        assert rows[-1]["ma60"] == 14.35405
    contains_today = start <= TODAY <= end
    assert bool(quote_calls if asset_type == "stock" else repo.live_calls) == contains_today
    if contains_today:
        assert rows[-1]["is_live"] is True
        assert rows[-1]["ma5"] == 12.432
        assert rows[-1]["ma60"] == 13.47067


def test_daily_empty_fallback_stays_empty(monkeypatch):
    monkeypatch.setattr(kline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(kline.kline_sync, "sync_daily_batch", lambda *a, **kw: pl.DataFrame())
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=_Repo("stock", pl.DataFrame(), True), capabilities=None,
    )))
    result = kline.get_daily(request, "603317.SH", start_date="2026-07-10", end_date="2026-07-13")
    assert result["rows"] == []
