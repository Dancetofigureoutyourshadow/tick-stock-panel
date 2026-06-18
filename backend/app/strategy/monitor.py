"""策略实时监控 — 订阅行情更新，检查策略买卖信号和提醒条件。

职责: 接收实时行情 DataFrame → 检查监控中策略的信号/提醒 → 推送告警。
不知道: 策略加载逻辑、AI、API、配置持久化、回测。
依赖: 外部调用 on_quote_update() 传入实时数据。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import polars as pl

logger = logging.getLogger(__name__)


@dataclass
class StrategyAlert:
    """策略告警"""
    type: str              # "entry" | "exit" | "alert"
    strategy_id: str
    symbol: str
    name: str | None
    message: str
    price: float | None = None
    change_pct: float | None = None
    signals: list[str] = field(default_factory=list)


class StrategyMonitorService:
    """策略实时监控服务"""

    def __init__(self, alert_handler: Callable[[StrategyAlert], None] | None = None):
        """
        Args:
            alert_handler: 告警回调 (如推 SSE)
        """
        self._alert_handler = alert_handler
        # strategy_id → 监控配置
        self._watching: dict[str, dict] = {}

    def start(self, strategy_id: str, config: dict) -> None:
        """开始监控一个策略

        config: {
            "entry_signals": ["signal_n_day_high", ...],
            "exit_signals": ["signal_ma20_breakdown", ...],
            "alerts": [{"field": "rsi_14", "op": ">", "value": 80, "message": "..."}],
        }
        """
        self._watching[strategy_id] = config
        logger.info("strategy monitor started: %s", strategy_id)

    def stop(self, strategy_id: str) -> None:
        self._watching.pop(strategy_id, None)
        logger.info("strategy monitor stopped: %s", strategy_id)

    def stop_all(self) -> None:
        self._watching.clear()

    @property
    def watching(self) -> dict[str, dict]:
        return dict(self._watching)

    def on_quote_update(self, df: pl.DataFrame) -> list[StrategyAlert]:
        """行情更新后调用。向量化检查所有监控策略。

        Args:
            df: 实时 enriched 数据 (~5500行)
        Returns:
            触发的告警列表
        """
        if not self._watching or df.is_empty():
            return []

        all_alerts: list[StrategyAlert] = []

        for strategy_id, cfg in self._watching.items():
            # 买入信号
            entry_sigs = cfg.get("entry_signals", [])
            if entry_sigs:
                for sym, name, price, pct, hit_sigs in self._check_signals(df, entry_sigs):
                    alert = StrategyAlert(
                        type="entry",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=f"买入信号触发",
                        price=price,
                        change_pct=pct,
                        signals=hit_sigs,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

            # 卖出信号
            exit_sigs = cfg.get("exit_signals", [])
            if exit_sigs:
                for sym, name, price, pct, hit_sigs in self._check_signals(df, exit_sigs):
                    alert = StrategyAlert(
                        type="exit",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=f"卖出信号触发",
                        price=price,
                        change_pct=pct,
                        signals=hit_sigs,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

            # 提醒条件
            for alert_cfg in cfg.get("alerts", []):
                for sym, name, price, pct in self._check_alert(df, alert_cfg):
                    alert = StrategyAlert(
                        type="alert",
                        strategy_id=strategy_id,
                        symbol=sym,
                        name=name,
                        message=alert_cfg.get("message", "提醒"),
                        price=price,
                        change_pct=pct,
                    )
                    all_alerts.append(alert)
                    self._emit(alert)

        return all_alerts

    def _emit(self, alert: StrategyAlert) -> None:
        if self._alert_handler:
            try:
                self._alert_handler(alert)
            except Exception as e:
                logger.warning("alert handler failed: %s", e)

    @staticmethod
    def _check_signals(
        df: pl.DataFrame,
        signals: list[str],
    ) -> list[tuple[str, str | None, float | None, float | None, list[str]]]:
        """检查信号列，返回 [(symbol, name, price, change_pct, [hit_signals])]。
        支持内置 signal_ 与自定义 csg_ 前缀。"""
        cols = set(df.columns)
        resolved: list[tuple[str, str]] = []  # (原值, 列名)
        for s in signals:
            col = s if (s.startswith("signal_") or s.startswith("csg_")) else f"signal_{s}"
            if col in cols:
                resolved.append((s, col))
        if not resolved:
            return []

        mask = pl.any_horizontal(pl.col(c).fill_null(False) for _, c in resolved)
        hit_df = df.filter(mask)

        results = []
        for row in hit_df.iter_rows(named=True):
            sym = row.get("symbol", "")
            name = row.get("name")
            price = row.get("close")
            pct = row.get("change_pct")
            hit_sigs = [orig for orig, col in resolved if row.get(col)]
            results.append((sym, name, price, pct, hit_sigs))
        return results

    @staticmethod
    def _check_alert(
        df: pl.DataFrame,
        alert: dict,
    ) -> list[tuple[str, str | None, float | None, float | None]]:
        """检查阈值型提醒条件"""
        field = alert.get("field", "")
        if field not in df.columns:
            return []

        if "op" in alert:
            # 阈值比较
            op = alert["op"]
            value = alert["value"]
            col = pl.col(field)
            ops = {
                ">": col > value,
                ">=": col >= value,
                "<": col < value,
                "<=": col <= value,
            }
            expr = ops.get(op)
            if expr is None:
                return []
        else:
            # 信号列 (布尔)
            expr = pl.col(field).fill_null(False)

        hit_df = df.filter(expr)
        results = []
        for row in hit_df.iter_rows(named=True):
            results.append((
                row.get("symbol", ""),
                row.get("name"),
                row.get("close"),
                row.get("change_pct"),
            ))
        return results
