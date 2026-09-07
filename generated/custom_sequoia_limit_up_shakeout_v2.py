"""X 涨停洗盘 v2 —— 把"洗盘当天追进"换成"洗盘结束后再买", 打分转向低位低量。

基线 custom_sequoia_limit_up_shakeout 在 2016-2026 全区间年化 -0.1164、4831 笔交易:
交易量足够大, 亏损不是样本不足, 是入场腿本身负 alpha。原策略在"昨日大涨 >= 9.5% +
今日放量收阴 + 支撑未破"当天就买, 等于在最拥挤、最高换手的那一天接货。原策略文件
一字节不动, 便于同区间同参数对打。

四处改动与依据 (依据来自 custom_sequoia_ma_volume_v2 阶段 1-19 的实测, 存档见
.tmp-strategy-opt-v2/):

1. 入场时点: 「大涨次日放量收阴当天」-> 「大涨发生在 wait_min ~ wait_max 个交易日
   之前, 且这段时间里没有新的大涨」。保留的策略身份就是这个"大涨/涨停作为关注度
   催化"的事件腿 (spike_gain_min 默认仍是 9.5%); 改掉的只是成交时点 —— 上一轮实测
   5 日动量 >+10% 的横截面超额 -0.99%、20 日 >+20% 是 -0.94%, 事件当天入场必然吃到
   这一段负 edge。

2. 入场状态: 追加"洗盘真的结束了"的三条硬条件 —— 短期动量已经转负
   (mom_window 日涨幅 <= post_mom_max)、量能干涸 (volume <= 20 日均量 * vol_dry_ratio)、
   仍站在趋势线上方且乖离不过大。原策略的"放量收阴"要求恰好相反 (volume >= 前一日
   * 1.5), 这一条整个反转过来。

3. 打分: momentum_5d .40 / close_position .35 / vol_ratio_5d .25 -> 低成交额 / 低换手 /
   低波动 / 低动量四因子横截面分位, 全部"越低越好"。60 日前瞻十分组里 turnover20
   首末价差 -5.26pp、amount20 -4.26pp、vol20 -3.47pp、r20 -2.23pp, 原三因子全在反向。

4. basic_filter 与风控按 v2 配方重写: 去掉 price_min / price_max / market_cap_min /
   turnover_min / turnover_max, 流动性只留 amount_min; 拆掉 -6% 止损、-5% 移动止损,
   持有期 12 -> 40 天, 止盈 0.12 -> 0.20。反转形态要等均值回复, 需要月度级别的持有期,
   而 -5% 的移动止损在噪声里必然先割掉自己 (上一轮占 51.6% 的退出、均亏 -2.49%)。

**必须显式写 None**: 加载器是 bf = {**DEFAULT_BASIC_FILTER}; bf.update(BASIC_FILTER)
(app/strategy/engine.py:471-474), 省略键 = 继承引擎默认值; 下游全是 `is not None`
判断, 所以 None 才是"关掉"。跑回测时 overrides 里同样要把风控块写全 —— 回测只读请求体
里的 overrides (app/backtest/strategy.py:1021)。
"""

from __future__ import annotations

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    valid_rolling_max,
    valid_rolling_mean,
    valid_rolling_std,
    valid_shift,
)


META = {
    "id": "custom_sequoia_limit_up_shakeout_v2",
    "name": "X 涨停洗盘 v2 (洗盘结束后)",
    "description": "近期出现过涨停级大涨作为催化, 等洗盘结束(动量转负+量能干涸+仍站趋势线)后才入场, 按低成交额/低换手/低波动/低动量打分",
    "tags": ["Sequoia-X", "涨停", "洗盘", "反转", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "spike_gain_min", "label": "催化日最低涨幅", "type": "float", "default": 0.095, "min": 0.04, "max": 0.15, "step": 0.005},
        {"id": "wait_min", "label": "催化后最少等待(交易日)", "type": "int", "default": 5, "min": 1, "max": 20, "step": 1},
        {"id": "wait_max", "label": "催化后最多等待(交易日)", "type": "int", "default": 15, "min": 2, "max": 60, "step": 1},
        {"id": "mom_window", "label": "动量回看周期", "type": "int", "default": 5, "min": 2, "max": 30, "step": 1},
        {"id": "post_mom_max", "label": "入场最大区间涨幅", "type": "float", "default": 0.0, "min": -0.3, "max": 0.3, "step": 0.02},
        {"id": "post_mom_min", "label": "入场最小区间涨幅", "type": "float", "default": -0.4, "min": -0.9, "max": 0.0, "step": 0.05},
        {"id": "mom_exit", "label": "退出区间涨幅", "type": "float", "default": 0.2, "min": 0.1, "max": 3.0, "step": 0.05},
        {"id": "vol_dry_ratio", "label": "量能干涸倍数", "type": "float", "default": 1.15, "min": 0.20, "max": 2.00, "step": 0.05},
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 20, "min": 10, "max": 120, "step": 5},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.12, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.35, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 15.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "exit_on_trend_break", "label": "跌破趋势线退出", "type": "bool", "default": False},
        {"id": "trend_break_buffer", "label": "跌破趋势线缓冲", "type": "float", "default": 0.03, "min": 0.0, "max": 0.15, "step": 0.01},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.30, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.50, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_reversal", "label": "打分权重-低动量", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
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
    "amount_min": 0.2e8,
    "exclude_st": True,
    "exclude_new_days": 120,
    "boards": ["沪主板", "深主板"],
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_sequoia_limit_up_shakeout_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_limit_up_shakeout_v2_exit"]
STOP_LOSS = None
TAKE_PROFIT = 0.15
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


