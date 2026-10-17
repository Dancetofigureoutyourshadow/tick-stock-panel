from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper


def write_part(root, sub, day, symbol, close, extra=False):
    path = root / sub / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"symbol": symbol, "date": date.fromisoformat(day), "open": close, "close": close}
    if extra:
        row.update(prev_close=10.0, quote_ts=1_700_000_000)
    pl.DataFrame([row]).write_parquet(path)


@pytest.mark.parametrize("asset_type,sub,symbol", [
    ("stock", "kline_daily", "600000.SH"),
    ("etf", "kline_etf_daily", "510300.SH"),
])
def test_raw_bar_and_previous_close_tolerate_added_fields(tmp_path, asset_type, sub, symbol):
    write_part(tmp_path, sub, "2026-09-15", symbol, 10.0)
    write_part(tmp_path, sub, "2026-09-16", symbol, 10.2, extra=True)
    assert paper.read_daily_bar(tmp_path, symbol, asset_type, "2026-09-16") == {"open": 10.2, "close": 10.2}
    assert paper._prev_close(tmp_path, symbol, asset_type, "2026-09-16") == 10.0
    assert paper._prev_close(tmp_path, symbol, asset_type, "2026-09-17") == 10.2


def test_index_benchmark_tolerates_added_quote_timestamp(tmp_path):
    write_part(tmp_path, "kline_index_daily", "2026-09-15", "000300.SH", 4000.0)
    write_part(tmp_path, "kline_index_daily", "2026-09-16", "000300.SH", 4020.0, extra=True)
    assert paper._index_close(tmp_path, "2026-09-16") == 4020.0


def test_settlement_keeps_fill_and_nav_when_history_schema_evolves(tmp_path, monkeypatch):
    write_part(tmp_path, "kline_daily", "2026-09-15", "600000.SH", 10.0)
    write_part(tmp_path, "kline_daily", "2026-09-16", "600000.SH", 10.2, extra=True)
    monkeypatch.setattr(paper, "cn_today", lambda: date(2026, 9, 16))
    monkeypatch.setattr(paper, "cn_now", lambda: datetime(2026, 9, 16, 14, 59, tzinfo=CN_TZ))
    paper.create_account(tmp_path, 10000, slippage_bps=0)
    order, error = paper.create_order(tmp_path, "600000.SH", "buy", qty=100, order_type="close", ref_price=10.2)
    assert error is None and order
    result = paper.settle_day(tmp_path, "2026-09-16")
    assert result["filled"] == 1
    assert paper.load_fills(tmp_path)[0]["fee"] == 5.0
    assert paper.get_account(tmp_path)["cash"] == 8975.0
    assert result["nav"]["nav"] == 9995.0
