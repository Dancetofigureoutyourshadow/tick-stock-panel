"""关键价位计算 —— 独立模块,纯函数,无 IO / 无存储。

输入: 已经包含 OHLCV 的 polars 日 K DataFrame(内存中,通常来自 KlineRepository 缓存)。
输出: 4 类结构化价位点,供:
  - 图表 markLine 渲染(压力位 / 支撑位 / 成交密集区 / 枢轴点 / 前高前低)
  - AI 个股分析提示词(价位上下文)

设计:
  - 纯函数 + polars 向量化,毫秒级,无需落盘。
  - 每个点位带 {value, label, type, side, strength?},前端直接画水平价格线。
  - NaN/Inf 全部过滤,空数据返回空列表,不抛异常。
"""
from __future__ import annotations

import logging
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)


# ================================================================
# 输出结构
# ================================================================

class PriceLevel:
    """单个价位点的数据结构(用 dict 表达,这里只作文档说明)。

    {
      "value": 12.34,          # 价格
      "label": "压力位 R1",     # 显示标签
      "type": "pivot",         # 类型分组(同类型用一个开关按钮控制显隐)
      "side": "resistance",    # 方向:resistance(压力) / support(支撑) / neutral
      "strength": "medium",    # 强度:strong / medium / weak(可选,影响线型)
      "rank": 1,               # 档位(仅 pivot 有):0=P,1=R1/S1,2=R2/S2,3=R3/S3
                               #   前端按"显示到第几档"过滤,非 pivot 点位无此字段
    }
    """


# 价位分组 → 开关 key。前端按这个 type 显隐。
LEVEL_TYPES = {
    "sr": "压力支撑",        # 布林带 + swing 高低点
    "profile": "成交密集区",  # 成交量分布 POC + 密集区
    "pivot": "枢轴点",        # 经典 Pivot P/R/S
    "extreme": "前高前低",    # 60/120/250 日极值
    "keltner": "Keltner通道", # 短/中/长三档 MA±n×ATR
    "atr_stop": "ATR止损",    # close±nATR 动态止盈止损
    "gap": "缺口位",          # 未回补跳空缺口
    "fib": "斐波那契",        # 回撤位 0.236~0.786
    "round": "整数关口",      # 心理整数位
}


# ================================================================
# 1. 压力位 / 支撑位 —— 布林带上下轨 + 局部 swing 高低点
# ================================================================

def _support_resistance(df: pl.DataFrame) -> list[dict]:
    """布林带上下轨(压力/支撑带)。

    本组只放"通道型"压力支撑 —— 即布林带的上下轨,代表近期波动边界。
    局部 swing 高低点 / 前高前低 等点位归到 extreme 组,避免与本组重叠。
    """
    if df.is_empty() or df.height < 20:
        return []

    out: list[dict] = []
    last = df.tail(1)

    if "boll_upper" in df.columns:
        bu = last["boll_upper"][0]
        if _ok(bu):
            out.append({"value": round(float(bu), 2), "label": "压力位(布林上轨)",
                        "type": "sr", "side": "resistance", "strength": "medium"})
    if "boll_lower" in df.columns:
        bl = last["boll_lower"][0]
        if _ok(bl):
            out.append({"value": round(float(bl), 2), "label": "支撑位(布林下轨)",
                        "type": "sr", "side": "support", "strength": "medium"})

    return out


# ================================================================
# 2. 成交密集区 —— 成交量分布 (Volume Profile)
# ================================================================

