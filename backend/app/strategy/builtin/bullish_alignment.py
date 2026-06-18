"""均线多头 — MA5>MA10>MA20>MA60 + 短期动量为正"""
import polars as pl

META = {
    "id": "bullish_alignment",
    "name": "均线多头",
    "description": "MA5>MA10>MA20>MA60多头排列 + 短期动量为正",
    "tags": ["均线", "多头"],
    "params": [],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_ma_golden_5_20", "signal_ma_golden_20_60"]
EXIT_SIGNALS = ["signal_ma_dead_5_20", "signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    return (
        (pl.col("ma5") > pl.col("ma10"))
        & (pl.col("ma10") > pl.col("ma20"))
        & (pl.col("ma20") > pl.col("ma60"))
        & (pl.col("momentum_20d") > 0)
    )
