"""低波动龙头 — 正动量 + 低波动 + MA20上方"""
import polars as pl

META = {
    "id": "low_volatility_leader",
    "name": "低波动龙头",
    "description": "20日动量为正 + 年化波动 < 30% + MA20上方",
    "tags": ["低波动", "龙头"],
    "params": [
        {"id": "vol_max", "label": "最大年化波动", "type": "float",
         "default": 0.30, "min": 0.05, "max": 1.0, "step": 0.01},
    ],
    "scoring": {"momentum_60d": 0.4, "momentum_20d": 0.3, "turnover_rate": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

ENTRY_SIGNALS = ["signal_ma20_breakout"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 30
ALERTS = []


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    vol_max = params.get("vol_max", 0.30)
    return (
        (pl.col("momentum_20d") > 0)
        & (pl.col("annual_vol_20d") < vol_max)
        & (pl.col("close") > pl.col("ma20"))
    )
