"""强势高开 — 高开 > 3% 且保持上涨, 集合竞价强势"""
import polars as pl

META = {
    "id": "strong_open",
    "name": "强势高开",
    "description": "高开 > 3% 且收盘高于开盘价, 集合竞价强势",
    "tags": ["高开", "强势"],
    "params": [
        {"id": "min_open_gap", "label": "最低高开%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
        {"id": "min_change", "label": "最低涨幅%", "type": "float",
         "default": 3.0, "min": 1.0, "max": 10.0, "step": 0.5},
    ],
    "scoring": {"change_pct": 0.4, "amplitude": 0.2, "amount": 0.4},
    "order_by": "score",
    "descending": True,
    "limit": 50,
}

ENTRY_SIGNALS = []
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    min_gap = params.get("min_open_gap", 3.0) / 100.0
    min_chg = params.get("min_change", 3.0) / 100.0
    return (
        (pl.col("open") > pl.col("prev_close") * (1 + min_gap))
        & (pl.col("close") > pl.col("open"))
        & (pl.col("change_pct") > min_chg)
    )
