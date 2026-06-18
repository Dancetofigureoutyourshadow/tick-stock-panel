"""策略引擎 — 加载、执行、评分。

职责: 从文件系统加载策略 Python 模块，执行两阶段过滤(基础+策略)，
     通用评分排序。
不知道: AI、API、前端、配置持久化、回测。
"""
from __future__ import annotations

import importlib.util
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import polars as pl

logger = logging.getLogger(__name__)

# 引擎级默认基础过滤 — 策略未定义 BASIC_FILTER 时兜底
DEFAULT_BASIC_FILTER: dict = {
    "price_min": 3,
    "price_max": 300,
    "market_cap_min": 10e8,
    "float_cap_min": None,
    "float_cap_max": None,
    "amount_min": 0.2e8,
    "amount_max": None,
    "turnover_min": None,
    "turnover_max": None,
    "exclude_st": True,
    "exclude_new_days": 30,
    "boards": ["沪主板", "深主板", "创业板", "科创板", "北交所"],
}


@dataclass
class StrategyDef:
    """加载后的策略定义（只读数据 + filter 函数引用）"""
    meta: dict
    basic_filter: dict
    entry_signals: list[str]
    exit_signals: list[str]
    stop_loss: float | None
    trailing_stop: float | None
    trailing_take_profit_activate: float | None
    trailing_take_profit_drawdown: float | None
    max_hold_days: int | None
    alerts: list[dict]
    filter_fn: Callable[[pl.DataFrame, dict], pl.Expr] | None
    filter_history_fn: Callable[[pl.DataFrame, dict], pl.DataFrame] | None
    lookback_days: int
    source: str  # "builtin" | "custom" | "ai"
    file_path: Path | None = None


@dataclass
class StrategyResult:
    """策略执行结果"""
    as_of: date
    strategy_id: str
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)


