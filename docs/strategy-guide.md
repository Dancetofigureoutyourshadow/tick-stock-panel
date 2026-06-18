# 策略开发指南

本文档是策略开发的完整参考。人类开发者参考它编写策略，AI 读取它生成策略代码。

## 1. 策略文件格式

每个策略是一个 Python 文件，放在以下目录：

- 内置策略: `backend/app/strategy/builtin/`
- 自定义策略: `data/strategies/custom/`，建议文件名和 ID 使用 `custom_时间戳`
- AI 生成策略: `data/strategies/ai/`，文件名和 ID 使用 `ai_时间戳`

## 2. 文件结构模板

```python
"""策略简短描述"""
import polars as pl

META = {
    "id": "strategy_id",              # 英文ID, 唯一, 文件名同名；自定义策略建议 custom_时间戳
    "name": "策略中文名",              # 显示名称
    "description": "策略详细描述",     # 一句话说明策略逻辑
    "tags": ["标签1", "标签2"],        # 分类标签

    # 基础过滤参数 (Stage 1, 引擎统一处理)
    "basic_filter": {
        "price_min": 5,               # 最低价格
        "price_max": 200,             # 最高价格
        "market_cap_min": 20e8,       # 最小总市值 (元)
        "amount_min": 1e8,            # 最小成交额 (元)
        "exclude_st": True,           # 排除 ST/*ST/退市
        "exclude_new_days": 60,       # 排除上市N天内新股
    },

    # 策略参数 (Stage 2, filter() 使用, 前端渲染为表单)
    # 用户可能调节的阈值通过 params 暴露；公式常数、固定窗口边界不必强行参数化
    "params": [
        {
            "id": "param_id",          # 参数ID, filter() 中 params.get("param_id")
            "label": "参数显示名",      # 前端显示
            "type": "float",           # float | int | select
            "default": 2.0,            # 默认值
            "min": 0.5,                # 最小值 (float/int)
            "max": 10.0,               # 最大值 (float/int)
            "step": 0.1,               # 步长
            # select 类型用 options:
            # "options": ["ma5", "ma10", "ma20", "ma60"],
        },
    ],

    # 评分权重 (用于排序, 权重总和 = 1.0)
    "scoring": {
        "momentum_60d": 0.4,
        "vol_ratio_5d": 0.3,
        "change_pct": 0.3,
    },

    "order_by": "score",              # 排序字段, 通常用 "score"
    "descending": True,               # True = 从高到低
    "limit": 100,                      # 最多返回条数
}

# 买入信号 (回测 + 监控用, 对应 enriched 表的信号列名)
ENTRY_SIGNALS = ["signal_broken_board_recovery"]

# 卖出信号
EXIT_SIGNALS = ["signal_ma20_breakdown"]

# 止损 (负数, 如 -0.08 = -8%)
STOP_LOSS = -0.08

# 最长持有天数
MAX_HOLD_DAYS = 20

# 提醒条件 (监控用)
ALERTS = [
    {"field": "signal_broken_board_recovery", "message": "反包信号"},
    {"field": "rsi_14", "op": ">", "value": 80, "message": "RSI超买预警"},
]


# 策略规则（人类可读，逐条编号，至少 3 条）
RULES = """
1. 规则描述一
2. 规则描述二
3. 规则描述三
"""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    """策略核心过滤逻辑。

    df:     Stage 1 基础过滤后的 enriched 数据
    params: META.params 中定义的参数值 (用户可在前端覆盖)

    返回:   Polars 布尔表达式 (pl.Expr)
    """
    vol_min = params.get("vol_ratio_min", 2.0)
    return (
        ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
        & pl.col("signal_broken_board_recovery").fill_null(False)
        & (pl.col("vol_ratio_5d") >= vol_min)
    )
```

### 历史窗口策略（filter_history）

普通 `filter()` 只接收当前日期的单日数据。当策略需要以下逻辑时，必须使用 `filter_history()`：

- "最近 N 天内出现过某个事件"（如涨停、金叉）
- "某个事件发生后的第 X 天"（如涨停后放量下跌）
- "前高 / 前低 / 上次某事件的价格"等需要回溯历史的自定义字段
- 任何需要多日数据才能计算的时序逻辑

**不需要** `filter_history()` 的场景：只用当日指标列做比较（如 close > ma60、rsi_14 < 30）。

