from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import MatrixComputeCache, build_market_data_matrix
from app.backtest.regime_alignment import build_regime_scale_series
from app.strategy.engine import StrategyEngine

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path):
    return StrategyEngine._load_file(path)


def _param_default(strategy, param_id: str):
    return next(item["default"] for item in strategy.meta["params"] if item["id"] == param_id)


def _rows(*, break_price: float | None = None, extension_price: float | None = None, break_volume: float | None = None):
    rows = []
    for index in range(100):
        if index < 40:
            close = 100 + index * 2
        elif index < 55:
            close = 178 + ((index % 3) - 1)
        elif index == 55 and break_price is not None:
            close = break_price
        elif index == 55 and extension_price is not None:
            close = extension_price
        else:
            close = 220
        rows.append(
            {
                "symbol": "000001.SZ",
                "name": "A",
                "date": date(2020, 1, 1) + timedelta(days=index),
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": break_volume if index == 55 and break_volume is not None else (100.0 if index < 40 else 20.0),
                "amount": 1e8,
                "turnover_rate": 1.0,
            }
        )
    return pl.DataFrame(rows)


def _signals(strategy, panel: pl.DataFrame, **overrides):
    market = build_market_data_matrix(
        panel,
        field_columns={"amount", "turnover_rate"},
    )
    params = {item["id"]: item["default"] for item in strategy.meta["params"]}
    params.update(overrides)
    return strategy.matrix_strategy.compute_signals(market, params)


def test_v2_and_v3_load_with_stable_ids_and_defaults():
    v2 = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v2.py")
    v3 = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")

    assert v2.meta["id"] == "custom_sequoia_high_tight_flag_v2"
    assert v3.meta["id"] == "custom_sequoia_high_tight_flag_v3"
    assert _param_default(v2, "exit_on_flag_break") is False
    assert _param_default(v3, "exit_on_flag_break") is False
    assert _param_default(v3, "entry_mode") == "flag_lurk"
    assert v3.execution_backend == "matrix_native"
    assert v3.max_hold_days == 40
    assert _param_default(v3, "flag_break_buffer") == 0.03
    assert _param_default(v3, "min_momentum") == 0.45
    assert _param_default(v3, "max_volume_ratio") == 0.70
    assert _param_default(v3, "ext_max") == 1.10
    assert _param_default(v3, "turnover_max") == 8.0
    assert _param_default(v3, "w_turnover") == 0.30
    assert _param_default(v3, "w_vol") == 0.50
    assert _param_default(v3, "exit_on_volume_ma_break") is True
    assert _param_default(v3, "ma_exit_volume_ratio") == 1.50
    assert _param_default(v3, "ma_exit_min_extension") == 1.05
    assert _param_default(v3, "ma_exit_buffer") == 0.02
    assert v3.meta["regime_position_pct"] is None


def test_v3_keeps_v2_entry_and_score_path_identical():
    v2 = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v2.py")
    v3 = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")
    params = {
        "momentum_window": 40,
        "min_momentum": 0.45,
        "flag_window": 10,
        "max_flag_range": 0.15,
        "flag_floor": 0.80,
        "max_volume_ratio": 0.70,
        "trend_window": 20,
        "ext_max": 1.10,
        "ext_exit": 1.35,
        "turnover_max": 8.0,
        "exit_on_flag_break": False,
        "flag_break_buffer": 0.03,
        "w_amount": 0.10,
        "w_turnover": 0.30,
        "w_vol": 0.50,
        "w_tightness": 0.10,
    }
    panel = _rows(break_price=150)
    v2_signals = _signals(v2, panel, **params)
    v3_signals = _signals(
        v3,
        panel,
        **params,
        exit_on_volume_ma_break=False,
        exit_on_trend_break=False,
    )
    np.testing.assert_array_equal(v2_signals.entry, v3_signals.entry)
    np.testing.assert_allclose(v2_signals.score, v3_signals.score)


