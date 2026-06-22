"""策略实时监控 — 订阅行情更新，检查策略买卖信号和提醒条件。

职责: 接收实时行情 DataFrame → 检查监控中策略的信号/提醒 → 推送告警。
不知道: 策略加载逻辑、AI、API、配置持久化、回测。
依赖: 外部调用 on_quote_update() 传入实时数据。

本模块含两个评估器:
  1. StrategyMonitorService — 旧的策略监控 (type=strategy),第二步迁移到 MonitorRuleEngine
  2. MonitorRuleEngine — 通用规则引擎,覆盖 signal/price/market/strategy 四类,
     支持 scope (symbols/all/sector) + 多条件 AND/OR + cooldown 去重
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import polars as pl

from app.strategy.custom_signals import _OP_BUILDERS  # type: ignore  # 复用运算符构造器

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


# ================================================================
# 通用监控规则引擎 MonitorRuleEngine
# ================================================================

_SIGNAL_PREFIXES = ("signal_", "csg_")


def _is_signal_field(field: str) -> bool:
    return any(field.startswith(p) for p in _SIGNAL_PREFIXES)


def _build_condition_mask(df: pl.DataFrame, conditions: list[dict], logic: str) -> pl.DataFrame:
    """根据 conditions + logic 构建过滤后的命中 DataFrame。

    conditions: [{"field","op","value"?}] — op=truth 为布尔信号, 否则阈值比较
    logic: "and" | "or"
    返回命中行 (含 symbol/name/close/change_pct + 各信号列)
    """
    cols = set(df.columns)
    parts: list[pl.Expr] = []
    for c in conditions:
        field = c["field"]
        if field not in cols:
            return df.head(0)  # 字段缺失,无法判定 → 空结果
        op = c["op"]
        if op == "truth":
            parts.append(pl.col(field).fill_null(False))
        elif op in _OP_BUILDERS:
            parts.append(_OP_BUILDERS[op](pl.col(field), c["value"]))
        else:
            return df.head(0)
    if not parts:
        return df.head(0)
    if logic == "or":
        mask = pl.any_horizontal(parts)
    else:
        mask = pl.all_horizontal(parts)
    return df.filter(mask)


class MonitorRuleEngine:
    """通用监控规则引擎 — 接收实时行情 DataFrame,评估所有规则,返回 AlertEvent。

    与 StrategyMonitorService 的区别:
      - 规则来自 monitor_rules 存储 (用户可配), 而非写死的 strategy config
      - 支持 scope (symbols/all/sector) 过滤作用域
      - 支持 conditions + logic (AND/OR) 任意组合
      - ★ cooldown 去重: 同一 (rule_id, symbol) 在冷却期内不重复触发
    """

    def __init__(self, alert_handler: Callable[[dict], None] | None = None):
        self._alert_handler = alert_handler
        self._rules: dict[str, dict] = {}  # rule_id → rule
        # (rule_id, symbol) → 上次触发时间戳(秒)。用于 cooldown 去重。
        self._last_fire: dict[tuple[str, str], float] = {}
        self._strategy_engine = None  # 延迟注入, type=strategy 规则用它读策略信号
        # symbol → 股票名 (enriched DataFrame 已 drop name 列, 触发时从此映射回填)
        self._name_map: dict[str, str] = {}

    def set_strategy_engine(self, engine) -> None:
        """注入 StrategyEngine, type=strategy 规则据此读策略的 entry/exit_signals。"""
        self._strategy_engine = engine

    def set_name_map(self, name_map: dict[str, str]) -> None:
        """注入 symbol → 股票名 映射, 用于在告警事件里回填 name 字段。

        enriched DataFrame 在 pipeline 计算后不含 name 列 (见 indicators/pipeline.py),
        触发时从 instruments 表预构建此映射, 保证 AlertEvent.name 有值。
        """
        self._name_map = name_map or {}

    # ── 规则管理 ───────────────────────────────────────
    def set_rules(self, rules: list[dict]) -> None:
        """批量设置规则 (覆盖)。用于启动时 reload。"""
        self._rules = {}
        for r in rules:
            if r.get("enabled") is not False:
                self._rules[r["id"]] = r
        logger.info("MonitorRuleEngine: 装载 %d 条规则", len(self._rules))

    def add_rule(self, rule: dict) -> None:
        if rule.get("enabled") is not False:
            self._rules[rule["id"]] = rule
        else:
            self._rules.pop(rule["id"], None)

    def remove_rule(self, rule_id: str) -> None:
        self._rules.pop(rule_id, None)
        # 清理对应的 cooldown 记录
        self._last_fire = {k: v for k, v in self._last_fire.items() if k[0] != rule_id}

    def clear(self) -> None:
        self._rules.clear()
        self._last_fire.clear()

    @property
    def rules(self) -> dict[str, dict]:
        return dict(self._rules)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    # ── 评估 ───────────────────────────────────────────
    def evaluate(self, df: pl.DataFrame) -> list[dict]:
        """行情更新后评估所有规则。

        Args:
            df: 实时 enriched 数据 (~5500行, 含 signal_/csg_/指标列)
        Returns:
            触发的 AlertEvent dict 列表 (含 ts/rule_id/source/type/symbol/...)
        """
        if not self._rules or df.is_empty():
            return []

        now = time.time()
        events: list[dict] = []

        for rule_id, rule in self._rules.items():
            try:
                events.extend(self._evaluate_rule(df, rule, now))
            except Exception as e:
                logger.warning("规则评估失败 %s: %s", rule_id, e)

        return events

    def _evaluate_rule(self, df: pl.DataFrame, rule: dict, now: float) -> list[dict]:
        """评估单条规则,返回触发的 events。"""
        # 1. 按 scope 过滤作用域
        scoped = self._apply_scope(df, rule)
        if scoped.is_empty():
            return []

        # 2. 根据 type 构建命中集
        hit_rows: list[tuple[str, Any, Any, Any, list[str]]] = []  # (symbol,name,price,pct,signals)

        rtype = rule.get("type", "signal")
        if rtype == "strategy":
            # 策略类型: 从 StrategyEngine 读策略的 entry/exit_signals, 按 direction 评估
            hit_rows = self._match_strategy(scoped, rule)
        else:
            # signal / price / market: 通用条件匹配
            hit_rows = self._match_conditions(scoped, rule)

        if not hit_rows:
            return []

        # 3. cooldown 去重 + 生成 events
        cooldown = rule.get("cooldown_seconds", 3600)
        severity = rule.get("severity", "info")
        message = rule.get("message", "") or self._default_message(rule)
        source = rtype if rtype != "strategy" else "strategy"
        ev_type = rule.get("direction", "entry") if rtype == "strategy" else rtype

        events: list[dict] = []
        for sym, name, price, pct, hit_sigs in hit_rows:
            key = (rule["id"], sym)
            last = self._last_fire.get(key)
            if last is not None and (now - last) < cooldown:
                continue  # 冷却期内, 跳过
            self._last_fire[key] = now
            # enriched DataFrame 已 drop name 列 → 从注入的 name_map 回填 (instruments 表)
            resolved_name = name if name else self._name_map.get(sym)
            ev = {
                "ts": int(now * 1000),
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "source": source,
                "type": ev_type,
                "symbol": sym,
                "name": resolved_name,
                "message": message,
                "price": price,
                "change_pct": pct,
                "signals": hit_sigs,
                "severity": severity,
            }
            events.append(ev)
            if self._alert_handler:
                try:
                    self._alert_handler(ev)
                except Exception as e:
                    logger.warning("alert handler failed: %s", e)

        return events

    @staticmethod
    def _apply_scope(df: pl.DataFrame, rule: dict) -> pl.DataFrame:
        """按 scope 过滤 DataFrame。"""
        scope = rule.get("scope", "symbols")
        if scope == "all":
            return df
        if scope == "symbols":
            syms = rule.get("symbols", [])
            if not syms:
                return df.head(0)
            return df.filter(pl.col("symbol").is_in(syms))
        if scope == "sector":
            # sector 过滤: 需 df 含板块列 (后续接入 ext_data JOIN)
            # 当前先返回全量, sector 精确过滤第二步完善
            return df
        return df

    def _match_strategy(
        self, df: pl.DataFrame, rule: dict,
    ) -> list[tuple[str, Any, Any, Any, list[str]]]:
        """策略类型评估: 从 StrategyEngine 读策略信号, 按 direction 用 OR 匹配。

        direction=entry → 策略 entry_signals
        direction=exit  → 策略 exit_signals
        direction=both  → entry + exit 合并 (命中信号名区分来源)
        """
        if self._strategy_engine is None:
            return []
        sid = rule.get("strategy_id")
        if not sid:
            return []
        try:
            s = self._strategy_engine.get(sid)
        except Exception:
            return []
        if s is None:
            return []

        direction = rule.get("direction", "entry")
        # 收集要评估的信号 (OR 组合), 与旧 StrategyMonitorService 行为一致
        sigs: list[str] = []
        if direction in ("entry", "both"):
            sigs.extend(s.entry_signals or [])
        if direction in ("exit", "both"):
            sigs.extend(s.exit_signals or [])
        if not sigs:
            return []

        # 复用旧的 _check_signals 静态方法 (已支持 signal_/csg_ 前缀)
        return StrategyMonitorService._check_signals(df, sigs)

    @staticmethod
    def _match_conditions(
        df: pl.DataFrame, rule: dict,
    ) -> list[tuple[str, Any, Any, Any, list[str]]]:
        """按 conditions + logic 匹配,返回命中行 [(symbol,name,price,pct,signals)]。"""
        conditions = rule.get("conditions", [])
        logic = rule.get("logic", "and")
        if not conditions:
            return []
        hit_df = _build_condition_mask(df, conditions, logic)
        results = []
        for row in hit_df.iter_rows(named=True):
            sym = row.get("symbol", "")
            name = row.get("name")
            price = row.get("close")
            pct = row.get("change_pct")
            # 收集命中的信号列名 (仅 op=truth 且为真的)
            hit_sigs = [
                c["field"] for c in conditions
                if c.get("op") == "truth" and row.get(c["field"])
            ]
            results.append((sym, name, price, pct, hit_sigs))
        return results

    def _default_message(self, rule: dict) -> str:
        """生成默认 message。策略类型带策略名 + 方向 (对齐 demo 模板格式, 让前端可高亮)。"""
        rtype = rule.get("type", "signal")
        if rtype == "strategy":
            direction = rule.get("direction", "entry")
            action = {"entry": "买入", "exit": "卖出", "both": "买卖"}.get(direction, "买入")
            # 从 StrategyEngine 取策略名; 失败则退化为 rule_name 里截取的部分
            sname = ""
            sid = rule.get("strategy_id")
            if sid and self._strategy_engine is not None:
                try:
                    s = self._strategy_engine.get(sid)
                    sname = s.meta.get("name", "") or s.meta.get("id", "")
                except Exception:  # noqa: BLE001
                    sname = ""
            if not sname:
                rn = rule.get("name", "")
                sname = rn.split(" · ", 1)[1] if " · " in rn else (rn or "策略")
            return f"策略「{sname}」{action}信号"
        name_map = {"signal": "信号触发", "price": "价格触发", "market": "市场异动"}
        return name_map.get(rtype, "监控触发")
