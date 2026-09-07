"""五级环境仓位缩放 (regime_position_pct → regime_scale) 的回归与行为测试。

回归保护是这组测试的核心: 五级全填 1.0 时结果必须与完全不传该字段**逐位相同**,
否则这项引擎改动会让上一轮所有基线结论失效。IEEE754 下 ``x * 1.0 == x``, 蒙特卡洛
已固定 ``default_rng(42)``, 所以除 ``stats["statistics_ms"]`` (纯计时) 之外应完全一致。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import build_market_matrix
from app.backtest.regime_alignment import (
    build_regime_scale_series,
    normalize_regime_position_pct,
)


def _panel(
    symbols: list[str],
    days: int = 4,
    price: float = 10.0,
    overrides: dict[tuple[str, int], dict] | None = None,
) -> pl.DataFrame:
    overrides = overrides or {}
    start = date(2024, 1, 1)
    rows = []
    for sym in symbols:
        for i in range(days):
            patch = overrides.get((sym, i), {})
            rows.append({
                "symbol": sym,
                "name": sym,
                "date": start + timedelta(days=i),
                "open": patch.get("open", price),
                "high": patch.get("high", price),
                "low": patch.get("low", price),
                "close": patch.get("close", price),
                "volume": patch.get("volume", 100_000),
                "score": patch.get("score", {"A": 4, "B": 3, "C": 2, "D": 1}.get(sym, 0)),
                "signal_limit_up": patch.get("signal_limit_up", False),
                "signal_limit_down": patch.get("signal_limit_down", False),
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


def _mask(panel: pl.DataFrame, marks: set[tuple[str, int]]) -> pl.Series:
    values = []
    base = date(2024, 1, 1)
    for row in panel.select(["symbol", "date"]).iter_rows(named=True):
        day = (row["date"] - base).days
        values.append((row["symbol"], day) in marks)
    return pl.Series(values, dtype=pl.Boolean)


def _engine() -> BacktestEngine:
    return BacktestEngine(repo=None)  # 组合撮合不访问 repo


def _matrix(panel: pl.DataFrame, entries: pl.Series, exits: pl.Series, config: MatcherConfig):
    """与 ``simulate_portfolio`` 内部完全一致的建矩阵口径。

    ``regime_scale`` 只有 ``simulate_market_matrix`` 这条入口能传, 所以测试自己建矩阵。
    """
    return build_market_matrix(
        panel,
        entries,
        exits,
        entry_delay_bars=1 if config.entry_fill == "open_t+1" else 0,
        exit_delay_bars=1 if config.exit_fill == "open_t+1" else 0,
        minute_exit_trigger=config.exit_fill == "signal_next_minute",
    )


def _comparable(result) -> tuple:
    """剔除纯计时字段后的可比结果 —— 其余一切都要求逐位相同。"""
    stats = {key: value for key, value in result.stats.items() if key != "statistics_ms"}
    return (
        result.equity_curve,
        result.drawdown_curve,
        result.trades,
        result.per_symbol_stats,
        stats,
    )


def _regression_case() -> tuple[pl.DataFrame, pl.Series, pl.Series, MatcherConfig]:
    """一个会产生多种退出原因的非退化算例 (止损 + 到期), 用来做逐位比对。"""
    panel = _panel(
        ["A", "B", "C", "D"],
        days=6,
        overrides={
            ("A", 2): {"open": 10.2, "high": 10.8, "low": 10.1, "close": 10.7},
            ("A", 3): {"open": 10.7, "high": 11.2, "low": 10.5, "close": 11.0},
            ("A", 4): {"open": 11.0, "high": 11.3, "low": 10.8, "close": 11.1},
            ("B", 3): {"open": 10.0, "high": 10.1, "low": 9.7, "close": 9.8},
            ("C", 2): {"open": 9.9, "high": 10.0, "low": 9.6, "close": 9.7},
            # D 在第 3 根 bar 盘中跌破 -10% 止损线 (入场 10.0 → 止损线 9.0)
            ("D", 2): {"open": 9.5, "high": 9.6, "low": 8.5, "close": 8.8},
            ("D", 3): {"open": 8.8, "high": 9.0, "low": 8.6, "close": 8.9},
        },
    )
    config = MatcherConfig(
        matching="open_t+1",
        max_positions=4,
        max_exposure_pct=0.8,
        max_hold_days=3,
        stop_loss_pct=0.1,
        initial_capital=100_000,
    )
    return panel, _mask(panel, {("A", 0), ("B", 0), ("C", 0), ("D", 0)}), _mask(panel, set()), config


def test_all_ones_ladder_is_bit_for_bit_identical_to_no_ladder():
    """五级全 1.0 ⇔ 不传该字段: 逐位相同。这条挂了就说明既有基线结论全部作废。"""
    panel, entries, exits, config = _regression_case()
    matrix = _matrix(panel, entries, exits, config)
    engine = _engine()

    baseline = engine.simulate_market_matrix(matrix, config)
    scaled = engine.simulate_market_matrix(
        matrix, config, regime_scale=np.ones(len(matrix.timestamp_labels)),
    )

    # 先确认算例本身不是空跑, 否则「逐位相同」毫无意义
    assert len(baseline.trades) == 4
    assert {t.exit_reason for t in baseline.trades} == {"stop_loss", "max_hold"}
    assert all(t.exit_reason != "regime_reduce" for t in scaled.trades)

    assert _comparable(scaled) == _comparable(baseline)


def test_zero_ladder_opens_no_new_position():
    """弱市档位 0 → 当日敞口上限 0, 一笔新仓都不开, 且计入 buy_exposure 而非静默丢弃。"""
    panel = _panel(["A", "B", "C"], days=4)
    entries = _mask(panel, {("A", 0), ("B", 0), ("C", 0)})
    exits = _mask(panel, set())
    config = MatcherConfig(
        matching="open_t+1",
        max_positions=3,
        max_exposure_pct=0.9,
        initial_capital=100_000,
    )
    matrix = _matrix(panel, entries, exits, config)

    # 先验证同一算例在不缩放时确实会开仓, 排除「因为别的原因没成交」
    unscaled = _engine().simulate_market_matrix(matrix, config)
    assert len(unscaled.trades) == 3

    result = _engine().simulate_market_matrix(
        matrix, config, regime_scale=np.zeros(len(matrix.timestamp_labels)),
    )

    assert result.trades == []
    assert result.stats["execution"]["buy_exposure"] == 3


def test_downgrade_reduces_lowest_scored_positions_first_as_regime_reduce():
    """档位下调到装不下当前持仓时: 按 entry_score 从低到高减仓, 退出原因 regime_reduce。"""
    panel = _panel(["A", "B", "C"], days=6)  # score: A=4 > B=3 > C=2
    entries = _mask(panel, {("A", 0), ("B", 0), ("C", 0)})
    exits = _mask(panel, set())
    config = MatcherConfig(
        matching="open_t+1",
        max_positions=3,
        max_exposure_pct=0.9,
        initial_capital=100_000,
    )
    matrix = _matrix(panel, entries, exits, config)

    # 对照组: 不缩放时三笔都持到末日, 不存在 regime_reduce
    unscaled = _engine().simulate_market_matrix(matrix, config)
    assert [t.exit_reason for t in unscaled.trades] == ["end"] * 3

    # 第 4 根 bar 档位降到 0.4 → 当日敞口上限 0.9 × 0.4 = 0.36, 装不下 ~0.87 的持仓
    scale = np.ones(len(matrix.timestamp_labels))
    scale[3] = 0.4
    result = _engine().simulate_market_matrix(matrix, config, regime_scale=scale)

    assert len(result.trades) == 3
    # 卖出顺序即打分从低到高: 先 C 再 B, 打分最高的 A 保留到末日
    assert [t.symbol for t in result.trades] == ["C", "B", "A"]
    assert [t.exit_reason for t in result.trades] == ["regime_reduce", "regime_reduce", "end"]
    assert {t.exit_date for t in result.trades if t.exit_reason == "regime_reduce"} == {"2024-01-04"}
    # 减仓不能污染信号腿的统计
    assert all(t.exit_signal_id is None for t in result.trades)


def test_regime_scale_length_mismatch_raises():
    panel, entries, exits, config = _regression_case()
    matrix = _matrix(panel, entries, exits, config)
    with pytest.raises(ValueError, match="regime_scale 长度"):
        _engine().simulate_market_matrix(matrix, config, regime_scale=np.ones(3))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -0.1])
def test_regime_scale_invalid_values_raise(bad):
    panel, entries, exits, config = _regression_case()
    matrix = _matrix(panel, entries, exits, config)
    scale = np.ones(len(matrix.timestamp_labels))
    scale[2] = bad
    with pytest.raises(ValueError, match="非法值"):
        _engine().simulate_market_matrix(matrix, config, regime_scale=scale)


# ── normalize_regime_position_pct: 宁可报错也不静默降级 ──────────────────────────
# 上一轮 regime_position_pct / mainline_priority 被 Pydantic extra='ignore' 静默丢弃,
# 「传了但没生效」的结果被当成有效结论, 这几条就是防止再踩同一个坑。

def test_normalize_regime_position_pct_fills_missing_levels_with_one():
    assert normalize_regime_position_pct({"weak": 0}) == {
        "strong": 1.0,
        "lean_strong": 1.0,
        "range": 1.0,
        "lean_weak": 1.0,
        "weak": 0.0,
    }


def test_normalize_regime_position_pct_returns_none_when_absent():
    assert normalize_regime_position_pct(None) is None
    assert normalize_regime_position_pct({}) is None


@pytest.mark.parametrize(("ladder", "message"), [
    ({"strang": 1.0}, "档位名无效"),
    ({"strong": 1.0, "bear": 0.0}, "档位名无效"),
    ({"weak": "abc"}, "不是数值"),
    ({"weak": 1.5}, "必须在 0~1 之间"),
    ({"weak": -0.1}, "必须在 0~1 之间"),
])
def test_normalize_regime_position_pct_fails_loudly(ladder, message):
    with pytest.raises(ValueError, match=message):
        normalize_regime_position_pct(ladder)


# ── build_regime_scale_series: T-1 对齐, 无未来数据 ─────────────────────────────

_LABELS = ("2024-01-01", "2024-01-02", "2024-01-03")


def test_build_regime_scale_series_uses_t_minus_one_and_keeps_first_day_neutral():
    regime = {
        "2024-01-01": {"state": "weak", "score": 10},
        "2024-01-02": {"state": "strong", "score": 90},
        # 当日环境只会影响下一个 bar, 所以 01-03 的 range 档位在本区间内用不上
        "2024-01-03": {"state": "range", "score": 50},
    }
    scale = build_regime_scale_series(
        _LABELS, {"weak": 0.0, "strong": 1.0, "range": 0.5}, regime,
    )
    assert scale.tolist() == [1.0, 0.0, 1.0]


def test_build_regime_scale_series_returns_none_without_ladder():
    assert build_regime_scale_series(_LABELS, None, {"2024-01-01": ("weak", 0)}) is None


def test_build_regime_scale_series_treats_unknown_state_as_no_scaling():
    regime = {
        "2024-01-01": ("bull", 0),
        "2024-01-02": ("weak", 0),
        "2024-01-03": ("weak", 0),
    }
    scale = build_regime_scale_series(_LABELS, {"weak": 0.25}, regime)
    assert scale.tolist() == [1.0, 1.0, 0.25]


def test_build_regime_scale_series_fails_closed_on_internal_gap():
    with pytest.raises(ValueError, match="市场环境数据覆盖不完整"):
        build_regime_scale_series(_LABELS, {"weak": 0.0}, {"2024-01-01": ("weak", 0)})
