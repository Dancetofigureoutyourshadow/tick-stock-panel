"""X RPS 突破 v2 —— 保留 RPS 相对强度筛选, 把"突破 60 日高点"换成"强势股回调日"。

基线 custom_sequoia_rps_breakout 在 2016-2026 全区间年化 -0.3546、3270 笔交易: 样本
足够, 亏损来自入场腿。原策略要求"RPS >= 85 且收盘突破前 60 日高点", 等于在 120 日
相对强度最高的一批里再挑突破当天买 —— 上一轮实测这正是负 alpha 最集中的位置。原策略
文件一字节不动, 便于同区间同参数对打。

四处改动与依据 (依据来自 custom_sequoia_ma_volume_v2 阶段 1-19 的实测, 存档见
.tmp-strategy-opt-v2/):

1. 保留策略身份: RPS 仍然是 rps_window 日涨幅的横截面 rank(pct=True) 百分位
   (_rank_pct 与原文件同一口径, 含并列平均排名), 入场仍然要求 rps >= rps_min, 退出
   仍然认 rps <= exit_rps。也就是"只在长期相对强度靠前的股票池里做"这一条不动。

2. 入场时点: 「突破前 60 日高点当天」-> 「同一批强势股的回调日」。
   pullback_window 日动量落在 [pullback_min, pullback_max] (默认 -0.25 ~ 0)、仍站在
   趋势线上方、乖离 <= ext_max、换手 <= turnover_max。breakout_window /
   breakout_buffer 两个参数连同那一腿整个删掉: 上一轮实测 20 日动量 >+20% 的横截面
   超额 -0.94%, 5 日 >+10% 是 -0.99%, 唯一为正的形态是「动量 <= 0 且站上趋势线」
   +0.026%。长期强势(RPS) + 短期回调, 才是这条线能站住的组合。

3. 打分: 原来直接把 RPS 当 score (等于按短中期动量排序) -> 低成交额 / 低换手 /
   低波动 / 低动量四因子横截面分位, 全部"越低越好", 另留一个默认权重 0 的 w_rps
   (高 RPS 更好, 唯一不取反的因子) 供网格决定要不要把身份因子加回打分。60 日前瞻
   十分组里 turnover20 首末价差 -5.26pp、amount20 -4.26pp、vol20 -3.47pp、r20 -2.23pp。

4. basic_filter 与风控按 v2 配方重写: 去掉 price_min / price_max / market_cap_min /
   turnover_min / turnover_max, 流动性只留 amount_min; 拆掉 -9% 止损、-10% 移动止损与
   0.20/0.08 移动止盈, 持有期 120 -> 40 天, 改 take_profit 0.20。

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
    valid_rolling_mean,
    valid_rolling_std,
    valid_shift,
)


META = {
    "id": "custom_sequoia_rps_breakout_v2",
    "name": "X RPS 突破 v2 (强势回调)",
    "description": "只在 RPS 相对强度靠前的股票池里做, 入场改到回调日(动量转负但仍站趋势线), 按低成交额/低换手/低波动/低动量打分, RPS 走弱或乖离拉开后退出",
    "tags": ["Sequoia-X", "RPS", "相对强度", "反转", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "rps_window", "label": "RPS回看周期", "type": "int", "default": 120, "min": 60, "max": 240, "step": 20},
        {"id": "rps_min", "label": "最低RPS", "type": "float", "default": 85.0, "min": 50.0, "max": 98.0, "step": 1.0},
        {"id": "exit_rps", "label": "退出RPS", "type": "float", "default": 65.0, "min": 20.0, "max": 80.0, "step": 5.0},
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 60, "min": 20, "max": 120, "step": 5},
        {"id": "pullback_window", "label": "回调回看周期", "type": "int", "default": 20, "min": 5, "max": 60, "step": 5},
        {"id": "pullback_max", "label": "入场最大区间涨幅", "type": "float", "default": 0.0, "min": -0.3, "max": 0.3, "step": 0.02},
        {"id": "pullback_min", "label": "入场最小区间涨幅", "type": "float", "default": -0.25, "min": -0.9, "max": 0.0, "step": 0.05},
        {"id": "mom_exit", "label": "退出区间涨幅", "type": "float", "default": 0.5, "min": 0.1, "max": 3.0, "step": 0.05},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.10, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.30, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 8.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "exit_on_trend_break", "label": "跌破趋势线退出", "type": "bool", "default": False},
        {"id": "trend_break_buffer", "label": "跌破趋势线缓冲", "type": "float", "default": 0.03, "min": 0.0, "max": 0.15, "step": 0.01},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.60, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.20, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_reversal", "label": "打分权重-低动量", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_rps", "label": "打分权重-高RPS", "type": "float", "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05},
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
ENTRY_SIGNALS = ["signal_sequoia_rps_breakout_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_rps_breakout_v2_exit"]
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


def _rank_pct(values: np.ndarray) -> np.ndarray:
    """实现 pandas rank(pct=True) 的平均排名口径 (与原策略文件逐行等价)。"""
    ranked = np.full(values.shape, np.nan, dtype=np.float32)
    for time_id in range(values.shape[0]):
        row = values[time_id]
        valid_assets = np.flatnonzero(np.isfinite(row))
        if valid_assets.size == 0:
            continue

        order = np.argsort(row[valid_assets], kind="stable")
        sorted_values = row[valid_assets[order]]
        ranks = np.empty(sorted_values.size, dtype=np.float32)

        start = 0
        while start < sorted_values.size:
            end = start + 1
            while end < sorted_values.size and sorted_values[end] == sorted_values[start]:
                end += 1
            # 位置是 1-based；并列值使用平均排名。
            ranks[start:end] = np.float32(((start + 1) + end) / 2.0)
            start = end

        ranked[time_id, valid_assets[order]] = (
            ranks / np.float32(valid_assets.size) * np.float32(100.0)
        )
    return ranked


class SequoiaRpsPullbackMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("rps_window", 120)),
            int(params.get("trend_window", 60)),
            int(params.get("pullback_window", 20)),
            20,
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        rps_window = int(params.get("rps_window", 120))
        rps_min = float(params.get("rps_min", 85.0))
        exit_rps = float(params.get("exit_rps", 65.0))
        trend_window = int(params.get("trend_window", 60))
        pullback_window = int(params.get("pullback_window", 20))
        pullback_max = float(params.get("pullback_max", 0.0))
        pullback_min = float(params.get("pullback_min", -0.25))
        mom_exit = float(params.get("mom_exit", 0.5))
        ext_max = float(params.get("ext_max", 1.10))
        ext_exit = float(params.get("ext_exit", 1.30))
        turnover_max = float(params.get("turnover_max", 8.0))
        trend_break_buffer = float(params.get("trend_break_buffer", 0.03))
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if exit_rps >= rps_min:
            raise ValueError("退出RPS必须小于最低RPS")
        if pullback_min >= pullback_max:
            raise ValueError("入场最小区间涨幅必须小于最大区间涨幅")
        if mom_exit <= pullback_max:
            raise ValueError("退出区间涨幅必须大于入场最大区间涨幅")
        if ext_exit <= ext_max:
            raise ValueError("退出乖离阈值必须大于入场最大乖离")
        if not 0.0 <= trend_break_buffer < 1.0:
            raise ValueError("跌破趋势线缓冲必须在 0~1 之间")

        close = market.close
        close_valid = np.isfinite(close)

        rps_base = valid_shift(close, rps_window, close_valid)
        rps_change = np.full(market.shape, np.nan, dtype=np.float32)
        np.divide(
            close - rps_base,
            rps_base,
            out=rps_change,
            where=np.isfinite(rps_base) & (rps_base != 0),
        )
        rps = _rank_pct(rps_change)

        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        past_close = valid_shift(close, pullback_window, close_valid)
        prev_close = valid_shift(close, 1, close_valid)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        with np.errstate(invalid="ignore", divide="ignore"):
            ext = close / trend_ma
            mom = close / past_close - np.float32(1.0)
            ret1 = close / prev_close - np.float32(1.0)

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, np.isfinite(ret1), 20)

        base_valid = (
            close_valid
            & np.isfinite(trend_ma)
            & np.isfinite(past_close)
            & np.isfinite(rps)
        )

        # 入场: 长期相对强度靠前(身份) + 短期回调但没崩 + 仍站趋势线 + 乖离/换手不过高。
        entry = (
            base_valid
            & (rps >= np.float32(rps_min))
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & (mom <= np.float32(pullback_max))
            & (mom >= np.float32(pullback_min))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )

        # 退出: 相对强度掉出池子(原策略同一条腿) 或 乖离拉开 / 动量转强。
        exit_ = base_valid & (
            (rps <= np.float32(exit_rps))
            | (ext >= np.float32(ext_exit))
            | (mom >= np.float32(mom_exit))
        )
        if params.get("exit_on_trend_break", False):
            exit_ |= base_valid & (
                close < trend_ma * np.float32(1.0 - trend_break_buffer)
            )

        weights = {
            "amount": max(float(params.get("w_amount", 0.10)), 0.0),
            "turnover": max(float(params.get("w_turnover", 0.60)), 0.0),
            "vol": max(float(params.get("w_vol", 0.20)), 0.0),
            "reversal": max(float(params.get("w_reversal", 0.10)), 0.0),
            "rps": max(float(params.get("w_rps", 0.0)), 0.0),
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("五个打分权重不能同时为 0")
        parts = {
            "amount": (amount_ma, np.isfinite(amount_ma), True),
            "turnover": (turnover_ma, np.isfinite(turnover_ma), True),
            "vol": (vol20, np.isfinite(vol20), True),
            "reversal": (mom, np.isfinite(mom), True),
            "rps": (rps, np.isfinite(rps), False),
        }
        score = np.zeros(market.shape, dtype=np.float32)
        for name, weight in weights.items():
            if weight <= 0:
                continue
            values, valid, invert = parts[name]
            score += _row_rank01(values, valid & entry, invert=invert) * np.float32(
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
            entry_signal_ids=("signal_sequoia_rps_breakout_v2_entry",),
            exit_signal_ids=("signal_sequoia_rps_breakout_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaRpsPullbackMatrixStrategy()
