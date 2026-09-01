"""daily-batch 混合资产分组测试。"""
import datetime as _dt

import polars as pl
import pytest

from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def test_daily_batch_groups_index_symbols(repo, monkeypatch):
    from app.api import kline as kline_api

    calls = {"stock_batch": [], "index": []}

    def fake_stock_batch(symbols, start, end, columns=None):
        calls["stock_batch"].append(list(symbols))
        return pl.DataFrame()

    def fake_index_daily(symbol, start, end, columns=None):
        calls["index"].append(symbol)
        return pl.DataFrame({
            "symbol": [symbol], "date": [_dt.date(2026, 7, 24)],
            "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1],
        })

    monkeypatch.setattr(repo, "get_daily_batch", fake_stock_batch)
    monkeypatch.setattr(repo, "get_index_daily", fake_index_daily)
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: {"000001.SH"})
    monkeypatch.setattr(repo, "get_etf_symbol_set", lambda: set())

    state = type("S", (), {"repo": repo})()
    req = type("R", (), {"app": type("A", (), {"state": state})()})()

    out = kline_api.get_daily_batch(req, {"symbols": ["600000.SH", "000001.SH"], "days": 12})
    assert calls["stock_batch"] == [["600000.SH"]]
    assert calls["index"] == ["000001.SH"]
    assert "000001.SH" in out["data"]


def test_daily_batch_computes_limit_signals_from_raw_prices(repo, monkeypatch):
    date_prev = _dt.date(2026, 7, 23)
    date_current = _dt.date(2026, 7, 24)
    source = pl.DataFrame({
        "symbol": ["000001.SZ", "000001.SZ"],
        "date": [date_prev, date_current],
        "open": [10.0, 11.0],
        "high": [10.0, 11.0],
        "low": [10.0, 11.0],
        "close": [10.0, 11.0],
        "volume": [100, 100],
        "raw_close": [10.0, 11.0],
        "raw_high": [10.0, 11.0],
        "raw_low": [10.0, 11.0],
    })
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (pl.DataFrame(), None))
    monkeypatch.setattr(repo, "_scan_daily_batch", lambda *args, **kwargs: source)
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({
        "symbol": ["000001.SZ"], "name": ["平安银行"],
    }))

    out = repo.get_daily_batch(
        ["000001.SZ"], date_prev, date_current,
        columns=["symbol", "date", "close", "signal_limit_up", "signal_limit_down"],
    )

    assert out.select("signal_limit_up").to_series().to_list() == [None, True]
    assert out.select("signal_limit_down").to_series().to_list() == [None, False]


def test_daily_batch_overlays_live_cache_when_today_is_not_on_disk(repo, monkeypatch):
    previous = _dt.date(2026, 7, 23)
    current = _dt.date(2026, 7, 24)
    monkeypatch.setattr(repo, "get_enriched_latest", lambda: (
        pl.DataFrame({
            "symbol": ["000001.SZ"],
            "date": [current],
            "open": [11.0],
            "high": [11.5],
            "low": [10.8],
            "close": [11.2],
            "volume": [120.0],
        }),
        current,
    ))
    monkeypatch.setattr(repo, "_scan_daily_batch", lambda *args, **kwargs: pl.DataFrame({
        "symbol": ["000001.SZ"],
        "date": [previous],
        "open": [10.0],
        "high": [10.4],
        "low": [9.8],
        "close": [10.1],
        "volume": [100.0],
    }))

    out = repo.get_daily_batch(
        ["000001.SZ"], previous, current,
        columns=["symbol", "date", "open", "high", "low", "close", "volume"],
    )

    assert out.select("date").to_series().to_list() == [previous, current]
    assert out.filter(pl.col("date") == current).select("close").item() == 11.2


def test_daily_close_projection_reads_only_bounded_partitions(repo, monkeypatch):
    first = _dt.date(2026, 8, 10)
    second = _dt.date(2026, 8, 11)
    for trading_date, close in ((first, 10.1), (second, 10.3)):
        path = (
            repo.store.data_dir
            / "kline_daily_enriched"
            / f"date={trading_date.isoformat()}"
            / "part.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({
            "symbol": ["000001.SZ", "600000.SH"],
            "date": [trading_date, trading_date],
            "close": [close, 9.9],
        }).write_parquet(path)

    monkeypatch.setattr(
        repo,
        "get_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("narrow close lookup must not use generic daily query")
        ),
    )
    monkeypatch.setattr(
        repo,
        "get_enriched_latest",
        lambda: (_ for _ in ()).throw(
            AssertionError("narrow close lookup must not warm the enriched cache")
        ),
    )

    out = repo.get_daily_asset(
        "stock",
        "000001.SZ",
        first,
        second,
        columns=["date", "close"],
    )

    assert out.to_dicts() == [
        {"date": first, "close": 10.1},
        {"date": second, "close": 10.3},
    ]


def test_missing_daily_close_projection_does_not_fall_back_to_enriched(repo, monkeypatch):
    monkeypatch.setattr(
        repo,
        "get_daily",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("missing close partitions must not use generic daily query")
        ),
    )
    monkeypatch.setattr(
        repo,
        "get_enriched_latest",
        lambda: (_ for _ in ()).throw(
            AssertionError("missing close partitions must not warm the enriched cache")
        ),
    )

    out = repo.get_daily_asset(
        "stock",
        "000001.SZ",
        _dt.date(2026, 8, 10),
        _dt.date(2026, 8, 11),
        columns=["date", "close"],
    )

    assert out.is_empty()
    assert out.schema == {"date": pl.Date, "close": pl.Float64}
