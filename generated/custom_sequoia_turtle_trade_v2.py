"""X 海龟突破 v2 —— 保留唐奇安通道结构, 把"突破上沿"换成"回踩下半区"。

基线 custom_sequoia_turtle_trade 在 2016-2026 全区间年化 -0.3879、3262 笔交易: 样本
足够, 亏损来自入场腿。原策略要求"收盘 > 前 20 日最高价 * 1.005 且当日阳线且创新高",
是标准的追突破 —— 上一轮实测这正是负 alpha 最集中的位置。原策略文件一字节不动, 便于
同区间同参数对打。

四处改动与依据 (依据来自 custom_sequoia_ma_volume_v2 阶段 1-19 的实测, 存档见
.tmp-strategy-opt-v2/):

1. 保留策略身份: 仍然以唐奇安通道为核心结构 —— entry_window 日的通道上下沿都用
   **前一日可见**的 high/low 滚动极值 (valid_shift 1 再 rolling, 与原文件同口径, 不
   偷看当日), 趋势腿仍是 close > trend_window 日均线, 通道是否在上移仍可开关
   (channel_rising)。

2. 入场位置: 「收盘突破通道上沿」-> 「收盘落在通道下半区」。
   channel_position = (close - chan_low) / (chan_high - chan_low) <= pos_max
   (默认 0.35)。breakout_buffer 与"阳线 + 高于昨收"两条追涨确认整个删掉: 上一轮实测
   20 日动量 >+20% 的横截面超额 -0.94%, 5 日 >+10% 是 -0.99%, 唯一为正的形态是
   「动量 <= 0 且站上趋势线」+0.026%。通道下半区 + 趋势仍向上 = 同一个通道结构里
   风险收益比相反的那一侧。

3. 退出改走通道上沿: channel_position >= pos_exit (默认 0.90) 或乖离拉开
   (ext >= ext_exit); 原策略的"跌破 exit_window 日通道下沿"退出改为可选开关
   (exit_on_channel_break, 默认关) —— 入场本来就在下沿附近, 那一腿会立刻把自己割掉。

4. 打分: momentum_60d .55 / amount_ratio_5d .25 / close_position .20 -> 低成交额 /
   低换手 / 低波动 / 低通道位置四因子横截面分位, 全部"越低越好"。60 日前瞻十分组里
   turnover20 首末价差 -5.26pp、amount20 -4.26pp、vol20 -3.47pp、close/ma60 -2.27pp;
   第四个因子用通道位置, 保留策略身份。

5. basic_filter 与风控按 v2 配方重写: 去掉 price_min / price_max / market_cap_min /
   turnover_min / turnover_max, 流动性只留 amount_min (策略内部的 amount_min_yi 也
   保留下来当日频流动性闸门); 拆掉 -8% 止损、-10% 移动止损与 0.20/0.08 移动止盈,
   持有期 80 -> 40 天, 改 take_profit 0.20。

**必须显式写 None**: 加载器是 bf = {**DEFAULT_BASIC_FILTER}; bf.update(BASIC_FILTER)
(app/strategy/engine.py:471-474), 省略键 = 继承引擎默认值; 下游全是 `is not None`
判断。跑回测时 overrides 里同样要把风控块写全 (app/backtest/strategy.py:1021)。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_min,
    valid_rolling_std,
    valid_shift,
)


META = {
    "id": "custom_sequoia_turtle_trade_v2",
    "name": "X 海龟突破 v2 (通道下沿)",
    "description": "唐奇安通道仍在上移且趋势向上时, 在收盘回落到通道下半区时入场(不追突破), 按低成交额/低换手/低波动/低通道位置打分, 触及通道上沿或乖离拉开后退出",
    "tags": ["Sequoia-X", "海龟", "通道", "反转", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "entry_window", "label": "通道周期", "type": "int", "default": 20, "min": 10, "max": 120, "step": 5},
        {"id": "exit_window", "label": "退出通道周期", "type": "int", "default": 10, "min": 5, "max": 60, "step": 5},
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 120, "min": 20, "max": 120, "step": 5},
        {"id": "pos_max", "label": "入场最高通道位置", "type": "float", "default": 0.25, "min": 0.0, "max": 0.9, "step": 0.05},
        {"id": "pos_exit", "label": "退出通道位置", "type": "float", "default": 0.90, "min": 0.3, "max": 1.5, "step": 0.05},
        {"id": "channel_rising", "label": "要求通道上移", "type": "bool", "default": True},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.12, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.45, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 12.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "amount_min_yi", "label": "最低成交额(亿元)", "type": "float", "default": 0.5, "min": 0.1, "max": 5.0, "step": 0.1},
        {"id": "exit_on_channel_break", "label": "跌破退出通道下沿退出", "type": "bool", "default": False},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.60, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.20, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_chanpos", "label": "打分权重-低通道位置", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": None,
}

BASIC_FILTER = {
    "price_min": None,
    "price_max": None,
    "market_cap_min": None,
    "turnover_min": None,
    "turnover_max": None,
    "amount_min": 0.5e8,
    "exclude_st": True,
    "exclude_new_days": 250,
    "boards": ["沪主板", "深主板"],
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_sequoia_turtle_trade_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_turtle_trade_v2_exit"]
STOP_LOSS = None
TAKE_PROFIT = 0.20
TRAILING_STOP = None
TRAILING_TAKE_PROFIT_ACTIVATE = None
TRAILING_TAKE_PROFIT_DRAWDOWN = None
MAX_HOLD_DAYS = 40

_EPS = np.float32(1e-12)


def _row_rank01(values: np.ndarray, valid: np.ndarray, invert: bool) -> np.ndarray:
    """把每一行(交易日)的因子值线性映射到 [0,1]; invert=True 表示越小越好。

    与 build_matrix_score / custom_sequoia_ma_volume_v2 的横截面 min-max 口径保持
    一致: 无效值给 0, 当日全部相等时给 0.5。
    """
    work = np.where(valid, values, np.nan).astype(np.float32, copy=False)
    with np.errstate(invalid="ignore"):
        row_min = np.nanmin(np.where(np.isfinite(work), work, np.nan), axis=1, keepdims=True)
        row_max = np.nanmax(np.where(np.isfinite(work), work, np.nan), axis=1, keepdims=True)
    span = row_max - row_min
    out = np.zeros(values.shape, dtype=np.float32)
    varying = np.isfinite(span) & (span > _EPS)
    np.divide(work - row_min, span, out=out, where=valid & varying)
    out[valid & ~varying] = np.float32(0.5)
    if invert:
        out[valid] = np.float32(1.0) - out[valid]
    out[~valid] = 0.0
    return out


class SequoiaTurtleChannelPullbackMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("entry_window", 20)) + 1,
            int(params.get("exit_window", 10)) + 1,
            int(params.get("trend_window", 120)),
            20,
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        entry_window = int(params.get("entry_window", 20))
        exit_window = int(params.get("exit_window", 10))
        trend_window = int(params.get("trend_window", 120))
        pos_max = float(params.get("pos_max", 0.25))
        pos_exit = float(params.get("pos_exit", 0.90))
        ext_max = float(params.get("ext_max", 1.12))
        ext_exit = float(params.get("ext_exit", 1.45))
        turnover_max = float(params.get("turnover_max", 12.0))
        amount_min = float(params.get("amount_min_yi", 0.5)) * 100_000_000
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if pos_exit <= pos_max:
            raise ValueError("退出通道位置必须大于入场最高通道位置")
        if ext_exit <= ext_max:
            raise ValueError("退出乖离阈值必须大于入场最大乖离")

        close = market.close
        close_valid = np.isfinite(close)
        high_valid = close_valid & np.isfinite(market.high)
        low_valid = close_valid & np.isfinite(market.low)

        previous_high = valid_shift(market.high, 1, high_valid)
        previous_low = valid_shift(market.low, 1, low_valid)
        chan_high = valid_rolling_max(previous_high, np.isfinite(previous_high), entry_window)
        chan_low = valid_rolling_min(previous_low, np.isfinite(previous_low), entry_window)
        exit_low = valid_rolling_min(previous_low, np.isfinite(previous_low), exit_window)
        past_chan_high = valid_shift(chan_high, entry_window, np.isfinite(chan_high))
        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        prev_close = valid_shift(close, 1, close_valid)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        span = chan_high - chan_low
        chan_pos = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(
            close - chan_low,
            span,
            out=chan_pos,
            where=np.isfinite(span) & (span > _EPS) & close_valid,
        )

        with np.errstate(invalid="ignore", divide="ignore"):
            ext = close / trend_ma
            ret1 = close / prev_close - np.float32(1.0)

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, np.isfinite(ret1), 20)

        base_valid = (
            close_valid
            & np.isfinite(chan_high)
            & np.isfinite(chan_low)
            & np.isfinite(chan_pos)
            & np.isfinite(trend_ma)
        )

        # 入场: 通道下半区 + 趋势仍向上 + 乖离/换手不过高 + 日频流动性达标。
        entry = (
            base_valid
            & (chan_pos <= np.float32(pos_max))
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & amount_valid
            & (amount >= np.float32(amount_min))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )
        if params.get("channel_rising", True):
            # 通道整体上移: 当前通道上沿不低于一个通道周期前的上沿。
            entry &= np.isfinite(past_chan_high) & (chan_high >= past_chan_high)

        # 退出: 走到通道上沿 (原策略的"突破"就是这里, v2 用它兑现) 或乖离拉开。
        exit_ = base_valid & (
            (chan_pos >= np.float32(pos_exit)) | (ext >= np.float32(ext_exit))
        )
        if params.get("exit_on_channel_break", False):
            exit_ |= close_valid & np.isfinite(exit_low) & (close < exit_low)

        weights = {
            "amount": max(float(params.get("w_amount", 0.10)), 0.0),
            "turnover": max(float(params.get("w_turnover", 0.60)), 0.0),
            "vol": max(float(params.get("w_vol", 0.20)), 0.0),
            "chanpos": max(float(params.get("w_chanpos", 0.10)), 0.0),
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("四个打分权重不能同时为 0")
        parts = {
            "amount": (amount_ma, np.isfinite(amount_ma)),
            "turnover": (turnover_ma, np.isfinite(turnover_ma)),
            "vol": (vol20, np.isfinite(vol20)),
            "chanpos": (chan_pos, np.isfinite(chan_pos)),
        }
        score = np.zeros(market.shape, dtype=np.float32)
        for name, weight in weights.items():
            if weight <= 0:
                continue
            values, valid = parts[name]
            score += _row_rank01(values, valid & entry, invert=True) * np.float32(
                weight / total
            )
        score *= np.float32(100.0)
        score[~entry] = 0.0

        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            score=score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_sequoia_turtle_trade_v2_entry",),
            exit_signal_ids=("signal_sequoia_turtle_trade_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaTurtleChannelPullbackMatrixStrategy()