class StrategyEngine:
    """策略引擎 — 策略加载 + 执行 + 评分"""

    def __init__(self, enriched_loader: Callable[[date], pl.DataFrame],
                 enriched_history_loader: Callable[[date, int], pl.DataFrame] | None = None,
                 strategy_dirs: list[Path] | None = None):
        """
        Args:
            enriched_loader: (date) -> pl.DataFrame, 加载指定日期的 enriched 数据
            strategy_dirs:   策略文件搜索目录列表
        """
        self._loader = enriched_loader
        self._history_loader = enriched_history_loader
        self._strategies: dict[str, StrategyDef] = {}
        self._strategy_dirs = strategy_dirs or []
        self._load_all()

    # ================================================================
    # 加载
    # ================================================================

    def _load_all(self) -> None:
        self._strategies.clear()
        for d in self._strategy_dirs:
            if not d.exists():
                continue
            for f in sorted(d.glob("*.py")):
                if f.name.startswith("_"):
                    continue
                try:
                    s = self._load_file(f)
                    self._strategies[s.meta["id"]] = s
                    logger.debug("loaded strategy: %s (%s)", s.meta["id"], s.source)
                except Exception as e:
                    logger.warning("load strategy %s failed: %s", f.name, e)

    @staticmethod
    def _load_file(path: Path) -> StrategyDef:
        """从 Python 文件加载策略定义"""
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load module from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        meta = getattr(mod, "META", {})
        meta.setdefault("id", path.stem)
        meta.setdefault("name", path.stem)
        meta.setdefault("description", "")
        meta.setdefault("tags", [])
        meta.setdefault("params", [])
        meta.setdefault("scoring", {})
        meta.setdefault("order_by", "score")
        meta.setdefault("descending", True)
        meta.setdefault("limit", 100)

        # 合并默认基础过滤
        bf = {**DEFAULT_BASIC_FILTER}
        strat_bf = getattr(mod, "BASIC_FILTER", None)
        if strat_bf:
            bf.update(strat_bf)
        # meta 里的 basic_filter 也合并（优先级最高）
        meta_bf = meta.get("basic_filter")
        if meta_bf:
            bf.update(meta_bf)

        source = "custom"
        if "builtin" in str(path).replace("\\", "/"):
            source = "builtin"
        elif "/ai/" in str(path).replace("\\", "/") or "\\ai\\" in str(path):
            source = "ai"

        return StrategyDef(
            meta=meta,
            basic_filter=bf,
            entry_signals=getattr(mod, "ENTRY_SIGNALS", []),
            exit_signals=getattr(mod, "EXIT_SIGNALS", []),
            stop_loss=getattr(mod, "STOP_LOSS", None),
            trailing_stop=getattr(mod, "TRAILING_STOP", None),
            trailing_take_profit_activate=getattr(mod, "TRAILING_TAKE_PROFIT_ACTIVATE", None),
            trailing_take_profit_drawdown=getattr(mod, "TRAILING_TAKE_PROFIT_DRAWDOWN", None),
            max_hold_days=getattr(mod, "MAX_HOLD_DAYS", None),
            alerts=getattr(mod, "ALERTS", []),
            filter_fn=getattr(mod, "filter", None),
            filter_history_fn=getattr(mod, "filter_history", None),
            lookback_days=int(getattr(mod, "LOOKBACK_DAYS", meta.get("lookback_days", 1)) or 1),
            source=source,
            file_path=path,
        )

    def reload(self) -> None:
        """热重载所有策略"""
        self._load_all()

    # ================================================================
    # 查询
    # ================================================================

    def list_strategies(self) -> list[dict]:
        """返回所有策略的元信息"""
        result = []
        for s in self._strategies.values():
            result.append({**s.meta, "source": s.source})
        return result

    def get(self, strategy_id: str) -> StrategyDef:
        s = self._strategies.get(strategy_id)
        if not s:
            raise ValueError(f"unknown strategy: {strategy_id}")
        return s

    def has(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies

    # ================================================================
    # 执行
    # ================================================================

    def run(
        self,
        strategy_id: str,
        as_of: date,
        pool: list[str] | None = None,
        params: dict | None = None,
        overrides: dict | None = None,
        precomputed: pl.DataFrame | None = None,
        precomputed_history: pl.DataFrame | None = None,
    ) -> StrategyResult:
        """执行策略: 基础过滤 → 策略过滤 → 评分排序

        Args:
            strategy_id:        策略 ID
            as_of:              选股日期
            pool:               限定股票池
            params:             策略参数 (用户在设置面板调的值)
            overrides:          用户覆盖配置 (basic_filter/scoring/stop_loss 等)
            precomputed:        已加载的 enriched 数据 (run_all 场景复用)
            precomputed_history: 已加载的历史窗口数据 (run_all 场景复用)
        """
        t0 = time.perf_counter()

        s = self.get(strategy_id)
        params = params or {}
        overrides = overrides or {}

        # 加载数据。普通策略只读目标日期；声明 filter_history 的策略读取历史窗口。
        if s.filter_history_fn:
            if precomputed_history is not None and not precomputed_history.is_empty():
                df = precomputed_history
            elif self._history_loader:
                df = self._history_loader(as_of, max(1, s.lookback_days))
            else:
                logger.warning("strategy %s requires history loader", strategy_id)
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            df = s.filter_history_fn(df, params)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)
            if "date" in df.columns:
                df = df.filter(pl.col("date") == as_of)
        elif precomputed is not None and not precomputed.is_empty():
            df = precomputed
        else:
            df = self._loader(as_of)
            if df.is_empty():
                return StrategyResult(as_of=as_of, strategy_id=strategy_id)

        # 基础过滤: 策略默认 basic_filter 兜底, 用户 override 优先覆盖。
        # 这样策略文件里写的 exclude_st/price_min 等默认值即使前端没保存也能生效。
        bf = dict(s.basic_filter) if s.basic_filter else {}
        if overrides and overrides.get("basic_filter"):
            bf.update(overrides["basic_filter"])

        # Stage 1: 基础过滤（enabled 默认开启; 显式 enabled=false 才跳过）
        if bf and bf.get("enabled", True):
            df = self._apply_basic_filter(df, bf)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # Stage 2: 策略过滤
        if s.filter_fn:
            expr = s.filter_fn(df, params)
            df = df.filter(expr)

        # Stage 3: 评分
        scoring = s.meta.get("scoring", {})
        scoring_overrides = overrides.get("scoring")
        if scoring_overrides:
            scoring = {**scoring, **scoring_overrides}
        df = self._apply_scoring(df, scoring)

        # 排序 + 限制
        limit = s.meta.get("limit", 100)
        order_desc = s.meta.get("descending", True)
        if "score" in df.columns:
            df = df.sort("score", descending=order_desc)
        elif s.meta.get("order_by") and s.meta["order_by"] != "score":
            ob = s.meta["order_by"]
            if ob in df.columns:
                df = df.sort(ob, descending=order_desc)
        df = df.head(limit)

        # 输出
        rows = _sanitize(df.to_dicts())
        elapsed = (time.perf_counter() - t0) * 1000

        scores: dict[str, float] = {}
        if "score" in df.columns:
            for r in df.iter_rows(named=True):
                scores[r["symbol"]] = float(r.get("score") or 0)

        return StrategyResult(
            as_of=as_of,
            strategy_id=strategy_id,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
            scores=scores,
        )

    def run_all(self, as_of: date, params_map: dict | None = None,
                overrides_map: dict | None = None) -> dict[str, StrategyResult]:
        """批量执行所有策略 (enriched 只加载一次，基础过滤按策略分组缓存，历史数据共享)"""
        df = self._loader(as_of)
        params_map = params_map or {}
        overrides_map = overrides_map or {}

        # 历史策略: 找最大 lookback，一次加载共享
        history_strats = [(sid, s) for sid, s in self._strategies.items() if s.filter_history_fn]
        if history_strats and self._history_loader:
            max_lookback = max(s.lookback_days for _, s in history_strats)
            shared_history = self._history_loader(as_of, max(1, max_lookback))
        else:
            shared_history = None

        # 按 basic_filter hash 分组，避免重复过滤
        bf_cache: dict[str, pl.DataFrame] = {}
        results: dict[str, StrategyResult] = {}

        for sid, strat in self._strategies.items():
            try:
                bf_key = _dict_hash(strat.basic_filter)
                if bf_key not in bf_cache:
                    if strat.basic_filter.get("enabled", True):
                        bf_cache[bf_key] = self._apply_basic_filter(df, strat.basic_filter)
                    else:
                        bf_cache[bf_key] = df
                base = bf_cache[bf_key]

                # 从已过滤的 base 执行 (filter_history 策略使用共享历史)
                results[sid] = self.run(
                    sid, as_of,
                    params=params_map.get(sid),
                    overrides=overrides_map.get(sid),
                    precomputed=base,
                    precomputed_history=shared_history,
                )
            except Exception as e:
                logger.warning("run strategy %s failed: %s", sid, e)

        return results

    # ================================================================
    # 内部: 基础过滤
    # ================================================================

    @staticmethod
    def _basic_filter_expr(df: pl.DataFrame, bf: dict) -> pl.Expr | None:
        """构建基础过滤表达式。回测可复用为买入候选 mask，不删除行情行。"""
        exprs: list[pl.Expr] = []
        if bf.get("price_min") is not None:
            exprs.append(pl.col("close") >= bf["price_min"])
        if bf.get("price_max") is not None:
            exprs.append(pl.col("close") <= bf["price_max"])
        if bf.get("market_cap_min") is not None and "total_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("total_shares") >= bf["market_cap_min"]
            )
        if bf.get("market_cap_max") is not None and "total_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("total_shares") <= bf["market_cap_max"]
            )
        # 流通市值
        if bf.get("float_cap_min") is not None and "float_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("float_shares") >= bf["float_cap_min"]
            )
        if bf.get("float_cap_max") is not None and "float_shares" in df.columns:
            exprs.append(
                pl.col("close") * pl.col("float_shares") <= bf["float_cap_max"]
            )
        if bf.get("amount_min") is not None:
            exprs.append(pl.col("amount") >= bf["amount_min"])
        if bf.get("amount_max") is not None:
            exprs.append(pl.col("amount") <= bf["amount_max"])
        # 换手率
        if bf.get("turnover_min") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") >= bf["turnover_min"])
        if bf.get("turnover_max") is not None and "turnover_rate" in df.columns:
            exprs.append(pl.col("turnover_rate") <= bf["turnover_max"])
        if bf.get("exclude_st") and "name" in df.columns:
            exprs.append(~pl.col("name").str.contains("(?i)ST|\\*ST|退"))
        # 板块过滤
        boards = bf.get("boards")
        if boards and isinstance(boards, list) and len(boards) > 0:
            board_exprs: list[pl.Expr] = []
            for b in boards:
                if b == "沪主板":
                    board_exprs.append(pl.col("symbol").str.starts_with("60"))
                elif b == "深主板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("00")
                        | pl.col("symbol").str.starts_with("001")
                    )
                elif b == "创业板":
                    board_exprs.append(
                        pl.col("symbol").str.starts_with("300")
                        | pl.col("symbol").str.starts_with("301")
                    )
                elif b == "科创板":
                    board_exprs.append(pl.col("symbol").str.starts_with("688"))
                elif b == "北交所":
                    board_exprs.append(pl.col("symbol").str.contains(r"\.BJ$"))
            if board_exprs:
                exprs.append(pl.any_horizontal(board_exprs))
        if exprs:
            return pl.all_horizontal(exprs)
        return None

    @staticmethod
    def _apply_basic_filter(df: pl.DataFrame, bf: dict) -> pl.DataFrame:
        """Stage 1: 基础参数过滤"""
        expr = StrategyEngine._basic_filter_expr(df, bf)
        if expr is not None:
            return df.filter(expr)
        return df

    # ================================================================
    # 内部: 评分
    # ================================================================

    @staticmethod
    def _apply_scoring(df: pl.DataFrame, weights: dict) -> pl.DataFrame:
        """通用评分: min-max 归一化 → 加权求和 → 0~100 分"""
        if not weights:
            return df
        total_weight = sum(weights.values())
        if total_weight <= 0:
            return df

        score_parts: list[pl.Expr] = []
        for col, weight in weights.items():
            if col not in df.columns:
                continue
            w = weight / total_weight
            col_min = pl.col(col).min()
            col_range = pl.col(col).max() - col_min
            normalized = pl.when(col_range > 0).then(
                (pl.col(col) - col_min) / col_range
            ).otherwise(pl.lit(0.5))
            score_parts.append(normalized * w)

        if not score_parts:
            return df

        score_expr = score_parts[0]
        for part in score_parts[1:]:
            score_expr = score_expr + part
        return df.with_columns((score_expr * 100).alias("score"))


def _sanitize(rows: list[dict]) -> list[dict]:
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                r[k] = None
    return rows


def _dict_hash(d: dict) -> str:
    """用于 basic_filter 分组缓存"""
    return str(sorted(d.items()))
