"""断板反包 — 涨停 + 放量 + 涨幅 >3%"""
import polars as pl

META = {
    "id": "broken_board_recovery",
    "name": "断板反包",
    "description": "连板≥2后断板1-2天, 出现放量反包信号",
    "tags": ["涨停", "反包"],
    "params": [
        {"id": "vol_ratio_min", "label": "最低量比", "type": "float",
         "default": 1.5, "min": 0.5, "max": 5.0, "step": 0.1},
        {"id": "change_pct_min", "label": "最低涨幅", "type": "float",
         "default": 0.03, "min": 0.01, "max": 0.10, "step": 0.01},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_limit_up"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 10
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    vol_min = params.get("vol_ratio_min", 1.5)
    chg_min = params.get("change_pct_min", 0.03)
    return (
        pl.col("signal_limit_up").fill_null(False)
        & (pl.col("vol_ratio_5d") >= vol_min)
        & (pl.col("change_pct") > chg_min)
    )
