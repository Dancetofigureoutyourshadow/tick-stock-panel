"""X 定向增发公告 v2 —— **不是原策略的等价实现**, 是缺公告源时的量价代理。

⚠️ 首先必须讲清楚的事:

原策略 custom_sequoia_private_placement 依赖 ak.stock_qbzf_em() 的「发行方式 / 发行
日期 / 股票代码」三列公告字段, 而本项目的策略上下文只有 enriched 行情字段。已在
backend/ 与 data/ 全量搜过「发行方式」「定向增发」「qbzf」「发行日期」四个关键词,
唯一命中就是原策略文件自己 —— 本地**没有**任何定向增发公告数据集。所以原策略是
fail-closed 的 (缺列时 return pl.lit(False)), 它跑回测恒为空仓, 既不能证伪也不能调参。

因此本文件**不复现公告事件**, 也不虚构任何公告字段或公告接口。它换成一条可观测的
量价代理, 并且把这件事写进 name / tags / description, 便于任何人一眼看出差异:

  代理假设: 定增标的在公告前后通常具备「长期滞涨 + 近期缩量企稳 + 仍在中期趋势线
  上方」的量价特征 (定增需要股价维稳、发行价参考近期均价、机构折价入场)。本文件筛
  的就是这三条, **不是**"确实发布了定增公告的股票"。

这条代理是可回测、可调参、可复核的; 它的经济含义与原策略只是同向而非等价。若日后把
公告数据集接进策略上下文, 应当回到原 polars_expr 版本 (或新建 v3) 用真实公告事件替
换本文件的代理腿, 而不是把本文件的结论当成定增策略的结论。

打分与风控沿用 custom_sequoia_ma_volume_v2 的 v2 配方 (依据见该文件 docstring 与
.tmp-strategy-opt-v2/ 存档): 低成交额 / 低换手 / 低波动 / 低长期动量四因子横截面分位
全部"越低越好"; basic_filter 去掉 price_min / price_max / market_cap_min /
turnover_min / turnover_max, 只留 amount_min; 风控拆掉止损与移动止损, 走
take_profit 0.20 + max_hold_days 40。

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
    "id": "custom_sequoia_private_placement_v2",
    "name": "X 定向增发公告 v2 (无公告源·量价代理)",
    "description": "本地无定向增发公告数据集, 本策略不复现公告事件: 改用长期滞涨+近期缩量企稳+仍在趋势线上方的量价代理, 按低成交额/低换手/低波动/低长期动量打分。结论不等于定增策略结论。",
    "tags": ["Sequoia-X", "定向增发", "量价代理", "非等价实现", "缺公告数据源", "v2"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "params": [
        {"id": "long_window", "label": "长期回看周期", "type": "int", "default": 180, "min": 40, "max": 360, "step": 20},
        {"id": "long_mom_max", "label": "长期最大涨幅(滞涨上限)", "type": "float", "default": 0.20, "min": -0.5, "max": 1.0, "step": 0.05},
        {"id": "long_mom_min", "label": "长期最小涨幅", "type": "float", "default": -0.50, "min": -0.95, "max": 0.0, "step": 0.05},
        {"id": "trend_window", "label": "趋势均线周期", "type": "int", "default": 120, "min": 20, "max": 120, "step": 5},
        {"id": "mom_window", "label": "短期动量回看周期", "type": "int", "default": 20, "min": 5, "max": 60, "step": 5},
        {"id": "mom_max", "label": "入场最大区间涨幅", "type": "float", "default": 0.0, "min": -0.3, "max": 0.3, "step": 0.02},
        {"id": "mom_min", "label": "入场最小区间涨幅", "type": "float", "default": -0.25, "min": -0.9, "max": 0.0, "step": 0.05},
        {"id": "mom_exit", "label": "退出区间涨幅", "type": "float", "default": 0.5, "min": 0.1, "max": 3.0, "step": 0.05},
        {"id": "vol_dry_ratio", "label": "量能干涸倍数", "type": "float", "default": 0.95, "min": 0.20, "max": 2.00, "step": 0.05},
        {"id": "ext_max", "label": "入场最大乖离(收盘/趋势线)", "type": "float", "default": 1.08, "min": 1.0, "max": 2.0, "step": 0.05},
        {"id": "ext_exit", "label": "退出乖离阈值", "type": "float", "default": 1.35, "min": 1.05, "max": 3.0, "step": 0.05},
        {"id": "turnover_max", "label": "入场最高换手率(%)", "type": "float", "default": 10.0, "min": 0.5, "max": 50.0, "step": 0.5},
        {"id": "exit_on_trend_break", "label": "跌破趋势线退出", "type": "bool", "default": False},
        {"id": "trend_break_buffer", "label": "跌破趋势线缓冲", "type": "float", "default": 0.03, "min": 0.0, "max": 0.15, "step": 0.01},
        {"id": "w_amount", "label": "打分权重-低成交额", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_turnover", "label": "打分权重-低换手", "type": "float", "default": 0.30, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_vol", "label": "打分权重-低波动", "type": "float", "default": 0.50, "min": 0.0, "max": 1.0, "step": 0.05},
        {"id": "w_longmom", "label": "打分权重-低长期动量", "type": "float", "default": 0.10, "min": 0.0, "max": 1.0, "step": 0.05},
    ],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "limit": None,
    # 原策略声明的公告字段契约保留在这里, 供日后接入公告源时对齐; 本文件不使用它们。
    "unavailable_data": ["发行方式", "发行日期", "股票代码"],
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
ENTRY_SIGNALS = ["signal_sequoia_private_placement_v2_entry"]
EXIT_SIGNALS = ["signal_sequoia_private_placement_v2_exit"]
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


class SequoiaPlacementProxyMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "volume", "amount", "turnover_rate"})

    def required_warmup_bars(self, params: dict) -> int:
        return max(
            int(params.get("long_window", 180)),
            int(params.get("trend_window", 120)),
            int(params.get("mom_window", 20)),
            20,
        ) + 5

    def compute_signals(
        self,
        market: MarketDataMatrix,
        params: dict,
    ) -> SignalMatrix:
        long_window = int(params.get("long_window", 180))
        long_mom_max = float(params.get("long_mom_max", 0.20))
        long_mom_min = float(params.get("long_mom_min", -0.50))
        trend_window = int(params.get("trend_window", 120))
        mom_window = int(params.get("mom_window", 20))
        mom_max = float(params.get("mom_max", 0.0))
        mom_min = float(params.get("mom_min", -0.25))
        mom_exit = float(params.get("mom_exit", 0.5))
        vol_dry_ratio = float(params.get("vol_dry_ratio", 0.95))
        ext_max = float(params.get("ext_max", 1.08))
        ext_exit = float(params.get("ext_exit", 1.35))
        turnover_max = float(params.get("turnover_max", 10.0))
        trend_break_buffer = float(params.get("trend_break_buffer", 0.03))
        # 入场与退出的阈值必须留出间隔, 否则同一根 bar 上既入场又退出。
        if long_mom_min >= long_mom_max:
            raise ValueError("长期最小涨幅必须小于长期最大涨幅")
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
        volume_valid = close_valid & np.isfinite(market.volume)

        prev_close = valid_shift(close, 1, close_valid)
        past_close = valid_shift(close, mom_window, close_valid)
        long_base = valid_shift(close, long_window, close_valid)
        trend_ma = valid_rolling_mean(close, close_valid, trend_window)
        vol_ma20 = valid_rolling_mean(market.volume, volume_valid, 20)

        amount = market.field("amount")
        turnover = market.field("turnover_rate")
        amount_valid = np.isfinite(amount) & (amount > 0)
        turnover_valid = np.isfinite(turnover)

        with np.errstate(invalid="ignore", divide="ignore"):
            ret1 = close / prev_close - np.float32(1.0)
            mom = close / past_close - np.float32(1.0)
            long_mom = close / long_base - np.float32(1.0)
            ext = close / trend_ma

        amount_ma = valid_rolling_mean(amount, amount_valid, 20)
        turnover_ma = valid_rolling_mean(turnover, turnover_valid, 20)
        vol20 = valid_rolling_std(ret1, np.isfinite(ret1), 20)

        base_valid = (
            close_valid
            & np.isfinite(trend_ma)
            & np.isfinite(past_close)
            & np.isfinite(long_base)
        )

        # 入场 (代理腿, 不是公告事件): 长期滞涨 + 短期回调但没崩 + 仍站趋势线 +
        # 量能不放大 + 换手不狂热。
        entry = (
            base_valid
            & (long_mom <= np.float32(long_mom_max))
            & (long_mom >= np.float32(long_mom_min))
            & (close > trend_ma)
            & (ext <= np.float32(ext_max))
            & (mom <= np.float32(mom_max))
            & (mom >= np.float32(mom_min))
            & np.isfinite(vol_ma20)
            & (market.volume <= vol_ma20 * np.float32(vol_dry_ratio))
            & turnover_valid
            & (turnover <= np.float32(turnover_max))
        )

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
            "longmom": max(float(params.get("w_longmom", 0.10)), 0.0),
        }
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("四个打分权重不能同时为 0")
        parts = {
            "amount": (amount_ma, np.isfinite(amount_ma)),
            "turnover": (turnover_ma, np.isfinite(turnover_ma)),
            "vol": (vol20, np.isfinite(vol20)),
            "longmom": (long_mom, np.isfinite(long_mom)),
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
            entry_signal_ids=("signal_sequoia_private_placement_v2_entry",),
            exit_signal_ids=("signal_sequoia_private_placement_v2_exit",),
        )


MATRIX_STRATEGY = SequoiaPlacementProxyMatrixStrategy()