def _volume_profile(df: pl.DataFrame, bins: int = 40) -> list[dict]:
    """按价格分桶统计成交量,找 POC(控制点)+ 高成交密集区。

    密集区 = 成交量高于均值的桶,按成交量降序取前 3 个作为关键价位带。
    """
    if df.is_empty() or "volume" not in df.columns or df.height < 20:
        return []

    hi = float(df["high"].max())
    lo = float(df["low"].min())
    if not (hi > lo > 0):
        return []

    # 每根 K 的价格区间中点 × 成交量 ≈ 该价位层贡献的成交量(简化模型)
    df2 = df.select([
        ((pl.col("high") + pl.col("low")) / 2).alias("mid"),
        pl.col("volume").alias("vol"),
    ]).drop_nulls()

    # 桶边界:bins 个桶需要 bins-1 个内部 break,cut 据此切成 bins 段
    step = (hi - lo) / bins
    edges = [lo + i * step for i in range(bins + 1)]   # 含首尾,共 bins+1 个边界值
    breaks = edges[1:-1]                                 # 内部 break,bins-1 个
    bin_labels = [f"{i}" for i in range(bins)]          # 桶序号 0..bins-1
    # 至少要有 1 个不同的内部 break
    if len(set(f"{b:.6f}" for b in breaks)) < 1:
        return []

    df2 = df2.with_columns(
        pl.col("mid").cut(breaks, labels=bin_labels).alias("bin")
    )
    prof = df2.group_by("bin").agg(pl.col("vol").sum())
    if prof.is_empty():
        return []

    # 把桶序号字符串还原为 int,以便回查 edges;并按序号排序保证可索引
    prof = prof.with_columns(pl.col("bin").cast(pl.Int64).alias("bi")).sort("bi")
    bin_ids = prof["bi"].to_list()
    vols = prof["vol"].to_list()
    mean_vol = sum(vols) / len(vols) if vols else 0

    def bin_mid(bin_id: int) -> float:
        return (edges[bin_id] + edges[bin_id + 1]) / 2

    close = float(df.tail(1)["close"][0])

    out: list[dict] = []
    # POC:成交量最大的桶
    poc_pos = max(range(len(vols)), key=lambda i: vols[i])
    poc_mid = bin_mid(bin_ids[poc_pos])
    out.append({"value": round(poc_mid, 2), "label": "成交密集区(POC)",
                "type": "profile", "side": _side(poc_mid, close), "strength": "strong"})

    # 其他高成交区(高于均值,排除 POC),按成交量降序取 2 个
    candidates = [(i, v) for i, v in enumerate(vols) if v > mean_vol and i != poc_pos]
    candidates.sort(key=lambda x: x[1], reverse=True)
    for i, _v in candidates[:2]:
        mid = bin_mid(bin_ids[i])
        out.append({"value": round(mid, 2), "label": "成交密集区",
                    "type": "profile", "side": _side(mid, close), "strength": "medium"})
    return out


# ================================================================
# 3. 枢轴点 (Pivot Point) —— 经典公式,基于最近完整交易日
# ================================================================

def _pivot_points(df: pl.DataFrame) -> list[dict]:
    """经典 Pivot:P = (H+L+C)/3, R1/R2/R3, S1/S2/S3。

    基准:最后 1 根 K(代表"上一交易日")。实务中常用前一日,这里取最后一根。
    """
    if df.is_empty():
        return []
    last = df.tail(1)
    h = last["high"][0]
    l = last["low"][0]
    c = last["close"][0]
    if not _ok(h) or not _ok(l) or not _ok(c):
        return []

    h, l, c = float(h), float(l), float(c)
    p = (h + l + c) / 3
    r1 = 2 * p - l
    s1 = 2 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = h + 2 * (p - l)
    s3 = l - 2 * (h - p)

    def lv(v: float, label: str, side: str, strength: str, rank: int) -> dict:
        # rank:档位标记,前端据此按"显示到第几档"过滤
        #   0 = 枢轴位 P(始终显示)
        #   1 = R1/S1(第一档压力/支撑)
        #   2 = R2/S2(第二档)
        #   3 = R3/S3(第三档,极端,实际很少触及)
        return {"value": round(v, 2), "label": label, "type": "pivot",
                "side": side, "strength": strength, "rank": rank}

    return [
        lv(p, "枢轴位 P", "neutral", "strong", 0),
        lv(r1, "压力位 R1", "resistance", "medium", 1),
        lv(r2, "压力位 R2", "resistance", "medium", 2),
        lv(r3, "压力位 R3", "resistance", "weak", 3),
        lv(s1, "支撑位 S1", "support", "medium", 1),
        lv(s2, "支撑位 S2", "support", "medium", 2),
        lv(s3, "支撑位 S3", "support", "weak", 3),
    ]


