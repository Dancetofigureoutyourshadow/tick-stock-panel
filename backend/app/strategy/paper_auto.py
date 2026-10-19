"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。仓位: 固定金额 或 账户权益百分比。
冷却: 同规则同 symbol 最近一次自动下单后 N 个交易日内不再触发 (按订单
created_at 日期判断, 无独立状态文件 — 可由订单列表重放推导)。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: quote_service._evaluate_monitors 产出的 rule_events →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper, paper_intents

logger = logging.getLogger(__name__)

FOLLOW_MONITOR_PREFIX = "paper_follow_"


def strategy_monitor_rules(data_dir: Path) -> list[dict]:
    """为启用的策略跟单构造内部信号监控，独立于用户的通知偏好。"""
    strategy_ids = {
        str(rule["match_id"])
        for account_id in paper.list_account_ids(data_dir)
        for rule in load_auto_rules(data_dir, account_id, enabled_only=True)
        if rule.get("match_kind") == "strategy"
    }
    return [{
        "id": FOLLOW_MONITOR_PREFIX + hashlib.sha1(sid.encode("utf-8")).hexdigest()[:16],
        "name": "模拟盘策略跟单",
        "type": "strategy",
        "asset_type": "stock",
        "scope": "all",
        "symbols": [],
        "strategy_id": sid,
        "notify_events": ["buy_signal", "sell_signal"],
        "conditions": [],
        "cooldown_seconds": 0,
    } for sid in sorted(strategy_ids)]


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "auto_rules"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"arule_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def validate_rule(rule: dict) -> None:
    """校验规则字段, 非法抛 ValueError (中文信息)。"""
    if (rule.get("name") or "").strip() == "":
        raise ValueError("规则名称不能为空")
    if rule.get("match_kind") not in ("strategy", "rule"):
        raise ValueError(f"match_kind 非法: {rule.get('match_kind')!r} (应为 strategy / rule)")
    if not (rule.get("match_id") or "").strip():
        raise ValueError("match_id 不能为空")
    legacy = bool(rule.get("_legacy_mode") or "position_sizing" in rule or (rule.get("side") == "auto" and "size_value" not in rule) or "auto_follow_enabled" in rule or "auto_exit_enabled" in rule)
    if legacy:
        _validate_legacy_rule(rule)
        return
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("side") == "auto" and rule.get("match_kind") != "strategy":
        raise ValueError("side=auto (跟信号方向) 仅支持匹配策略 — 监控规则事件无方向语义")
    if rule.get("size_mode") not in ("fixed_amount", "pct_equity"):
        raise ValueError(f"size_mode 非法: {rule.get('size_mode')!r}")
    value = rule.get("size_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("size_value 必须是正数")
    if rule.get("size_mode") == "pct_equity" and value > 100:
        raise ValueError("pct_equity 的 size_value 不能超过 100 (%)")
    if rule.get("order_type") not in ("market", "next_open", "close"):
        raise ValueError(f"order_type 非法: {rule.get('order_type')!r}")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")
    max_positions = rule.get("max_positions", 0)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 0:
        raise ValueError("max_positions 必须是非负整数 (0 = 不限制)")


def _validate_legacy_rule(rule: dict) -> None:
    if rule.get("side") not in ("buy", "sell", "auto"):
        raise ValueError(f"side 非法: {rule.get('side')!r}")
    if rule.get("match_kind") == "strategy" and rule.get("side") != "auto":
        raise ValueError("跟策略时不需要选择买入或卖出, side 必须为 auto")
    if rule.get("match_kind") == "rule" and rule.get("side") not in ("buy", "sell"):
        raise ValueError("跟监控规则时必须明确选择买入或卖出")
    if rule.get("entry_fill") not in ("close_t", "open_t+1"):
        raise ValueError("建仓口径必须是 close_t 或 open_t+1")
    if rule.get("exit_fill") not in ("close_t", "open_t+1"):
        raise ValueError("清仓口径必须是 close_t 或 open_t+1")
    if rule.get("position_sizing") not in ("equal", "score_weight"):
        raise ValueError("买入权重必须是 equal 或 score_weight")
    max_positions = rule.get("max_positions")
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or not 0 < max_positions <= paper.MAX_POSITION_SYMBOLS:
        raise ValueError(f"最大持仓数必须在 1 到 {paper.MAX_POSITION_SYMBOLS} 之间")
    max_total_position = rule.get("max_total_position")
    if isinstance(max_total_position, bool) or not isinstance(max_total_position, (int, float)) or not 0 < float(max_total_position) <= 100:
        raise ValueError("策略最大总仓位必须在 0 到 100 (%) 之间")
    cooldown = rule.get("cooldown_days", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError("cooldown_days 必须是非负整数")


def normalize_rule(rule: dict) -> dict:
    d = dict(rule)
    d["name"] = (d.get("name") or "").strip()
    d["match_id"] = (d.get("match_id") or "").strip()
    legacy = bool(d.get("_legacy_mode") or "position_sizing" in d or (d.get("side") == "auto" and "size_value" not in d) or "auto_follow_enabled" in d or "auto_exit_enabled" in d)
    if legacy:
        d["_legacy_mode"] = True
        if not d.get("side"):
            d["side"] = "auto" if d.get("match_kind") == "strategy" else "buy"
        d.setdefault("entry_fill", paper.DEFAULT_ENTRY_FILL)
        d.setdefault("exit_fill", paper.DEFAULT_EXIT_FILL)
        d.setdefault("position_sizing", paper.DEFAULT_POSITION_SIZING)
        d.setdefault("max_positions", paper.DEFAULT_MAX_POSITIONS)
        d.setdefault("max_total_position", paper.DEFAULT_MAX_TOTAL_POSITION * 100)
        d.setdefault("auto_follow_enabled", paper.DEFAULT_AUTO_FOLLOW_ENABLED)
        d.setdefault("auto_exit_enabled", paper.DEFAULT_AUTO_EXIT_ENABLED)
        d.setdefault("cooldown_days", 5)
        d.setdefault("enabled", True)
        d.setdefault("created_at", _now_iso())
        return d
    # side=auto: 跟随策略信号方向 (入场/进池 → 买入, 出场/移出 → 卖出)。
    # 存量规则缺 side 时按 buy 兼容 (原语义), 新建默认 auto。
    if d.get("match_kind") == "strategy":
        d.setdefault("side", "auto")
    else:
        d.setdefault("side", "buy")
    # 新的双向/持仓上限规则保持金额配置; 旧单向客户端仍走存量升级路径。
    if d["side"] == "auto" or "max_positions" in rule or "_legacy_mode" in rule:
        d["_legacy_mode"] = False
    d.setdefault("size_mode", "fixed_amount")
    d.setdefault("order_type", "next_open")
    d.setdefault("cooldown_days", 5)
    d.setdefault("max_positions", 0)   # 0 = 不限制 (对齐回测 max_positions 语义)
    d.setdefault("enabled", True)
    d.setdefault("created_at", _now_iso())
    return d


def _normalize_stored_rule(rule: dict) -> dict:
    normalized = normalize_rule(rule)
    if normalized.get("match_kind") == "strategy" and "_legacy_mode" not in rule and not normalized.get("_legacy_mode"):
        # 旧版单向策略规则升级为策略买卖双向跟单；普通监控规则仍保留单向行为。
        normalized = normalize_rule({
            **normalized,
            "side": "auto",
            "auto_follow_enabled": True,
            "auto_exit_enabled": True,
            "cooldown_days": 0,
            "max_positions": normalized.get("max_positions") or paper.DEFAULT_MAX_POSITIONS,
        })
    return normalized


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = _normalize_stored_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    validate_rule(rule)
    if rule.get("_legacy_mode") and rule.get("enabled") and rule.get("match_kind") == "strategy":
        for existing in load_auto_rules(data_dir, account_id):
            if existing.get("id") != rule.get("id") and existing.get("enabled") and existing.get("match_kind") == "strategy" and existing.get("match_id") == rule.get("match_id"):
                raise ValueError(f"策略 {rule['match_id']} 已有启用的自动跟单规则, 请先停用或编辑原规则")
    atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json", json.dumps(rule, ensure_ascii=False, indent=2))
    return rule


def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    rule = _normalize_stored_rule(json.loads(p.read_text(encoding="utf-8")))
    rule["enabled"] = enabled
    return save_auto_rule(data_dir, rule, account_id)


def update_auto_rule(data_dir: Path, rule_id: str, values: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    path = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not path.exists():
        return None
    current = _normalize_stored_rule(json.loads(path.read_text(encoding="utf-8")))
    updated = normalize_rule({**current, **values, "id": rule_id, "created_at": current.get("created_at")})
    return save_auto_rule(data_dir, updated, account_id)


def _matches(rule: dict, ev: dict) -> bool:
    if rule["match_kind"] == "strategy":
        if ev.get("source") != "strategy" or ev.get("strategy_id") != rule["match_id"]:
            return False
        return not rule.get("_legacy_mode") or ev.get("type") in ("buy_signal", "sell_signal")
    return ev.get("rule_id") == rule["match_id"]


def _strategy_event_side(event: dict) -> str | None:
    if event.get("type") in ("buy_signal", "pool_entry"):
        return "buy"
    if event.get("type") in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _in_cooldown(data_dir: Path, rule: dict, symbol: str, cooldown_days: int, account_id: str, side: str | None = None) -> bool:
    """同规则同 symbol 最近一次自动下单是否仍在冷却期 (按日历日, 含当日)。"""
    if cooldown_days <= 0:
        return False
    prefix = f"auto:{rule['id']}"
    today = cn_now().date()
    for order in paper.load_orders(data_dir, account_id):
        if order.get("source") != prefix or order.get("symbol") != symbol:
            continue
        if side is not None and order.get("side") != side:
            continue
        try:
            created = _date.fromisoformat(order["created_at"][:10])
        except (KeyError, ValueError):
            continue
        if (today - created).days < cooldown_days:
            return True
    for intent in paper_intents.load(data_dir, account_id):
        if intent.get("status") == "pending" and intent.get("source") == prefix and intent.get("symbol") == symbol:
            return True
    return False


def _sizing_qty(data_dir: Path, rule: dict, ref_price: float, account_id: str) -> int:
    if ref_price <= 0:
        return 0
    if rule["size_mode"] == "fixed_amount":
        amount = float(rule["size_value"])
    else:  # pct_equity: 按账户总权益 (现金 + 最新定版持仓市值)
        acc = paper.get_account(data_dir, account_id)
        if acc is None:
            return 0
        nav_rows = paper.load_nav(data_dir, account_id)
        equity = nav_rows[-1]["nav"] if nav_rows else float(acc["cash"])
        amount = equity * float(rule["size_value"]) / 100.0
    return paper.qty_from_amount(amount, ref_price)


def _legacy_events(data_dir: Path, events: list[dict], rules: list[dict], account_id: str, *, dry_run: bool = False) -> list[dict]:
    created: list[dict] = []
    today = cn_now().date().isoformat()
    account = paper.get_account(data_dir, account_id)
    if account is None:
        return created
    positions = paper.load_positions(data_dir, account_id)
    prices = {str(event.get("symbol")): float(event.get("price") or 0) for event in events if event.get("symbol") and event.get("price")}
    equity = float(account.get("cash", 0)) + sum(
        int(pos.get("qty", 0)) * prices.get(symbol, float(pos.get("avg_cost", 0)))
        for symbol, pos in positions.items()
    )
    for rule in rules:
        matched = [event for event in events if _matches(rule, event) and event.get("symbol") and float(event.get("price") or 0) > 0]
        if not matched:
            continue
        buys: dict[str, dict] = {}
        for event in matched:
            side = _strategy_event_side(event) if rule["match_kind"] == "strategy" else rule["side"]
            if side != "buy" or (
                rule["match_kind"] != "strategy"
                and _in_cooldown(data_dir, rule, str(event["symbol"]), int(rule.get("cooldown_days", 0)), account_id, "buy")
            ):
                continue
            symbol = str(event["symbol"]).strip()
            if symbol not in buys or float(event.get("score") or 0) > float(buys[symbol].get("score") or 0):
                buys[symbol] = event
        buy_events = list(buys.values())
        scope = rule["match_id"] if rule["match_kind"] == "strategy" else None
        scoped_value = sum(
            int(lot.get("qty", 0)) * float(lot.get("price", lot.get("avg_cost", 0)))
            for pos in positions.values() for lot in (pos.get("lots", []) or [])
            if not scope or lot.get("strategy_id") == scope
        )
        pending = [
            order for order in paper.load_orders(data_dir, account_id)
            if order.get("status") == "pending" and order.get("side") == "buy"
            and order.get("strategy_scope_id") == scope
        ]
        scoped_value += sum(
            int(order.get("qty", 0)) * float(order.get("ref_price") or 0)
            for order in pending
        )
        occupied = {
            symbol for symbol, pos in positions.items()
            if sum(int(lot.get("qty", 0)) for lot in (pos.get("lots", []) or []) if not scope or lot.get("strategy_id") == scope) > 0
        }
        occupied.update(str(order.get("symbol")) for order in pending)
        slots = max(0, int(rule["max_positions"]) - len(occupied))
        buy_events.sort(key=lambda event: event.get("_rank", -float(event.get("score") or 0)))
        selected = [event for event in buy_events if str(event["symbol"]).strip() not in occupied][:slots]

        max_exposure = float(rule["max_total_position"]) / 100
        exposure_capacity = max(0.0, equity * max_exposure - scoped_value)
        per_position_cap = equity * max_exposure / max(1, int(rule["max_positions"]))
        total_budget = min(float(account.get("cash", 0)), exposure_capacity, per_position_cap * len(selected))
        scores = [max(float(event.get("score") or 0), 0.0) for event in selected]
        score_total = sum(scores)
        weights = (
            [score / score_total for score in scores]
            if rule["position_sizing"] == "score_weight" and score_total > 0
            else [1 / len(selected)] * len(selected) if selected else []
        )
        order_type = "close" if rule["entry_fill"] == "close_t" else "next_open"
        for event, weight in zip(selected, weights, strict=True):
            symbol = str(event["symbol"]).strip()
            allocation = min(total_budget * weight, per_position_cap, float(account.get("cash", 0)))
            commission_pct = float(account.get("commission_pct", paper.DEFAULT_COMMISSION_PCT))
            slippage_bps = float(account.get("slippage_bps", paper.DEFAULT_SLIPPAGE_BPS))
            estimated_unit_cost = float(event["price"]) * (1 + commission_pct + slippage_bps / 10000)
            qty = paper.qty_from_amount(allocation, estimated_unit_cost)
            if qty <= 0:
                continue
            auto_enabled = bool(rule.get("auto_follow_enabled", True))
            common = {
                "strategy_id": event.get("strategy_id"), "strategy_snapshot": event.get("strategy_snapshot") or {},
                "buy_score": event.get("score"), "strategy_scope_id": scope,
                "auto_rule_id": rule["id"], "automation_source": "auto_follow",
                "max_positions": rule["max_positions"], "max_total_position": max_exposure,
            }
            if dry_run:
                created.append({
                    "symbol": symbol, "side": "buy", "qty": qty, "order_type": order_type,
                    "ref_price": float(event["price"]), "source": f"auto:{rule['id']}",
                    "auto_enabled": auto_enabled, **common,
                })
            elif not auto_enabled:
                intent = paper_intents.create(
                    data_dir, account_id=account_id, symbol=symbol, side="buy", qty=qty,
                    order_type=order_type, ref_price=float(event["price"]), source=f"auto:{rule['id']}",
                    reason=f"自动跟单规则: {rule['name']}", **common,
                )
                created.append({**intent, "kind": "intent"})
            else:
                order, error = paper.create_order(
                    data_dir, symbol, "buy", account_id=account_id, qty=qty,
                    order_type=order_type, ref_price=float(event["price"]), source=f"auto:{rule['id']}", **common,
                )
                if not error and order:
                    created.append(order)
                    occupied.add(symbol)
        sell_events = [event for event in matched if ( _strategy_event_side(event) if rule["match_kind"] == "strategy" else rule["side"] ) == "sell"]
        for event in sell_events:
            symbol = str(event["symbol"]).strip()
            if rule["match_kind"] != "strategy" and _in_cooldown(
                data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id, "sell"
            ):
                continue
            position = paper.load_positions(data_dir, account_id).get(symbol) or {}
            lots = [lot for lot in position.get("lots", []) if int(lot.get("qty", 0)) > 0 and str(lot.get("date", "")) < today and (rule["match_kind"] != "strategy" or lot.get("strategy_id") == event.get("strategy_id"))]
            for lot in lots:
                if event.get("_lot_id") and str(lot.get("lot_id")) != event["_lot_id"]:
                    continue
                qty = paper.normalize_qty(int(lot.get("qty", 0)))
                if qty <= 0:
                    continue
                auto_enabled = bool(rule.get("auto_exit_enabled", False)) if rule["match_kind"] == "strategy" else bool(rule.get("auto_follow_enabled", True))
                order_type = "close" if rule["exit_fill"] == "close_t" else "next_open"
                common = {
                    "strategy_id": event.get("strategy_id"), "strategy_snapshot": event.get("strategy_snapshot") or lot.get("strategy_snapshot") or {},
                    "exit_reason": event.get("exit_reason", "signal") if rule["match_kind"] == "strategy" else None,
                    "target_lot_id": str(lot.get("lot_id")), "strategy_scope_id": scope,
                    "auto_rule_id": rule["id"], "automation_source": "strategy_exit" if rule["match_kind"] == "strategy" else "auto_follow",
                }
                if dry_run:
                    created.append({
                        "symbol": symbol, "side": "sell", "qty": qty, "order_type": order_type,
                        "ref_price": float(event["price"]), "source": f"auto:{rule['id']}",
                        "auto_enabled": auto_enabled, **common,
                    })
                elif not auto_enabled:
                    intent = paper_intents.create(
                        data_dir, account_id=account_id, symbol=symbol, side="sell", qty=qty,
                        order_type=order_type, ref_price=float(event["price"]), source=f"auto:{rule['id']}",
                        reason=f"自动跟单规则: {rule['name']}", lot_id=str(lot.get("lot_id")), **{k: v for k, v in common.items() if k != "target_lot_id"},
                    )
                    created.append({**intent, "kind": "intent"})
                else:
                    order, error = paper.create_order(
                        data_dir, symbol, "sell", account_id=account_id, qty=qty,
                        order_type=order_type, ref_price=float(event["price"]), source=f"auto:{rule['id']}", **common,
                    )
                    if not error and order:
                        created.append(order)
    return created


def _position_slots_used(data_dir: Path, account_id: str) -> set[str]:
    """已占用的持仓槽: 当前持仓标的 + 待成交买单标的 (去重)。
    模拟盘 next_open 单未成交也占槽, 防同日多信号重复开仓超限。"""
    used: set[str] = set()
    for symbol, p in paper.load_positions(data_dir, account_id).items():
        if p["qty"] > 0:
            used.add(symbol)
    for o in paper.load_orders(data_dir, account_id):
        if o.get("status") == "pending" and o["side"] == "buy":
            used.add(o["symbol"])
    return used


_AUTO_BUY_TYPES = {"buy_signal", "pool_entry"}


def _resolve_side(rule: dict, ev: dict) -> str | None:
    """规则方向 → 本次事件实际方向。side=auto (跟策略信号): 入场/进池买,
    出场/移出卖; 其余按规则固定方向。返回 None 表示该事件不适用 (如 auto
    收到无方向语义的事件类型)。"""
    side = rule.get("side", "buy")
    if side != "auto":
        return side
    etype = str(ev.get("type") or "")
    if etype in _AUTO_BUY_TYPES:
        return "buy"
    if etype in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _held_qty(data_dir: Path, symbol: str, account_id: str) -> int:
    p = paper.load_positions(data_dir, account_id).get(symbol)
    return int(p["qty"]) if p else 0


def on_rule_events(
    data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID,
    *, excluded_strategy_ids: set[str] | None = None,
) -> list[dict]:
    """监控事件 → 自动下单。返回本次创建的订单列表 (被拒订单只记日志)。

    触发条件: 事件带 symbol 与价格、有规则匹配、未冷却; 下单复用 paper.create_order
    (冻结/资金/T+1/涨跌停等校验自动生效), ref_price 用事件价格 (当前快照价)。
    买入信号受 max_positions 约束 (对齐回测口径): 已持仓 + 待成交买单的标的数
    达到上限后, 新标的开仓被跳过; 已持仓标的加仓与卖出信号不受限。
    """
    created: list[dict] = []
    if not events:
        return created
    rules = load_auto_rules(data_dir, account_id, enabled_only=True)
    if excluded_strategy_ids:
        rules = [r for r in rules if not (
            r.get("match_kind") == "strategy" and r.get("match_id") in excluded_strategy_ids
        )]
    if not rules:
        return created
    legacy_rules = [rule for rule in rules if rule.get("_legacy_mode")]
    if legacy_rules:
        created.extend(_legacy_events(data_dir, events, legacy_rules, account_id))
    rules = [rule for rule in rules if not rule.get("_legacy_mode")]
    if not rules:
        return created
    with paper.PAPER_LOCK:
        # 同一批事件内复用快照, 由 create_order 后的持仓变化反映 (循环内重取)
        for ev in events:
            symbol = (ev.get("symbol") or "").strip()
            price = ev.get("price")
            if not symbol or price is None or price <= 0:
                continue
            for rule in rules:
                if not _matches(rule, ev):
                    continue
                side = _resolve_side(rule, ev)
                if side is None:
                    continue
                if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id):
                    continue
                if side == "buy":
                    max_positions = int(rule.get("max_positions", 0))
                    if max_positions > 0:
                        used = _position_slots_used(data_dir, account_id)
                        if len(used) >= max_positions and symbol not in used:
                            logger.info(
                                "paper auto %s: %s 跳过开仓 — 已达最大持仓 %d (%s)",
                                rule["name"], symbol, max_positions, ", ".join(sorted(used)) or "空",
                            )
                            continue
                    qty = _sizing_qty(data_dir, rule, float(price), account_id)
                else:
                    # auto 的出场卖出 = 全平该标的 (策略出场语义); 固定 sell 规则仍按 sizing。
                    if rule.get("side") == "auto":
                        qty = _held_qty(data_dir, symbol, account_id)
                        if qty <= 0:
                            continue  # 未持仓, 出场信号无意义
                    else:
                        qty = _sizing_qty(data_dir, rule, float(price), account_id)
                        # 金额换算的股数与实际持仓对不上: 以可卖余量封顶, 金额设足够大即清仓
                        held = paper.sell_capacity(data_dir, symbol, account_id)
                        if held is None:
                            continue
                        available, pending_sell = held
                        qty = min(qty, paper.normalize_qty(available - pending_sell))
                if qty <= 0:
                    logger.info("paper auto %s: %s 金额不足以一手 (价 %s)", rule["name"], symbol, price)
                    continue
                order, err = paper.create_order(
                    data_dir, symbol, side,
                    account_id=account_id,
                    qty=qty,
                    order_type=rule["order_type"],
                    ref_price=float(price),
                    source=f"auto:{rule['id']}",
                )
                if err:
                    logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, err)
                    continue
                created.append(order)
                logger.info("paper auto %s: %s 触发 %s %d 股 (%s)", rule["name"], symbol, side, qty, order["id"])
    return created


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单 → 推送事件。与成交事件同构 (source=paper 对齐
    AlertEvent), 走同一推送通道; rule_id 从 order.source (auto:{rule_id}) 还原。
    """
    events: list[dict] = []
    for o in created:
        source = str(o.get("source", ""))
        rule_id = source.removeprefix("auto:") if source.startswith("auto:") else ""
        side_label = "买入" if o["side"] == "buy" else "卖出"
        type_label = _ORDER_TYPE_LABEL.get(o["order_type"], o["order_type"])
        is_intent = o.get("kind") == "intent"
        events.append({
            "source": "paper",
            "type": "auto_intent" if is_intent else "auto_order",
            "severity": "info",
            "ts": int(cn_now().timestamp() * 1000),
            "symbol": o["symbol"],
            "rule_id": rule_id,
            "side": o["side"],
            "qty": o["qty"],
            "account_id": account_id,
            "message": f"自动跟单触发{side_label}{'待确认操作' if is_intent else '下单'} ({type_label}) {o['qty']}股",
        })
    return events