def test_v3_exit_codes_distinguish_flag_break_and_extension():
    strategy = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")

    flag_break = _signals(
        strategy,
        _rows(break_price=150),
        exit_on_flag_break=True,
        flag_break_buffer=0.03,
    )
    extension = _signals(strategy, _rows(extension_price=260))
    trend_break = _signals(
        strategy,
        _rows(break_price=150),
        exit_on_flag_break=False,
        exit_on_trend_break=True,
        trend_break_buffer=0.0,
    )

    assert flag_break.exit_signal_ids == (
        "signal_sequoia_high_tight_flag_v3_extension_exit",
        "signal_sequoia_high_tight_flag_v3_flag_break_exit",
        "signal_sequoia_high_tight_flag_v3_trend_break_exit",
        "signal_sequoia_high_tight_flag_v3_volume_ma_break_exit",
    )
    assert flag_break.exit_signal_code[55, 0] == 1
    assert extension.exit_signal_code[55, 0] == 0
    assert trend_break.exit_signal_code[55, 0] == 2
    assert flag_break.exit[55, 0] == 1
    assert extension.exit[55, 0] == 1
    assert trend_break.exit[55, 0] == 1


def test_v3_volume_ma_break_requires_price_trend_and_volume_confirmation():
    strategy = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")
    signals = _signals(
        strategy,
        _rows(break_price=150, break_volume=100.0),
        exit_on_flag_break=False,
        exit_on_volume_ma_break=True,
        ma_exit_volume_ratio=1.5,
        ma_exit_min_extension=1.0,
        ma_exit_buffer=0.0,
    )
    assert signals.exit_signal_code[55, 0] == 3
    assert signals.exit[55, 0] == 1


def test_v3_signals_do_not_change_when_only_future_bars_change():
    strategy = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")
    baseline = _rows(break_price=150)
    future = baseline.with_columns(
        pl.when(pl.arange(0, pl.len()) >= 56)
        .then(pl.col("close") * 10)
        .otherwise(pl.col("close"))
        .alias("close")
    ).with_columns(
        pl.col("close").alias("open"),
        (pl.col("close") * 1.01).alias("high"),
        (pl.col("close") * 0.99).alias("low"),
    )

    first = _signals(strategy, baseline)
    second = _signals(strategy, future)
    np.testing.assert_array_equal(first.entry[:56], second.entry[:56])
    np.testing.assert_array_equal(first.exit[:56], second.exit[:56])
    np.testing.assert_array_equal(
        first.exit_signal_code[:56], second.exit_signal_code[:56]
    )


def test_v3_parameter_changes_are_not_reused_by_matrix_compute_cache():
    strategy = _load(ROOT / "generated" / "custom_sequoia_high_tight_flag_v3.py")
    market = build_market_data_matrix(
        _rows(),
        field_columns={"amount", "turnover_rate"},
    )
    params = {item["id"]: item["default"] for item in strategy.meta["params"]}
    shorter = {**params, "momentum_window": 20}
    cache = MatrixComputeCache(max_bytes=64 * 1024 * 1024)

    with cache.activate(market):
        default_signals = strategy.matrix_strategy.compute_signals(market, params)
        shorter_signals = strategy.matrix_strategy.compute_signals(market, shorter)

    assert not np.array_equal(default_signals.entry, shorter_signals.entry)
    rolling_max = cache.snapshot()["operations"]["valid_rolling_max"]
    assert rolling_max["misses"] >= 2


def test_regime_position_ladder_is_t_minus_one_and_can_go_flat():
    labels = ("2024-01-02", "2024-01-03", "2024-01-04")
    history = {
        "2024-01-02": {"state": "weak", "score": 10},
        "2024-01-03": {"state": "lean_weak", "score": 30},
    }

    scale = build_regime_scale_series(
        labels,
        {"weak": 0.0, "lean_weak": 0.5},
        history,
    )

    np.testing.assert_allclose(scale, [1.0, 0.0, 0.5])