```python
LOOKBACK_DAYS = 8  # 回看交易日数，根据策略需要设置

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    """df 包含目标日期之前 LOOKBACK_DAYS 个交易日的数据（所有股票混合）。
    每行包含 symbol, date 及所有指标列/信号列。

    返回值: 筛选后的 DataFrame，引擎会最终保留目标日期行。
    """
    if df.is_empty() or "date" not in df.columns:
        return df

    down_pct = float(params.get("prev_down_pct", -0.02))
    vol_ratio = float(params.get("volume_ratio", 1.2))
    tolerance = float(params.get("reversal_tolerance", 0.005))
    latest = df["date"].max()

    # 示例: 前日明显阴线下跌，今日放量阳线反包前日实体
    hist = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("open").shift(1).over("symbol").alias("_prev_open"),
            pl.col("high").shift(1).over("symbol").alias("_prev_high"),
            pl.col("close").shift(1).over("symbol").alias("_prev_close"),
            pl.col("volume").shift(1).over("symbol").alias("_prev_volume"),
            pl.col("change_pct").shift(1).over("symbol").alias("_prev_change_pct"),
        ])
    )

    return hist.filter(pl.col("date") == latest).filter(
        (pl.col("_prev_close") < pl.col("_prev_open"))
        & (pl.col("_prev_change_pct") <= down_pct)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("close") > pl.col("_prev_open"))
        & (pl.col("close") >= pl.col("_prev_high") * (1 - tolerance))
        & (pl.col("volume") >= pl.col("_prev_volume") * vol_ratio)
        & ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
    )
```

**关键要点：**
- `LOOKBACK_DAYS` 决定引擎加载多少天的数据，设为策略逻辑需要的最大回看天数
- 优先使用 Polars 的 `with_columns`、`over("symbol")`、`group_by`、`join`、`filter` 实现历史逻辑，避免把数据转成 Python list/dict 循环
- 只有遇到表达式难以描述的复杂状态机时，才使用 `partition_by("symbol")` + `to_dicts()` 逐股票分析
- 返回的 DataFrame 必须只包含目标日期行，通过 `pl.col("date") == latest` 过滤
- 未声明 `filter_history()` 的策略走普通 `filter()` 路径，不受影响

## 3. 常用指标列（参考，可直接使用）

以下列在数据中已预计算，可直接引用。**但如果这些列无法满足策略需求，可以不用，自行在 `filter_history()` 中基于 enriched 表的数据（已复权，含所有指标列和信号列）计算任何需要的字段。**

### 价格相关

| 列名 | 类型 | 说明 |
|------|------|------|
| open, high, low, close | float | OHLCV 开高低收 |
| prev_close | float | 昨收价 |
| change_pct | float | 涨跌幅 (如 0.032 = +3.2%) |
| change_amount | float | 涨跌额 |
| amount | float | 成交额 |
| amplitude | float | 振幅 |

### 均线

| 列名 | 说明 |
|------|------|
| ma5, ma10, ma20, ma30, ma60 | 简单移动均线 |
| ema5, ema10, ema20, ema30, ema60 | 指数移动均线 |

### 技术指标

| 列名 | 说明 |
|------|------|
| macd_dif | MACD DIF 线 |
| macd_dea | MACD DEA 线 |
| macd_hist | MACD 柱状 |
| boll_upper, boll_lower | 布林带上/下轨 |
| kdj_k, kdj_d, kdj_j | KDJ 指标 |
| rsi_6, rsi_14, rsi_24 | RSI 相对强弱 |
| atr_14 | 平均真实波幅 |

### 量能

| 列名 | 说明 |
|------|------|
| volume | 成交量 |
| vol_ma5, vol_ma10 | 成交量均线 |
| vol_ratio_5d | 5日量比 |
| turnover_rate | 换手率 |

### 动量与波动

| 列名 | 说明 |
|------|------|
| momentum_5d / 10d / 20d / 30d / 60d | N日涨幅 |
| annual_vol_20d | 20日年化波动率 |
| high_60d, low_60d | 60日最高/最低价 |

### 涨跌停

| 列名 | 说明 |
|------|------|
| consecutive_limit_ups | 连续涨停天数 |
| consecutive_limit_downs | 连续跌停天数 |

## 4. 常用信号列（参考）

信号列是布尔值，使用时用 `.fill_null(False)` 处理空值。同样仅供参考，不要求必须使用。

信号列是布尔值，**必须**使用 `.fill_null(False)` 处理空值。

| 列名 | 方向 | 说明 |
|------|------|------|
| signal_ma_golden_5_20 | 买入 | MA5 上穿 MA20 |
| signal_ma_dead_5_20 | 卖出 | MA5 下穿 MA20 |
| signal_ma_golden_20_60 | 买入 | MA20 上穿 MA60 |
| signal_macd_golden | 买入 | MACD 金叉 |
| signal_macd_dead | 卖出 | MACD 死叉 |
| signal_ma20_breakout | 买入 | 突破 MA20 |
| signal_ma20_breakdown | 卖出 | 跌破 MA20 |
| signal_n_day_high | 买入 | 60日新高 |
| signal_n_day_low | 卖出 | 60日新低 |
| signal_boll_breakout_upper | 中性 | 突破布林上轨 |
| signal_boll_breakdown_lower | 中性 | 跌破布林下轨 |
| signal_volume_surge | 中性 | 放量 |
| signal_limit_up | 买入 | 涨停 |
| signal_limit_down | 卖出 | 跌停 |
| signal_limit_down_recovery | 买入 | 跌停翘板 |