def _event_in_window(
    event: np.ndarray,
    event_valid: np.ndarray,
    wait_min: int,
    wait_max: int,
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (事件发生在 [wait_min, wait_max] 个交易日前, 最近 wait_min-1 日内无新事件)。

    事件矩阵先转成 0/1 float 再走 valid_rolling_max: 该窗口内出现过一次事件, 滚动最大
    值就是 1。无效 bar 用 NaN 排除, 由 valid_* 家族统一处理停牌与预热缺口。
    """
    flag = np.where(event_valid, event.astype(np.float32), np.float32(np.nan))
    flag_valid = np.isfinite(flag)
    shifted = valid_shift(flag, wait_min, flag_valid)
    span = valid_rolling_max(shifted, np.isfinite(shifted), wait_max - wait_min + 1)
    had_event = np.isfinite(span) & (span > np.float32(0.5))
    if wait_min >= 2:
        shifted_one = valid_shift(flag, 1, flag_valid)
        fresh = valid_rolling_max(shifted_one, np.isfinite(shifted_one), wait_min - 1)
        no_new_event = ~(np.isfinite(fresh) & (fresh > np.float32(0.5)))
    else:
        no_new_event = np.ones(event.shape, dtype=bool)
    return had_event, no_new_event


class SequoiaLimitUpShakeoutV2MatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("wait_max", 15)) + 2,
            int(params.get("trend_window", 20)),
            int(params.get("mom_window", 5)),
            20,
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        spike_gain_min = float(params.get("spike_gain_min", 0.095))
        wait_min = int(params.get("wait_min", 5))
        wait_max = int(params.get("wait_max", 15))
        mom_window = int(params.get("mom_window", 5))
        post_mom_max = float(params.get("post_mom_max", 0.0))
        post_mom_min = float(params.get("post_mom_min", -0.4))
        mom_exit = float(params.get("mom_exit", 0.2))
        vol_dry_ratio = float(params.get("vol_dry_ratio", 1.15))
        trend_window = int(params.get("trend_window", 20))
        ext_max = float(params.get("ext_max", 1.12))
        ext_exit = float(params.get("ext_exit", 1.35))
        turnover_max = float(params.get("turnover_max", 15.0))
        trend_break_buffer = float(params.get("trend_break_buffer", 0.03))
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if wait_min < 1:
            raise ValueError("催化后最少等待必须 >= 1 个交易日")
        if wait_max < wait_min:
            raise ValueError("催化后最多等待必须不小于最少等待")
        if post_mom_min >= post_mom_max:
            raise ValueError("入场最小区间涨幅必须小于最大区间涨幅")
        if mom_exit <= post_mom_max:
            raise ValueError("退出区间涨幅必须大于入场最大区间涨幅")
        if ext_exit <= ext_max:
            raise ValueError("退出乖离阈值必须大于入场最大乖离")
        if not 0.0 <= trend_break_buffer < 1.0:
            raise ValueError("跌破趋势线缓冲必须在 0~1 之间")

        close = market.close
        close_valid = np.isfinite(close)
        volume_valid = close_valid & np.isfinite(market.volume)

        prev_close = valid_shift(close, 1, close_valid)
        past_close = valid_shift(close, mom_window, close_valid)
        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        vol_ma20 = valid_rolling_mean(market.volume, volume_valid, 20)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        with np.errstate(invalid="ignore", divide="ignore"):
            ret1 = close / prev_close - np.float32(1.0)
            mom = close / past_close - np.float32(1.0)
            ext = close / trend_ma

        ret1_valid = np.isfinite(ret1)
        spike = ret1 >= np.float32(spike_gain_min)
        had_spike, no_new_spike = _event_in_window(spike, ret1_valid, wait_min, wait_max)

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, ret1_valid, 20)

        base_valid = close_valid & np.isfinite(trend_ma) & np.isfinite(past_close)

        # 入场: 催化已经发生但已过去若干日, 洗盘期间没有新的大涨, 短期动量转负、
        # 量能干涸, 仍站在趋势线上方且乖离不过大, 换手不狂热。
        entry = (
            base_valid
            & had_spike
            & no_new_spike
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & (mom <= np.float32(post_mom_max))
            & (mom >= np.float32(post_mom_min))
            & np.isfinite(vol_ma20)
            & (market.volume <= vol_ma20 * np.float32(vol_dry_ratio))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )

        # 退出走信号腿而不是移动止损: 乖离拉开或动量转强 = 均值回复已经完成。
        exit_ = base_valid & (
            (ext >= np.float32(ext_exit)) | (mom >= np.float32(mom_exit))
        )
        if params.get("exit_on_trend_break", False):
            exit_ |= base_valid & (
                close < trend_ma * np.float32(1.0 - trend_break_buffer)
            )

        weights = {
            "amount": max(float(params.get("w_amount", 0.10)), 0.0),
            "turnover": max(float(params.get("w_turnover", 0.30)), 0.0),
            "vol": max(float(params.get("w_vol", 0.50)), 0.0),
            "reversal": max(float(params.get("w_reversal", 0.10)), 0.0),
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("四个打分权重不能同时为 0")
        parts = {
            "amount": (amount_ma, np.isfinite(amount_ma)),
            "turnover": (turnover_ma, np.isfinite(turnover_ma)),
            "vol": (vol20, np.isfinite(vol20)),
            "reversal": (mom, np.isfinite(mom)),
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
            entry_signal_ids=("signal_sequoia_limit_up_shakeout_v2_entry",),
            exit_signal_ids=("signal_sequoia_limit_up_shakeout_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaLimitUpShakeoutV2MatrixStrategy()
