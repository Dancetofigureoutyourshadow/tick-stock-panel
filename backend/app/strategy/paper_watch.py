"""模拟盘绑定规则的内存态同步; 不落盘、不进监控页。

旧跟单用 follow 首轮信号, 现代绑定用 watch 首轮基线及四类事件。
绑定独立于真实监控的作用域/通知限制; 相同上下文的计算由监控引擎复用。
合成事件仅喂模拟盘, 不进公开通知或告警历史。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAPER_WATCH_RULE_PREFIX = "paper_watch:"
_NOTIFY_EVENTS = ("buy_signal", "sell_signal", "pool_entry", "pool_exit")


def is_paper_watch_event(ev: dict) -> bool:
    return str(ev.get("rule_id") or "").startswith(PAPER_WATCH_RULE_PREFIX)


def is_paper_sandbox_event(ev: dict) -> bool:
    from app.strategy.paper_auto import FOLLOW_MONITOR_PREFIX

    return str(ev.get("rule_id") or "").startswith((FOLLOW_MONITOR_PREFIX, PAPER_WATCH_RULE_PREFIX))


def synthetic_rule_id(strategy_id: str) -> str:
    return f"{PAPER_WATCH_RULE_PREFIX}{strategy_id}"


def _make_rule(strategy_id: str) -> dict:
    return {
        "id": synthetic_rule_id(strategy_id),
        "name": f"模拟盘自动评估 · {strategy_id}",
        "type": "strategy",
        "strategy_id": strategy_id,
        "asset_type": "stock",
        "scope": "all",
        "enabled": True,
        "notify_events": list(_NOTIFY_EVENTS),
    }


def sync_from_state(app_state: Any, data_dir: Path) -> dict[str, Any] | None:
    """引擎未就绪时安全跳过; 其余调用方应在持久化成功后同步。"""
    monitor_engine = getattr(app_state, "monitor_engine", None)
    strategy_engine = getattr(app_state, "strategy_engine", None)
    if monitor_engine is None or strategy_engine is None:
        return None
    try:
        return sync(data_dir, monitor_engine, strategy_engine)
    except Exception as e:  # noqa: BLE001
        logger.warning("paper watch sync failed (ignored): %s", e)
        return None


def watched_strategy_ids(data_dir: Path) -> set[str]:
    """仅现代绑定使用 watch; legacy 的首轮信号由 follow 独立提供。"""
    from app.strategy import paper, paper_auto

    return {
        str(rule["match_id"])
        for acc_id in paper.list_account_ids(data_dir)
        for rule in paper_auto.load_auto_rules(data_dir, acc_id, enabled_only=True)
        if rule.get("match_kind") == "strategy" and rule.get("match_id")
        and not rule.get("_legacy_mode")
    }


def build_rules(data_dir: Path, real_rules: list[dict], strategy_engine: Any) -> list[dict]:
    """先构造完整规则集, 再由调用方一次 set_rules, 保留未变规则的状态。"""
    from app.strategy import paper_auto

    synthetic = paper_auto.strategy_monitor_rules(data_dir)
    synthetic += [_make_rule(sid) for sid in sorted(watched_strategy_ids(data_dir))]
    valid = []
    for rule in synthetic:
        if strategy_engine is not None:
            try:
                if strategy_engine.get(rule["strategy_id"]) is None:
                    continue
            except ValueError:
                continue
        valid.append(rule)
    return [r for r in real_rules if not is_paper_sandbox_event({"rule_id": r["id"]})] + valid


def sync(
    data_dir: Path, monitor_engine: Any, strategy_engine: Any,
    *, real_rules: list[dict] | None = None,
) -> dict[str, Any]:
    """一次安装真实规则 + 两类合成规则, 不先清空再补回。"""
    current_rules = list(monitor_engine.rules.values())
    final_rules = build_rules(
        data_dir, current_rules if real_rules is None else real_rules, strategy_engine,
    )
    current = {
        str(r["strategy_id"]) for r in current_rules
        if str(r["id"]).startswith(PAPER_WATCH_RULE_PREFIX)
    }
    want = {
        str(r["strategy_id"]) for r in final_rules
        if str(r["id"]).startswith(PAPER_WATCH_RULE_PREFIX)
    }
    monitor_engine.set_rules(final_rules)
    return {"added": sorted(want - current), "removed": sorted(current - want), "watching": sorted(want)}
