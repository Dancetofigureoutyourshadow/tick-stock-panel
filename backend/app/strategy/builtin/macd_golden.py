"""MACD金叉放量 — MACD金叉当日 + 量能放大"""
import polars as pl

META = {
    "id": "macd_golden",
    "name": "MACD 金叉放量",
    "description": "MACD金叉当日 + 量能放大",
    "tags": ["MACD", "金叉", "放量"],
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
    ],
    "scoring": {"momentum_60d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_macd_golden"]
EXIT_SIGNALS = ["signal_macd_dead"]
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 20
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    vol_min = params.get("vol_ratio_min", 1.5)
    return (
        pl.col("signal_macd_golden").fill_null(False)
        & (pl.col("vol_ratio_5d") >= vol_min)
    )