# ================================================================
# 4. 前高 / 前低 —— 60 / 120 / 250 日极值
# ================================================================

def _extreme_levels(df: pl.DataFrame) -> list[dict]:
    """关键前高 / 前低 —— 历史极值 + 近期 swing 高低点(收敛后)。

    设计:把所有"前高前低"类点位集中在本组,与 sr(通道)区分:
      - 60 日极值:近一季度高低点(短期参照)
      - 250 日极值:年度高低点(牛熊分界参照);跳过 120 日(被 250 日包含,信息冗余)
      - swing 高低点:近期局部转折点,每侧只取距当前价最近的 2 个
    """
    if df.is_empty():
        return []
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    out: list[dict] = []

    # —— 历史极值(只取 60 / 250,避免中间档冗余)——
    for n in (60, 250):
        if df.height < n:
            continue
        sub = df.tail(n)
        hi = float(sub["high"].max())
        lo = float(sub["low"].min())
        if _ok(hi):
            out.append({"value": round(hi, 2), "label": f"{n}日新高",
                        "type": "extreme", "side": "resistance", "strength": "strong"})
        if _ok(lo):
            out.append({"value": round(lo, 2), "label": f"{n}日新低",
                        "type": "extreme", "side": "support", "strength": "strong"})

    # —— 近期 swing 高低点(每侧只取距当前价最近的 2 个,避免点位爆炸)——
    win = 5
    if df.height > win * 2 and close:
        highs = df["high"].to_list()
        lows = df["low"].to_list()
        swing_highs: list[float] = []
        swing_lows: list[float] = []
        for i in range(win, len(highs) - win):
            if highs[i] == max(highs[i - win:i + win + 1]):
                swing_highs.append(float(highs[i]))
            if lows[i] == min(lows[i - win:i + win + 1]):
                swing_lows.append(float(lows[i]))

        # 聚合 ±1% 相近价位,再按距当前价排序取最近 2 个
        agg_h = _aggregate_levels(swing_highs, 0.01)
        agg_h = [v for v in agg_h if v > close * 1.001]
        agg_h.sort(key=lambda v: abs(v - close))
        for v in agg_h[:2]:
            out.append({"value": round(v, 2), "label": "前高",
                        "type": "extreme", "side": "resistance", "strength": "medium"})

        agg_l = _aggregate_levels(swing_lows, 0.01)
        agg_l = [v for v in agg_l if v < close * 0.999]
        agg_l.sort(key=lambda v: abs(v - close))
        for v in agg_l[:2]:
            out.append({"value": round(v, 2), "label": "前低",
                        "type": "extreme", "side": "support", "strength": "medium"})

    return out


# ================================================================
# 5. Keltner 通道 —— MA ± n × ATR,短/中/长三档
# ================================================================

