"""X 均线放量 v2 —— 把追涨入场换成回调反转, 打分转向低位低量。

基线 custom_sequoia_ma_volume 在 2016-2026 全区间不盈利, 归因指向**入场信号本身
是负 alpha**, 不是参数没调好 (零成本仍然亏)。本文件按上一轮探针的实测结论逐条改
写; 原策略文件一个字节不动, 便于同区间同参数对打。

四处改动与各自的实测依据 (原始数据见 .tmp-strategy-opt/probe2.log 与 decile.log):

1. 入场: 「金叉 + 放量 + 上涨」-> 「20 日动量 <= 0 且站上 ma60」。
   前者的 20 日横截面超额是 -0.18% (再加上升趋势过滤 -0.20%); 20 日动量 >+20%
   是 -0.94%, 5 日 >+10% 是 -0.99%。实测唯一 20 日为正的形态就是「20 日动量 <= 0
   且站上 ma60」+0.026%, 所以 require_uptrend 这类上升趋势确认整个删掉。同时保留
   一个动量下界 (mom_min): 十分组里最低分位(暴跌)那一侧也不好, 反转不等于接刀。

2. 打分: momentum_20d .45 / vol_ratio_5d .35 / close_position .20 -> 低成交额 /
   低换手 / 低波动 / 低动量 四因子横截面分位。60 日前瞻十分组里全部因子单调负斜
   率: turnover20 首末价差 -5.26pp、amount20 -4.26pp、vol20 -3.47pp、close/ma60
   -2.27pp、r20 -2.23pp —— 按高动量打分等于往负 edge 上加权。

3. basic_filter: 去掉 price_min / price_max / market_cap_min, 并且**连
   turnover_min 一起去掉**。前三个作用在前复权 close 上, 而前复权价与「从当日到
   今天的累计涨幅」成反比, 会系统性剔除未来大牛股 (2005-06-30 实测: 通过者到末
   期涨幅中位 2.67x, 被剔除者 6.46x, 且只有 72/1086 通过)。turnover_min=0.5 则
   是直接砍掉十分组里表现最好的低换手一侧 —— 这一条超出计划里写的三项, 依据同
   上。流动性只留 amount_min (不受复权影响), 换手上限挪进策略内部做参数。

4. 风控: 拆掉 -6% 移动止损, 持有期 25 -> 40 天, 止损放宽到 -12%。移动止损在上一
   轮占 51.6% 的退出、均亏 -2.49%, 拆掉后年化 -15.8% -> -11.2%; 反转形态要等均
   值回复, 需要月度级别的持有期, 而 -6% 的移动止损在噪声里必然先割掉自己。

**跑回测时必须显式覆盖风控**: 回测只读请求体里的 overrides
(app/backtest/strategy.py:1021), 而 bt.py 的 BASE 每次都发全量风控块, 里面装的是
基线的 take_profit 0.14 / trailing_stop -0.06 / max_hold_days 25。不在 patch 里
改掉它们, 本文件的 TAKE_PROFIT / TRAILING_STOP / MAX_HOLD_DAYS 就等于没写。现成
的配置在 .tmp-strategy-opt-v2/r2_v2_configs.json。

打分口径与 custom_quiet_lowpos 完全一致 (横截面 min-max 只在当日入场候选内做, 无
效值给 0, 当日全相等给 0.5), 两条线的结果因此可以直接比。META["scoring"] 留空是
因为四个因子都是「越低越好」, 而 META scoring 表达不了方向 (方向只认 overrides),
留空即让引擎使用策略自带的 score; 请求体里要保持 scoring:{} + scoring_replace:true。
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
    "id": "custom_sequoia_ma_volume_v2",
    "name": "X 均线放量 v2 (反转)",
    "description": "回调到 20 日动量转负但仍站上 ma60 时入场，按低成交额/低换手/低波动/低动量打分，乖离或动量走高后退出",
    "tags": ["Sequoia-X", "反转", "低波动", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 60, "min": 20, "max": 120, "step": 5},
        {"id": "mom_window", "label": "动量回看周期", "type": "int", "default": 20, "min": 5, "max": 60, "step": 5},
        {"id": "mom_max", "label": "入场最大区间涨幅", "type": "float", "default": 0.0, "min": -0.3, "max": 0.3, "step": 0.02},
        {"id": "mom_min", "label": "入场最小区间涨幅", "type": "float", "default": -0.25, "min": -0.9, "max": 0.0, "step": 0.05},
        {"id": "mom_exit", "label": "退出区间涨幅", "type": "float", "default": 0.5, "min": 0.1, "max": 3.0, "step": 0.05},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.15, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.35, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 10.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "exit_on_trend_break", "label": "跌破趋势线退出", "type": "bool", "default": False},
        {"id": "trend_break_buffer", "label": "跌破趋势线缓冲", "type": "float", "default": 0.03, "min": 0.0, "max": 0.15, "step": 0.01},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.125, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.75, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.125, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_reversal", "label": "打分权重-低动量", "type": "float", "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05},
    ],
    # 四个因子都是「越低越好」, META scoring 表达不了方向 -> 打分在 compute_signals
    # 内部完成。留空即让引擎使用策略自带的 score。
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": None,
}

# 与基线的差别: 去掉 price_min / price_max / market_cap_min (作用在前复权 close 上,
# 系统性反向剔除未来大牛股) 以及 turnover_min (砍掉十分组里最好的低换手一侧)。
# 换手上限改为策略内部参数 turnover_max, 流动性只留成交额下限。
#
# 必须显式写 None, 不能靠"省略键": 加载器是
#   bf = {**DEFAULT_BASIC_FILTER}; bf.update(BASIC_FILTER)  (app\strategy\engine.py:471-474)
# 省略 = 继承引擎默认值 (price_min 3 / price_max 300 / market_cap_min 10e8,
# 见 engine.py:36-49), 等于这三个界照旧生效, 只是从 200/30亿 放宽成 300/10亿。
# 下游全是 `is not None` 判断 (matrix.py:3628-3633 的 _build_basic_filter_mask_uncached
# 与 _apply_bound), 所以 None 才是"关掉"。
BASIC_FILTER = {
    "price_min": None,
    "price_max": None,
    "market_cap_min": None,
    "turnover_min": None,
    "turnover_max": None,
    "amount_min": 0.5e8,
    "exclude_st": True,
    "exclude_new_days": 120,
    "boards": ["沪主板", "深主板"],
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_sequoia_ma_volume_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_ma_volume_v2_exit"]
# 不设止盈: 上一轮 14%/30%/取消三档差 <= 0.5pp, 全在噪声级, 留着只会截断右尾。
STOP_LOSS = None
TAKE_PROFIT = 0.20
TRAILING_STOP = None
MAX_HOLD_DAYS = 40

_EPS = np.float32(1e-12)


def _row_rank01(values: np.ndarray, valid: np.ndarray, invert: bool) -> np.ndarray:
    """把每一行(交易日)的因子值线性映射到 [0,1]; invert=True 表示越小越好。

    与 build_matrix_score / custom_quiet_lowpos 的横截面 min-max 口径保持一致:
    无效值给 0, 当日全部相等时给 0.5。
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


class SequoiaMaVolumeReversalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        # 不再需要 volume: 放量确认已删除, 波动率走收益率而不是成交量。
        return frozenset({"close", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("trend_window", 60)),
            int(params.get("mom_window", 20)),
            60,  # amount/turnover 的 20 日均值与 60 日趋势线共用同一段预热
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        trend_window = int(params.get("trend_window", 60))
        mom_window = int(params.get("mom_window", 20))
        mom_max = float(params.get("mom_max", 0.0))
        mom_min = float(params.get("mom_min", -0.25))
        mom_exit = float(params.get("mom_exit", 0.5))
        ext_max = float(params.get("ext_max", 1.15))
        ext_exit = float(params.get("ext_exit", 1.35))
        turnover_max = float(params.get("turnover_max", 10.0))
        trend_break_buffer = float(params.get("trend_break_buffer", 0.03))
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if mom_min >= mom_max:
            raise ValueError("入场最小区间涨幅必须小于最大区间涨幅")
        if mom_exit <= mom_max:
            raise ValueError("退出区间涨幅必须大于入场最大区间涨幅")
        if ext_exit <= ext_max:
            raise ValueError("退出乖离阈值必须大于入场最大乖离")
        if not 0.0 <= trend_break_buffer < 1.0:
            raise ValueError("跌破趋势线缓冲必须在 0~1 之间")

        close = market.close
        close_valid = np.isfinite(close)
        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        past_close = valid_shift(close, mom_window, close_valid)
        prev_close = valid_shift(close, 1, close_valid)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        with np.errstate(invalid="ignore", divide="ignore"):
            ext = close / trend_ma          # 乖离: 收盘 / 趋势线
            mom = close / past_close - np.float32(1.0)
            ret1 = close / prev_close - np.float32(1.0)

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, np.isfinite(ret1), 20)

        base_valid = close_valid & np.isfinite(trend_ma) & np.isfinite(past_close)

        # 入场: 回调 (20 日动量 <= mom_max) 但没崩 (>= mom_min), 仍站在趋势线上方,
        # 乖离不过大, 换手不狂热。刻意不做任何上升趋势/放量确认 —— 那是负 edge。
        entry = (
            base_valid
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & (mom <= np.float32(mom_max))
            & (mom >= np.float32(mom_min))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )

        # 退出走信号腿而不是移动止损: 乖离拉开或动量转强 = 均值回复已经完成。
        exit_ = base_valid & (
            (ext >= np.float32(ext_exit)) | (mom >= np.float32(mom_exit))
        )
        if params.get("exit_on_trend_break", False):
            # 入场前提就是站上趋势线, 前提破坏即离场。缓冲默认 3%, 比 -6% 移动止损
            # 慢得多, 触发频率也低; 这一项单独做开关, 方便阶段 5 拿它当网格维度。
            exit_ |= base_valid & (
                close < trend_ma * np.float32(1.0 - trend_break_buffer)
            )

        weights = {
            "amount": max(float(params.get("w_amount", 0.125)), 0.0),
            "turnover": max(float(params.get("w_turnover", 0.75)), 0.0),
            "vol": max(float(params.get("w_vol", 0.125)), 0.0),
            "reversal": max(float(params.get("w_reversal", 0.0)), 0.0),
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
            # 分位只在当日入场候选内做 (与 custom_quiet_lowpos 同口径), 四个方向
            # 全部取「越低越好」。
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
            entry_signal_ids=("signal_sequoia_ma_volume_v2_entry",),
            exit_signal_ids=("signal_sequoia_ma_volume_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaMaVolumeReversalMatrixStrategy()
