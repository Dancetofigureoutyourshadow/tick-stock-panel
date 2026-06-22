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

import datetime as _dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import polars as pl

from app.strategy.custom_signals import _OP_BUILDERS  # type: ignore  # 复用运算符构造器
from app.strategy import config as _strategy_config

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
        self._strategy_engine = None  # 延迟注入, type=strategy 规则用它跑选股
        # symbol → 股票名 (enriched DataFrame 已 drop name 列, 触发时从此映射回填)
        self._name_map: dict[str, str] = {}
        # 策略选股池状态: strategy_id → 上期选股符号集合 (用于 diff 变更)
        self._strategy_pools: dict[str, set[str]] = {}
        # 数据目录 (用于加载策略 overrides)
        self._data_dir = None

    def set_strategy_engine(self, engine) -> None:
        """注入 StrategyEngine, type=strategy 规则据此跑选股。"""
        self._strategy_engine = engine

    def set_data_dir(self, data_dir) -> None:
        """注入数据目录, 用于加载策略的用户覆盖配置。"""
        self._data_dir = data_dir

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
        #    元组格式: (event_type, symbol, name, price, pct, signals)
        hit_rows: list[tuple[str, str, Any, Any, Any, list[str]]] = []

        rtype = rule.get("type", "signal")
        if rtype == "strategy":
            # 策略类型: 跑策略选股 → 对比上期选股池 → 产出 new_entry/dropped 事件
            hit_rows = self._match_strategy(scoped, rule)
        else:
            # signal / price / market: 通用条件匹配
            for sym, name, price, pct, hit_sigs in self._match_conditions(scoped, rule):
                hit_rows.append((rtype, sym, name, price, pct, hit_sigs))

        if not hit_rows:
            return []

        # 3. cooldown 去重 + 生成 events
        cooldown = rule.get("cooldown_seconds", 3600)
        severity = rule.get("severity", "info")
        source = rtype

        events: list[dict] = []
        for ev_type, sym, name, price, pct, hit_sigs in hit_rows:
            # cooldown 键: 批量事件用特殊键, 单只事件用 (rule_id, symbol)
            is_batch = sym == "_batch"
            if is_batch:
                key = (rule["id"], f"_{ev_type}_batch")
            else:
                key = (rule["id"], sym)
            last = self._last_fire.get(key)
            if last is not None and (now - last) < cooldown:
                continue  # 冷却期内, 跳过
            self._last_fire[key] = now

            # 批量事件: name 存放预构建的消息文本
            if is_batch:
                resolved_name = ""
                message = name  # name 字段即批量消息
            else:
                resolved_name = name if name else self._name_map.get(sym)
                message = rule.get("message", "") or self._default_message(rule, ev_type=ev_type, sym=sym, name=resolved_name, pct=pct)

            ev = {
                "ts": int(now * 1000),
                "rule_id": rule["id"],
                "rule_name": rule.get("name", ""),
                "source": source,
                "type": ev_type,
                "symbol": "" if is_batch else sym,
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
    ) -> list[tuple[str, str, Any, Any, Any, list[str]]]:
        """策略类型评估: 跑策略选股 → 对比上期选股池 → 产出变更事件。

        返回 [(event_type, symbol, name, price, pct, signals)]
        event_type: "new_entry" (新入选) | "dropped" (已移出)
        单只变更逐只返回; 同一策略 >5 只合并为一条批量事件 (symbol="_batch")
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

        # 需要历史数据的策略跳过 (实时监控不支持 history loader)
        if s.filter_history_fn:
            logger.debug("策略 %s 需要历史数据, 跳过实时监控", sid)
            return []

        # 运行策略选股: 复用当前 enriched DataFrame 跳过数据加载
        overrides = {}
        if self._data_dir:
            try:
                overrides = _strategy_config.load_override(self._data_dir, sid)
            except Exception:
                pass

        try:
            result = self._strategy_engine.run(
                sid,
                as_of=_dt.date.today(),
                precomputed=df,
                overrides=overrides,
            )
        except Exception as e:
            logger.warning("策略 %s 选股执行失败: %s", sid, e)
            return []

        current_pool: set[str] = {r["symbol"] for r in result.rows}
        prev_pool = self._strategy_pools.get(sid)

        # 首次运行: 仅记录当前选股池, 不产生事件
        if prev_pool is None:
            self._strategy_pools[sid] = current_pool
            return []

        new_entries = current_pool - prev_pool
        dropped = prev_pool - current_pool

        # 无变更
        if not new_entries and not dropped:
            return []

        # 更新存储
        self._strategy_pools[sid] = current_pool

        sname = s.meta.get("name", "") or s.meta.get("id", sid)

        # 构建查找表 (新入选股票可在 result.rows 中找到; 移出股票需从 df 找)
        row_map: dict[str, dict] = {r["symbol"]: r for r in result.rows}
        dropped_map: dict[str, dict] = {}
        if dropped:
            try:
                _dd = df.filter(pl.col("symbol").is_in(list(dropped)))
                for row in _dd.iter_rows(named=True):
                    dropped_map[row["symbol"]] = row
            except Exception:
                pass

        results: list[tuple[str, str, Any, Any, Any, list[str]]] = []

        # ── 新入选 ──
        new_list = sorted(new_entries)
        if len(new_list) > 5:
            names: list[str] = []
            for sym in new_list:
                row = row_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                names.append(str(name))
            message = f"策略「{sname}」进入 {len(new_entries)} 只：{'、'.join(names)}"
            results.append(("new_entry", "_batch", message, None, None, []))
        else:
            for sym in new_list:
                row = row_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                price = row.get("close")
                pct = row.get("change_pct")
                results.append(("new_entry", sym, name, price, pct, []))

        # ── 已移出 ──
        dropped_list = sorted(dropped)
        if len(dropped_list) > 5:
            names = []
            for sym in dropped_list:
                row = dropped_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                names.append(str(name))
            message = f"策略「{sname}」移出 {len(dropped)} 只：{'、'.join(names)}"
            results.append(("dropped", "_batch", message, None, None, []))
        else:
            for sym in dropped_list:
                row = dropped_map.get(sym, {})
                name = row.get("name") or self._name_map.get(sym, sym)
                price = row.get("close")
                pct = row.get("change_pct")
                results.append(("dropped", sym, name, price, pct, []))

        return results

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

    def _default_message(self, rule: dict, ev_type: str = "", sym: str = "",
                          name: str = "", pct: Any = None) -> str:
        """生成默认 message。策略类型按变更方向生成。"""
        rtype = rule.get("type", "signal")
        if rtype == "strategy":
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

            if ev_type == "new_entry":
                pct_text = ""
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    pct_text = f" {sign}{pct * 100:.1f}%"
                return f"策略「{sname}」进入 {name}{pct_text}"
            elif ev_type == "dropped":
                pct_text = ""
                if pct is not None:
                    sign = "+" if pct >= 0 else ""
                    pct_text = f" {sign}{pct * 100:.1f}%"
                return f"策略「{sname}」移出 {name}{pct_text}"
            return f"策略「{sname}」变更"

        name_map = {"signal": "信号触发", "price": "价格触发", "market": "市场异动"}
        return name_map.get(rtype, "监控触发")