def _keltner_channels(df: pl.DataFrame) -> list[dict]:
    """三档 Keltner 通道(波动自适应边界)。

    基于 ATR 的通道:均线 ± n×ATR。ATR 自适应波动,通道宽度随行情自动收缩/扩张,
    比布林带(基于标准差)更稳定,实战常用作趋势边界与突破参照。

    三档:
      - 短期:MA20 ± 2×ATR    (近期波动带,约一个月)
      - 中期:MA60 ± 2.5×ATR  (季度波动带)
      - 长期:MA120 ± 3×ATR   (半年波动带,牛熊趋势边界)
    """
    if df.is_empty() or df.height < 20:
        return []
    out: list[dict] = []
    last = df.tail(1)
    close = float(last["close"][0]) if "close" in df.columns else 0
    if not close:
        return []

    # ATR 列由 compute_indicators 生成(atr_14);缺失则跳过
    if "atr_14" not in df.columns:
        return []
    atr = float(last["atr_14"][0])
    if not _ok(atr):
        return []

    def _band(ma_col: str | None, n: int, label_short: str, window: int) -> None:
        """单档通道:优先取预计算列,缺失则现场 rolling_mean。"""
        ma_val: float | None = None
        if ma_col and ma_col in df.columns:
            v = last[ma_col][0]
            ma_val = float(v) if _ok(v) else None
        elif df.height >= window:
            # ma120 等未预计算列:现场算
            v = df.select(pl.col("close").rolling_mean(window)).tail(1)["close"][0]
            ma_val = float(v) if _ok(v) else None
        if ma_val is None:
            return
        upper = ma_val + n * atr
        lower = ma_val - n * atr
        out.append({"value": round(upper, 2), "label": f"{label_short}通道上轨",
                    "type": "keltner", "side": _side(upper, close), "strength": "medium"})
        out.append({"value": round(lower, 2), "label": f"{label_short}通道下轨",
                    "type": "keltner", "side": _side(lower, close), "strength": "medium"})

    _band("ma20", 2, "短期", 20)
    _band("ma60", 2.5, "中期", 60)
    _band(None, 3, "长期", 120)   # ma120 未预计算,现场 rolling_mean
    return out


# ================================================================
# 6. ATR 止损位 —— close ± n × ATR,动态止盈止损
# ================================================================

def _atr_stops(df: pl.DataFrame) -> list[dict]:
    """基于 ATR 的动态止损/止盈位。

    ATR 衡量平均真实波幅,close ± n×ATR 是交易者最常用的止损位算法:
      - 止损位:close - 2×ATR  (跌破即趋势破坏)
      - 止盈位:close + 2×ATR  (突破即顺势扩展)
      - 近端波动带:close ± 1.5×ATR (中短期风控参考)
    """
    if df.is_empty() or "atr_14" not in df.columns:
        return []
    last = df.tail(1)
    close = float(last["close"][0])
    atr = float(last["atr_14"][0])
    if not _ok(close) or not _ok(atr):
        return []

    def lv(v: float, label: str, side: str, strength: str) -> dict:
        return {"value": round(v, 2), "label": label, "type": "atr_stop",
                "side": side, "strength": strength}

    return [
        lv(close + 2 * atr, "ATR 止盈(+2)", "resistance", "medium"),
        lv(close + 1.5 * atr, "ATR 上轨(+1.5)", "resistance", "weak"),
        lv(close - 1.5 * atr, "ATR 下轨(-1.5)", "support", "weak"),
        lv(close - 2 * atr, "ATR 止损(-2)", "support", "medium"),
    ]


# ================================================================
# 7. 缺口位 (Gap) —— 未回补的跳空缺口
# ================================================================

def _gap_levels(df: pl.DataFrame, lookback: int = 120) -> list[dict]:
    """近期未回补的向上/向下跳空缺口。

    向上缺口:当日 low > 前日 high(开盘跳空高开,全天未回补)
    向下缺口:当日 high < 前日 low(开盘跳空低开,全天未回补)

    缺口是天然的支撑/阻力位。只保留"未回补"的(后续价格未回到缺口区间内),
    并按价格聚合相近缺口(±0.5%),每方向只取距当前价最近的 2~3 个。
    """
    if df.is_empty() or df.height < 5:
        return []
    sub = df.tail(lookback) if df.height > lookback else df
    close = float(df.tail(1)["close"][0])
    highs = sub["high"].to_list()
    lows = sub["low"].to_list()

    up_gaps: list[tuple[float, float]] = []   # (缺口低点, 缺口高点)
    dn_gaps: list[tuple[float, float]] = []
    for i in range(1, len(highs)):
        if _ok(highs[i]) and _ok(lows[i]) and _ok(highs[i - 1]) and _ok(lows[i - 1]):
            if lows[i] > highs[i - 1]:          # 向上缺口
                up_gaps.append((highs[i - 1], lows[i]))
            elif highs[i] < lows[i - 1]:        # 向下缺口
                dn_gaps.append((highs[i], lows[i - 1]))

    def _filter_unfilled(gaps: list[tuple[float, float]], is_up: bool) -> list[float]:
        """过滤掉已被后续价格回补的缺口,取缺口价位中点。"""
        mids: list[float] = []
        for g_lo, g_hi in gaps:
            # 未回补判定:当前价不在缺口区间内
            if is_up and close >= g_hi:       # 向上缺口:价格已超过缺口上沿 = 未回补(站在缺口上方)
                mids.append((g_lo + g_hi) / 2)
            elif not is_up and close <= g_lo:  # 向下缺口:价格已低于缺口下沿 = 未回补
                mids.append((g_lo + g_hi) / 2)
        # 聚合相近缺口 + 按距当前价排序取最近 3 个
        agg = _aggregate_levels(mids, 0.005)
        agg.sort(key=lambda v: abs(v - close))
        return agg[:3]

    out: list[dict] = []
    for mid in _filter_unfilled(up_gaps, True):
        out.append({"value": round(mid, 2), "label": "向上缺口",
                    "type": "gap", "side": _side(mid, close), "strength": "medium"})
    for mid in _filter_unfilled(dn_gaps, False):
        out.append({"value": round(mid, 2), "label": "向下缺口",
                    "type": "gap", "side": _side(mid, close), "strength": "medium"})
    return out


# ================================================================
# 8. 斐波那契回撤 —— 基于近期波段的回撤位
# ================================================================

def _fibonacci_levels(df: pl.DataFrame, window: int = 120) -> list[dict]:
    """基于近期一段明确趋势的斐波那契回撤位。

    取近 window 个交易日的最高/最低点:
      - 若高点出现在低点之后(上涨波段):从低到高,回撤 = high - range × ratio
      - 若低点出现在高点之后(下跌波段):从高到低,回撤 = low + range × ratio
    比率:0.236 / 0.382 / 0.5 / 0.618 / 0.786
    """
    if df.is_empty() or df.height < 10:
        return []
    sub = df.tail(window) if df.height > window else df
    close = float(df.tail(1)["close"][0])

    highs = sub["high"].to_list()
    lows = sub["low"].to_list()
    hi_pos = highs.index(max(highs))
    lo_pos = lows.index(min(lows))
    hi_val = float(highs[hi_pos])
    lo_val = float(lows[lo_pos])
    if not _ok(hi_val) or not _ok(lo_val) or hi_val <= lo_val:
        return []

    ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    rng = hi_val - lo_val

    out: list[dict] = []
    # 判断波段方向:高点在低点之后 = 上涨波段(从低回撤)
    up_trend = hi_pos > lo_pos
    for r in ratios:
        if up_trend:
            val = hi_val - rng * r          # 从高点向下回撤
        else:
            val = lo_val + rng * r          # 从低点向上回撤
        out.append({"value": round(val, 2), "label": f"Fib {int(r * 1000) / 10:.1f}%",
                    "type": "fib", "side": _side(val, close), "strength": "medium"})
    return out


# ================================================================
# 9. 整数关口 —— 心理支撑/阻力位
# ================================================================

