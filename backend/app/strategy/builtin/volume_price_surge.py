"""量价齐升 — 突破MA20 + 放量 + 收阳"""
import polars as pl

META = {
    "id": "volume_price_surge",
    "name": "量价齐升",
    "description": "突破MA20 + 放量 + 收阳",
    "tags": ["量价", "突破"],
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 2.0, "min": 0.5, "max": 10.0, "step": 0.1},
    ],
    "scoring": {"vol_ratio_5d": 0.4, "change_pct": 0.3, "momentum_20d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    vol_min = params.get("vol_ratio_min", 2.0)
    return (
        pl.col("signal_ma20_breakout").fill_null(False)
        & (pl.col("vol_ratio_5d") >= vol_min)
        & (pl.col("close") > pl.col("open"))
    )