## 5. 规则

1. `filter()` 必须返回 `pl.Expr` (用 `&` `|` 组合布尔表达式)；`filter_history()` 返回筛选后的 `DataFrame`
2. 信号列使用 `.fill_null(False)` 处理空值
3. 用户可能调节的数值阈值通过 `params` 暴露；公式常数、固定窗口边界、一次性内部变量不必强行参数化
4. `scoring` 权重总和必须为 1.0
5. 遵循 A 股 T+1 规则 (当日买入次日才能卖出)
6. 只允许 `import polars as pl`，禁止 import 其他模块
7. 禁止使用 `open()`, `exec()`, `eval()`, `os`, `sys`, `subprocess`
8. **贴合用户需求优先**：第3/4节的指标列和信号列仅供参考，能用则用；如果用户需求需要自定义计算（如"前高""上次涨停价""N日内某个事件后X天"），直接在 `filter_history()` 中自行设计和计算，不需要局限于已有列
9. `filter_history()` 中优先用 Polars 向量化语法；仅在复杂状态机无法清晰表达时，才用 `partition_by("symbol")` 逐股票分析

## 6. 策略示例

### 强势反包

```python
"""强势反包 — 前日阴线下跌 + 今日放量阳线反包"""
import polars as pl

META = {
    "id": "strong_reversal",
    "name": "强势反包",
    "description": "前一日明显阴线下跌，今日放量阳线收复前一日阴线实体",
    "tags": ["反包", "短线", "放量"],
    "basic_filter": {
        "price_min": 3, "price_max": 200,
        "market_cap_min": 10e8, "amount_min": 0.5e8,
        "exclude_st": True, "exclude_new_days": 30,
    },
    "params": [
        {"id": "prev_down_pct", "label": "前日最大跌幅", "type": "float",
         "default": -0.02, "min": -0.10, "max": -0.005, "step": 0.005},
        {"id": "volume_ratio", "label": "成交量放大倍数", "type": "float",
         "default": 1.2, "min": 1.0, "max": 5.0, "step": 0.1},
        {"id": "reversal_tolerance", "label": "反包容忍误差", "type": "float",
         "default": 0.005, "min": 0.0, "max": 0.03, "step": 0.005},
    ],
    "scoring": {"change_pct": 0.4, "vol_ratio_5d": 0.3, "momentum_5d": 0.3},
    "order_by": "score", "descending": True, "limit": 100,
}

LOOKBACK_DAYS = 2

ENTRY_SIGNALS = ["signal_broken_board_recovery"]
EXIT_SIGNALS = ["signal_ma20_breakdown"]
STOP_LOSS = -0.05
MAX_HOLD_DAYS = 10
ALERTS = [{"field": "signal_broken_board_recovery", "message": "反包信号"}]

RULES = """
1. 前一交易日为阴线，且跌幅不小于设定阈值
2. 今日为阳线，收盘价收复前一日开盘价并接近或突破前一日高点
3. 今日成交量较前一日明显放大，且收盘价站上 MA5 或 MA10
"""

def filter_history(df: pl.DataFrame, params: dict) -> pl.DataFrame:
    if df.is_empty() or "date" not in df.columns:
        return df

    down_pct = float(params.get("prev_down_pct", -0.02))
    vol_ratio = float(params.get("volume_ratio", 1.2))
    tolerance = float(params.get("reversal_tolerance", 0.005))
    latest = df["date"].max()
    hist = (
        df.sort(["symbol", "date"])
        .with_columns([
            pl.col("open").shift(1).over("symbol").alias("_prev_open"),
            pl.col("high").shift(1).over("symbol").alias("_prev_high"),
            pl.col("close").shift(1).over("symbol").alias("_prev_close"),
            pl.col("volume").shift(1).over("symbol").alias("_prev_volume"),
            pl.col("change_pct").shift(1).over("symbol").alias("_prev_change_pct"),
        ])
    )
    return hist.filter(pl.col("date") == latest).filter(
        (pl.col("_prev_close") < pl.col("_prev_open"))
        & (pl.col("_prev_change_pct") <= down_pct)
        & (pl.col("close") > pl.col("open"))
        & (pl.col("close") > pl.col("_prev_open"))
        & (pl.col("close") >= pl.col("_prev_high") * (1 - tolerance))
        & (pl.col("volume") >= pl.col("_prev_volume") * vol_ratio)
        & ((pl.col("close") > pl.col("ma5")) | (pl.col("close") > pl.col("ma10")))
    )
```

## 7. 完整示例

见 [strategy-example.md](./strategy-example.md) — 从零创建强势反包策略的三步完整演示。