def _round_numbers(df: pl.DataFrame, pct: float = 0.10, max_count: int = 8) -> list[dict]:
    """当前价附近的心理整数关口。

    整数位(如 10/11/12元,或 60/65/70元)是天然的心理支撑/阻力,
    低价股尤其明显。按价格量级自适应步长:
      - 价格 < 10:   步长 0.5  (如 6.5, 7.0, 7.5)
      - 价格 < 20:   步长 1    (如 11, 12, 13)
      - 价格 < 100:  步长 5    (如 60, 65, 70)
      - 价格 < 500:  步长 10   (如 110, 120, 130)
      - 价格 >= 500: 步长 50   (如 1100, 1150, 1200)
    过滤掉距当前价 <1% 的(太近,无分析价值),最多 max_count 个。
    """
    if df.is_empty():
        return []
    close = float(df.tail(1)["close"][0])
    if not _ok(close):
        return []

    if close < 10:
        step = 0.5
    elif close < 20:
        step = 1.0
    elif close < 100:
        step = 5.0
    elif close < 500:
        step = 10.0
    else:
        step = 50.0

    lo = close * (1 - pct)
    hi = close * (1 + pct)
    # 找区间 [lo, hi] 内所有 step 的整数倍(严格限定在区间内)
    start = (int(lo / step) + (1 if lo % step > 0 else 0)) * step
    candidates: list[float] = []
    v = start
    while v <= hi:
        if v > 0:
            candidates.append(round(v, 2))
        v += step

    # 按距当前价从近到远排序,取前 max_count 个
    candidates.sort(key=lambda x: abs(x - close))
    out: list[dict] = []
    for v in candidates[:max_count]:
        # 过滤距当前价 <1% 的(太近,无分析价值)
        if abs(v - close) / close < 0.01:
            continue
        out.append({"value": round(v, 2), "label": f"整数关口 {v:g}",
                    "type": "round", "side": _side(v, close), "strength": "weak"})
    return out

def compute_levels(df: pl.DataFrame) -> dict[str, list[dict]]:
    """计算 9 类价位点,返回 {分组key: [点位...]}。

    分组 key 与 LEVEL_TYPES 一致(sr / profile / pivot / extreme /
    keltner / atr_stop / gap / fib / round),前端按 key 渲染开关按钮,逐组显隐。
    """
    if df.is_empty():
        return {k: [] for k in LEVEL_TYPES}

    try:
        return {
            "sr": _support_resistance(df),
            "profile": _volume_profile(df),
            "pivot": _pivot_points(df),
            "extreme": _extreme_levels(df),
            "keltner": _keltner_channels(df),
            "atr_stop": _atr_stops(df),
            "gap": _gap_levels(df),
            "fib": _fibonacci_levels(df),
            "round": _round_numbers(df),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("compute_levels failed: %s", e)
        return {k: [] for k in LEVEL_TYPES}


def summarize_levels(levels: dict[str, list[dict]], close: float | None) -> str:
    """生成给 AI 提示词的价位摘要文本(紧凑,供上下文)。"""
    if not close:
        return "无价位数据"
    parts: list[str] = []
    # 当前价
    parts.append(f"当前价 {close:.2f}")
    # 每组取前 2 个最相关的(距当前价近的优先)
    for key, label in LEVEL_TYPES.items():
        pts = levels.get(key, [])
        if not pts:
            continue
        # 按距当前价排序,取前 2
        ranked = sorted(pts, key=lambda p: abs(p["value"] - close))[:2]
        desc = "、".join(
            f"{p['label']}={p['value']}" for p in ranked
        )
        parts.append(f"{label}: {desc}")
    return " · ".join(parts)


# ================================================================
# 内部工具
# ================================================================

def _ok(v: Any) -> bool:
    """数值有效(非空/非 NaN/非 Inf/正数)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    import math
    return math.isfinite(f) and f > 0


def _side(level: float, close: float) -> str:
    """价位相对当前价的方向。"""
    if level > close * 1.001:
        return "resistance"
    if level < close * 0.999:
        return "support"
    return "neutral"


def _aggregate_levels(values: list[float], tol: float) -> list[float]:
    """把相近的价位聚合(±tol),返回去重后的代表值(保留最新)。"""
    if not values:
        return []
    values = sorted(values)
    out: list[float] = [values[0]]
    for v in values[1:]:
        if abs(v - out[-1]) / out[-1] <= tol:
            out[-1] = v  # 聚合到最新(更近期)
        else:
            out.append(v)
    return out
