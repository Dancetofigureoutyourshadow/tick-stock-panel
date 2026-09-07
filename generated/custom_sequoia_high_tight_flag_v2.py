"""X 高位紧旗 v2 —— 把"突破旗形高点"换成"旗内潜伏", 打分转向低位低量。

基线 custom_sequoia_high_tight_flag 在 2016-2026 全区间年化 0.0004、只有 36 笔交易:
既没有收益, 也没有统计意义。归因与 custom_sequoia_ma_volume_v2 一致 —— 入场那一腿
(突破确认) 本身是负 alpha, 而且"必须在突破当天成交"把候选压到了几乎为零。原策略文件
一字节不动, 便于同区间同参数对打。

四处改动与依据 (依据来自 custom_sequoia_ma_volume_v2 阶段 1-19 的实测, 存档见
.tmp-strategy-opt-v2/):

1. 入场: 「紧旗 + 缩量 + **收盘突破前一日可见旗形高点**」-> 「紧旗 + 缩量 + 仍在旗
   内」。突破确认整个删掉: 上一轮实测 20 日动量 >+20% 的横截面超额是 -0.94%,
   5 日 >+10% 是 -0.99%, 追突破就是往负 edge 上加杠杆。删掉之后候选从"突破当日"
   放宽到"整段整理期", 交易笔数量级上升, 资金才用得起来。
   保留的策略身份: momentum_window 区间涨幅 >= min_momentum (前期强势)、flag_window
   振幅 <= max_flag_range (紧旗)、low_flag >= high_momentum * flag_floor (旗不破位)、
   volume <= 前一日 20 日均量 * max_volume_ratio (缩量)。这四条就是"高位紧旗"。

2. min_momentum 默认 0.6 -> 0.30。0.6 在沪深主板叠加紧旗后一年只剩个位数候选,
   36 笔交易的直接原因。参数下界同时放宽到 0.05, 让阶段 5 的网格自己找位置; 上界
   仍留到 1.5, 想复现原口径把它调回 0.6 即可。

3. 打分: momentum_60d .50 / close_position .30 / vol_ratio_5d .20 -> 低成交额 /
   低换手 / 低波动 / 窄旗四因子横截面分位, 全部"越低越好"。60 日前瞻十分组里
   turnover20 首末价差 -5.26pp、amount20 -4.26pp、vol20 -3.47pp, 按高动量和
   close_position 打分等于反向。第四个因子用旗形振幅 (越窄越好), 保留策略身份。

4. basic_filter 与风控按 v2 配方重写: 去掉 price_min / price_max / market_cap_min /
   turnover_min / turnover_max (前五项作用在前复权 close 与规模上, 与未来涨幅反相关,
   2005-06-30 实测通过者末期涨幅中位 2.67x vs 被剔除者 6.46x), 流动性只留 amount_min;
   拆掉 -8% 止损与 -7% 移动止损 (噪声里必然先割自己), 改 take_profit 0.20 +
   max_hold_days 40。换手上限挪进策略参数 turnover_max。

**必须显式写 None**: 加载器是 bf = {**DEFAULT_BASIC_FILTER}; bf.update(BASIC_FILTER)
(app/strategy/engine.py:471-474), 省略键 = 继承引擎默认 price_min 3 / price_max 300 /
market_cap_min 10e8, 下游全是 `is not None` 判断, 所以 None 才是"关掉"。
跑回测时 overrides 里同样要把风控块写全 —— 回测只读请求体里的 overrides
(app/backtest/strategy.py:1021), 不覆盖等于本文件的常量没写。
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
    "id": "custom_sequoia_high_tight_flag_v2",
    "name": "X 高位紧旗 v2 (旗内潜伏)",
    "description": "前期强势后进入窄幅缩量整理, 在旗内仍安静时入场(不等突破), 按低成交额/低换手/低波动/窄旗打分, 乖离拉开或旗形破位后退出",
    "tags": ["Sequoia-X", "高位整理", "缩量", "潜伏", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "momentum_window", "label": "强动量周期", "type": "int", "default": 40, "min": 20, "max": 80, "step": 5},
        {"id": "min_momentum", "label": "最低区间涨幅", "type": "float", "default": 0.30, "min": 0.05, "max": 1.5, "step": 0.05},
        {"id": "flag_window", "label": "整理周期", "type": "int", "default": 10, "min": 5, "max": 30, "step": 1},
        {"id": "max_flag_range", "label": "最大整理振幅", "type": "float", "default": 0.15, "min": 0.04, "max": 0.40, "step": 0.01},
        {"id": "flag_floor", "label": "旗形下沿/区间高点", "type": "float", "default": 0.80, "min": 0.50, "max": 0.95, "step": 0.01},
        {"id": "max_volume_ratio", "label": "最大整理量比", "type": "float", "default": 0.80, "min": 0.30, "max": 1.50, "step": 0.05},
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 20, "min": 10, "max": 120, "step": 5},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.15, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.35, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 12.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "exit_on_flag_break", "label": "旗形破位退出", "type": "bool", "default": False},
        {"id": "flag_break_buffer", "label": "旗形破位缓冲", "type": "float", "default": 0.03, "min": 0.0, "max": 0.15, "step": 0.01},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.60, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.20, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_tightness", "label": "打分权重-窄旗", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
    ],
    # 四个因子都是「越低越好」, META scoring 表达不了方向 -> 打分在 compute_signals
    # 内部完成。留空即让引擎使用策略自带的 score。
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
ENTRY_SIGNALS = ["signal_sequoia_high_tight_flag_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_high_tight_flag_v2_exit"]
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


class SequoiaHighTightFlagLurkMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"high", "low", "close", "volume", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("momentum_window", 40)),
            int(params.get("flag_window", 10)) + 1,
            int(params.get("trend_window", 20)),
            20,
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        momentum_window = int(params.get("momentum_window", 40))
        min_momentum = float(params.get("min_momentum", 0.30))
        flag_window = int(params.get("flag_window", 10))
        max_flag_range = float(params.get("max_flag_range", 0.15))
        flag_floor = float(params.get("flag_floor", 0.80))
        max_volume_ratio = float(params.get("max_volume_ratio", 0.80))
        trend_window = int(params.get("trend_window", 20))
        ext_max = float(params.get("ext_max", 1.15))
        ext_exit = float(params.get("ext_exit", 1.35))
        turnover_max = float(params.get("turnover_max", 12.0))
        flag_break_buffer = float(params.get("flag_break_buffer", 0.03))
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if ext_exit <= ext_max:
            raise ValueError("退出乖离阈值必须大于入场最大乖离")
        if not 0.0 < flag_floor < 1.0:
            raise ValueError("旗形下沿比例必须在 0~1 之间")
        if not 0.0 <= flag_break_buffer < 1.0:
            raise ValueError("旗形破位缓冲必须在 0~1 之间")

        close = market.close
        close_valid = np.isfinite(close)
        price_valid = close_valid & np.isfinite(market.high) & np.isfinite(market.low)
        volume_valid = close_valid & np.isfinite(market.volume)

        high_momentum = valid_rolling_max(market.high, price_valid, momentum_window)
        low_momentum = valid_rolling_min(market.low, price_valid, momentum_window)
        previous_high = valid_shift(market.high, 1, price_valid)
        previous_low = valid_shift(market.low, 1, price_valid)
        prior_flag_high = valid_rolling_max(previous_high, np.isfinite(previous_high), flag_window)
        prior_flag_low = valid_rolling_min(previous_low, np.isfinite(previous_low), flag_window)
        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        prev_close = valid_shift(close, 1, close_valid)

        previous_volume = valid_shift(market.volume, 1, volume_valid)
        previous_vol_ma20 = valid_rolling_mean(previous_volume, np.isfinite(previous_volume), 20)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        with np.errstate(invalid="ignore", divide="ignore"):
            run_up = high_momentum / low_momentum - np.float32(1.0)
            flag_range = prior_flag_high / prior_flag_low - np.float32(1.0)
            ext = close / trend_ma
            ret1 = close / prev_close - np.float32(1.0)

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, np.isfinite(ret1), 20)

        base_valid = (
            price_valid
            & np.isfinite(high_momentum)
            & np.isfinite(low_momentum)
            & np.isfinite(prior_flag_high)
            & np.isfinite(prior_flag_low)
            & np.isfinite(trend_ma)
        )

        # 入场: 前期强势 + 窄旗 + 旗不破位 + 缩量 + 仍在旗内 (不等突破)。
        # 乖离与换手上限用来避免在已经拉开的位置潜伏。
        entry = (
            base_valid
            & np.isfinite(previous_vol_ma20)
            & (run_up >= np.float32(min_momentum))
            & (flag_range <= np.float32(max_flag_range))
            & (prior_flag_low >= high_momentum * np.float32(flag_floor))
            & (market.volume <= previous_vol_ma20 * np.float32(max_volume_ratio))
            & (close > prior_flag_low)
            & (close <= prior_flag_high)
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )

        # 退出走信号腿: 乖离拉开 = 旗形已经完成它该完成的那一段。
        exit_ = base_valid & (ext >= np.float32(ext_exit))
        if params.get("exit_on_flag_break", False):
            # 入场前提就是仍在旗内且未破下沿, 前提破坏即离场。
            exit_ |= base_valid & (
                close < prior_flag_low * np.float32(1.0 - flag_break_buffer)
            )

        weights = {
            "amount": max(float(params.get("w_amount", 0.10)), 0.0),
            "turnover": max(float(params.get("w_turnover", 0.60)), 0.0),
            "vol": max(float(params.get("w_vol", 0.20)), 0.0),
            "tightness": max(float(params.get("w_tightness", 0.10)), 0.0),
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("四个打分权重不能同时为 0")
        parts = {
            "amount": (amount_ma, np.isfinite(amount_ma)),
            "turnover": (turnover_ma, np.isfinite(turnover_ma)),
            "vol": (vol20, np.isfinite(vol20)),
            "tightness": (flag_range, np.isfinite(flag_range)),
        }
        score = np.zeros(market.shape, dtype=np.float32)
        for name, weight in weights.items():
            if weight <= 0:
                continue
            values, valid = parts[name]
            # 分位只在当日入场候选内做, 四个方向全部取「越低越好」。
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
            entry_signal_ids=("signal_sequoia_high_tight_flag_v2_entry",),
            exit_signal_ids=("signal_sequoia_high_tight_flag_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaHighTightFlagLurkMatrixStrategy()
