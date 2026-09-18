import polars as pl

from app.services.market_overview_builder import _merge_live_change_pct


class _QuoteService:
    def __init__(self, rows: list[dict]):
        self._rows = pl.DataFrame(rows)

    def get_quotes_compat(self) -> pl.DataFrame:
        return self._rows


def test_merge_live_change_pct_fills_missing_cold_start_rows_only():
    df = pl.DataFrame({
        "symbol": ["000001.SZ", "688801.SH"],
        "change_pct": [0.01, None],
    })
    quotes = _QuoteService([
        {"symbol": "000001.SZ", "change_pct": 0.02},
        {"symbol": "688801.SH", "change_pct": 1.79},
    ])

    out = _merge_live_change_pct(df, quotes, enabled=True)

    assert out.select("change_pct").to_series().to_list() == [0.01, 1.79]


def test_merge_live_change_pct_does_not_mix_live_data_into_history():
    df = pl.DataFrame({"symbol": ["688801.SH"], "change_pct": [None]})
    quotes = _QuoteService([{"symbol": "688801.SH", "change_pct": 1.79}])

    out = _merge_live_change_pct(df, quotes, enabled=False)

    assert out["change_pct"].to_list() == [None]
