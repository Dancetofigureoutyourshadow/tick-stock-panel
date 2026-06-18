"""新低反转 — 60日新低后收阳放量"""
import polars as pl

META = {
    "id": "n_day_low_reversal",
    "name": "新低反转",
    "description": "触及60日新低后当日收阳放量, 反转信号",
    "tags": ["反转", "新低"],
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_n_day_low"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    vol_min = params.get("vol_ratio_min", 1.5)
    return (
        pl.col("signal_n_day_low").fill_null(False)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("vol_ratio_5d") >= vol_min)
    )
