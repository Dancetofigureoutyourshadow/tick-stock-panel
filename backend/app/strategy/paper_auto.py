"""自动跟单规则域 — 监控事件触发模拟盘自动下单 (V2)。

规则匹配: 事件 source=="strategy" 且 strategy_id 相等 (跟策略), 或 rule_id 相等
(跟任意监控规则, 含信号/价格规则)。每条规则限制自身占账户权益的最大仓位和持仓数量。
冷却: 同规则同 symbol 最近一次自动下单后 N 个交易日内不再触发 (按订单
created_at 日期判断, 无独立状态文件 — 可由订单列表重放推导)。

规则按账户存放 (accounts/{account_id}/auto_rules/), 账户间隔离;
on_rule_events 由钩子对每个账户各调一次。

触发链路: quote_service._evaluate_monitors 产出的 rule_events →
paper_auto.on_rule_events → paper.create_order(source=f"auto:{rule_id}")。
所有下单走 paper 域的同一把锁与校验, 账户冻结/资金/涨跌停等约束自动生效。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date as _date
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper
from app.strategy import paper_intents

logger = logging.getLogger(__name__)


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


def load_auto_rules(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID, enabled_only: bool = False) -> list[dict]:
    out: list[dict] = []
    for f in sorted(_dir(data_dir, account_id).glob("arule_*.json")):
        try:
            r = normalize_rule(json.loads(f.read_text(encoding="utf-8")))
            if enabled_only and not r.get("enabled"):
                continue
            out.append(r)
        except Exception as e:
            logger.warning("paper auto rule load failed %s: %s", f.name, e)
    return out


def save_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    with paper.PAPER_LOCK:
        validate_rule(rule)
        if rule.get("enabled") and rule.get("match_kind") == "strategy":
            for existing in load_auto_rules(data_dir, account_id):
                if (
                    existing.get("id") != rule.get("id")
                    and existing.get("enabled", True)
                    and existing.get("match_kind") == "strategy"
                    and existing.get("match_id") == rule.get("match_id")
                ):
                    raise ValueError(f"策略 {rule['match_id']} 已有启用的自动跟单规则, 请先停用或编辑原规则")
        atomic_write_text(_dir(data_dir, account_id) / f"{rule['id']}.json", json.dumps(rule, ensure_ascii=False, indent=2))
        return rule


def create_auto_rule(data_dir: Path, rule: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    rule = normalize_rule({**rule, "id": _new_id()})
    validate_rule(rule)
    return save_auto_rule(data_dir, rule, account_id)


def delete_auto_rule(data_dir: Path, rule_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> bool:
    with paper.PAPER_LOCK:
        p = _dir(data_dir, account_id) / f"{rule_id}.json"
        if p.exists():
            p.unlink()
            return True
        return False


def set_enabled(data_dir: Path, rule_id: str, enabled: bool, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    rule = normalize_rule(json.loads(p.read_text(encoding="utf-8")))
    rule["enabled"] = enabled
    return save_auto_rule(data_dir, rule, account_id)


def update_auto_rule(data_dir: Path, rule_id: str, values: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{rule_id}.json"
    if not p.exists():
        return None
    current = normalize_rule(json.loads(p.read_text(encoding="utf-8")))
    updated = normalize_rule({**current, **values, "id": rule_id, "created_at": current.get("created_at")})
    return save_auto_rule(data_dir, updated, account_id)


def _strategy_event_side(ev: dict) -> str | None:
    if ev.get("type") in ("buy_signal", "pool_entry"):
        return "buy"
    if ev.get("type") in ("sell_signal", "pool_exit"):
        return "sell"
    return None


def _event_side(rule: dict, ev: dict) -> str | None:
    if rule.get("match_kind") == "strategy":
        return _strategy_event_side(ev)
    return rule.get("side") if rule.get("side") in ("buy", "sell") else None


def _matches(rule: dict, ev: dict) -> bool:
    if rule["match_kind"] == "strategy":
        if ev.get("source") != "strategy" or ev.get("strategy_id") != rule["match_id"]:
            return False
        return _strategy_event_side(ev) in ("buy", "sell")
    return ev.get("rule_id") == rule["match_id"]


def _in_cooldown(
    data_dir: Path,
    rule: dict,
    symbol: str,
    cooldown_days: int,
    account_id: str,
    side: str | None = None,
) -> bool:
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
        if intent.get("status") != "pending" or intent.get("source") != prefix or intent.get("symbol") != symbol:
            continue
        if side is not None and intent.get("side") != side:
            continue
        return True
    return False


def _equity(data_dir: Path, account_id: str, prices: dict[str, float] | None = None) -> float:
    acc = paper.get_account(data_dir, account_id)
    if acc is None:
        return 0.0
    positions = paper.load_positions(data_dir, account_id)
    return float(acc.get("cash", 0)) + sum(
        float(pos.get("qty", 0)) * float((prices or {}).get(symbol, pos.get("avg_cost", 0)))
        for symbol, pos in positions.items()
    )


def _rule_scope(rule: dict) -> str | None:
    return rule.get("match_id") if rule.get("match_kind") == "strategy" else None


def _occupied_symbols(data_dir: Path, account_id: str, strategy_scope_id: str | None) -> set[str]:
    symbols: set[str] = set()
    for symbol, position in paper.load_positions(data_dir, account_id).items():
        lots = position.get("lots", []) or []
        if strategy_scope_id:
            lots = [lot for lot in lots if lot.get("strategy_id") == strategy_scope_id]
        if sum(int(lot.get("qty", 0)) for lot in lots) > 0:
            symbols.add(symbol)
    _, pending_symbols = paper._pending_strategy_buy_reservations(
        data_dir, account_id, strategy_scope_id,
    ) if strategy_scope_id else (0.0, set())
    return symbols | pending_symbols


def _rule_capacity(data_dir: Path, rule: dict, events: list[dict], account_id: str) -> tuple[float, int]:
    """返回本规则当前还能使用的金额和持仓名额。金额是账户权益的百分比。"""
    positions = paper.load_positions(data_dir, account_id)
    scope_id = _rule_scope(rule)
    prices = {str(ev.get("symbol")): float(ev["price"]) for ev in events if ev.get("symbol") and ev.get("price")}
    symbols: set[str] = set()
    current_value = 0.0
    for symbol, position in positions.items():
        lots = position.get("lots", []) or []
        if scope_id:
            lots = [lot for lot in lots if lot.get("strategy_id") == scope_id]
        qty = sum(int(lot.get("qty", 0)) for lot in lots)
        if qty <= 0:
            continue
        symbols.add(symbol)
        current_value += qty * prices.get(symbol, float(position.get("avg_cost", 0)))
    equity = _equity(data_dir, account_id, prices)
    maximum = equity * float(rule.get("max_total_position", 100)) / 100.0
    remaining_value = max(0.0, maximum - current_value)
    reserved_value, reserved_symbols = paper._pending_strategy_buy_reservations(data_dir, account_id, scope_id)
    remaining_value = max(0.0, remaining_value - reserved_value)
    remaining_slots = max(0, int(rule.get("max_positions", paper.MAX_POSITION_SYMBOLS)) - len(symbols | reserved_symbols))
    return remaining_value, remaining_slots


def _sizing_qty(data_dir: Path, amount: float, ref_price: float, account_id: str) -> int:
    if amount <= 0 or ref_price <= 0:
        return 0
    acc = paper.get_account(data_dir, account_id) or {}
    slippage = float(acc.get("slippage_bps", paper.DEFAULT_SLIPPAGE_BPS)) / 10000.0
    qty = paper.qty_from_amount(amount / (1 + slippage), ref_price)
    while qty > 0:
        estimated_price = ref_price * (1 + slippage)
        fee = paper.buy_fee(qty, estimated_price, float(acc.get("commission_pct", paper.DEFAULT_COMMISSION_PCT)))
        if qty * estimated_price + fee <= amount + 1e-6:
            return qty
        qty -= paper.LOT_SIZE
    return 0


def on_rule_events(data_dir: Path, events: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """监控事件 → 自动下单。返回本次创建的订单列表 (被拒订单只记日志)。

    触发条件: 事件带 symbol 与价格、有规则匹配、未冷却; 下单复用 paper.create_order
    (冻结/资金/T+1/涨跌停等校验自动生效), ref_price 用事件价格 (当前快照价)。
    """
    created: list[dict] = []
    if not events:
        return created
    rules = load_auto_rules(data_dir, account_id, enabled_only=True)
    if not rules:
        return created
    with paper.PAPER_LOCK:
        acc = paper.get_account(data_dir, account_id)
        if acc is None:
            return created
        for rule in rules:
            matched = [x for x in events if _matches(rule, x) and x.get("symbol") and x.get("price") and float(x["price"]) > 0]
            if not matched:
                continue
            buy_candidates = [
                x for x in matched
                if _event_side(rule, x) == "buy"
                and not _in_cooldown(data_dir, rule, str(x["symbol"]), int(rule.get("cooldown_days", 0)), account_id, "buy")
            ]
            # 同一轮扫描同一规则同一标的只保留一个信号，避免重复创建订单。
            buy_by_symbol: dict[str, dict] = {}
            for event in buy_candidates:
                symbol = str(event["symbol"]).strip()
                previous = buy_by_symbol.get(symbol)
                if previous is None or float(event.get("score") or 0) > float(previous.get("score") or 0):
                    buy_by_symbol[symbol] = event
            buys = list(buy_by_symbol.values())
            buy_budget, remaining_slots = _rule_capacity(data_dir, rule, buys, account_id)
            occupied_symbols = _occupied_symbols(data_dir, account_id, _rule_scope(rule))
            if buys and remaining_slots <= 0:
                logger.info("paper auto %s: 策略持仓名额已满", rule["name"])
            raw_scores = [max(float(x.get("score") or 0), 0.0) for x in buys]
            score_total = sum(raw_scores)
            sell_events = [x for x in matched if _event_side(rule, x) == "sell"]
            for ev in buys + sell_events:
                symbol = str(ev["symbol"]).strip()
                price = float(ev["price"])
                order_side = _event_side(rule, ev)
                if order_side is None:
                    continue
                if order_side == "buy":
                    if remaining_slots <= 0 and symbol not in occupied_symbols:
                        continue
                    weight = (max(float(ev.get("score") or 0), 0.0) / score_total) if rule.get("position_sizing") == "score_weight" and score_total > 0 else 1.0 / len(buys)
                    qty = _sizing_qty(data_dir, buy_budget * weight, price, account_id)
                else:
                    if _in_cooldown(data_dir, rule, symbol, int(rule.get("cooldown_days", 0)), account_id, "sell"):
                        continue
                    qty = paper.normalize_qty(sum(
                        int(lot.get("qty", 0)) for lot in (paper.load_positions(data_dir, account_id).get(symbol) or {}).get("lots", [])
                        if lot.get("strategy_id") == ev.get("strategy_id") and str(lot.get("date", "")) < cn_now().date().isoformat()
                    )) if rule.get("match_kind") == "strategy" else paper.normalize_qty(int((paper.load_positions(data_dir, account_id).get(symbol) or {}).get("available_qty", 0)))
                if qty <= 0:
                    logger.info("paper auto %s: %s 无可用策略仓位或预算不足", rule["name"], symbol)
                    continue
                target_lots: list[dict | None] = [None]
                if order_side == "sell" and rule.get("match_kind") == "strategy":
                    position = paper.load_positions(data_dir, account_id).get(symbol) or {}
                    target_lots = [
                        lot for lot in position.get("lots", [])
                        if lot.get("strategy_id") == ev.get("strategy_id")
                        and int(lot.get("qty", 0)) > 0
                        and str(lot.get("date", "")) < cn_now().date().isoformat()
                    ]
                    if not target_lots:
                        logger.info("paper auto %s: %s 无本策略可卖批次", rule["name"], symbol)
                        continue
                fill_policy = rule.get("entry_fill") if order_side == "buy" else rule.get("exit_fill")
                order_type = "market" if fill_policy == "close_t" else "next_open"
                source = f"auto:{rule['id']}"
                remaining_qty = qty
                for target_lot in target_lots:
                    target_lot_id = str(target_lot.get("lot_id")) if target_lot else None
                    target_qty = remaining_qty
                    if target_lot:
                        reserved = paper._reserved_target_qty(data_dir, account_id, symbol, target_lot_id)
                        target_qty = min(target_qty, max(0, int(target_lot.get("qty", 0)) - reserved))
                        target_qty = paper.normalize_qty(target_qty)
                    if target_qty <= 0:
                        continue
                    strategy_scope_id = _rule_scope(rule)
                    automation_source = "strategy_exit" if order_side == "sell" and rule.get("match_kind") == "strategy" else "auto_follow"
                    # 策略卖点属于策略退出，使用退出开关；买入和普通监控规则仍使用自动跟单开关。
                    auto_enabled = (
                        rule.get("auto_exit_enabled", paper.DEFAULT_AUTO_EXIT_ENABLED)
                        if order_side == "sell" and rule.get("match_kind") == "strategy"
                        else rule.get("auto_follow_enabled", paper.DEFAULT_AUTO_FOLLOW_ENABLED)
                    )
                    if not auto_enabled:
                        intent = paper_intents.create(
                            data_dir, account_id=account_id, symbol=symbol, side=order_side,
                            qty=target_qty, order_type=order_type, ref_price=float(price), source=source,
                            reason=f"自动跟单规则: {rule['name']}", strategy_id=ev.get("strategy_id"),
                            strategy_snapshot=ev.get("strategy_snapshot"), buy_score=ev.get("score"),
                            exit_reason="signal" if order_side == "sell" and rule.get("match_kind") == "strategy" else None,
                            lot_id=target_lot_id,
                            auto_rule_id=rule["id"], strategy_scope_id=strategy_scope_id,
                            automation_source=automation_source,
                            max_positions=rule.get("max_positions"), max_total_position=float(rule.get("max_total_position", 100)) / 100,
                        )
                        created.append({**intent, "kind": "intent"})
                    else:
                        order, err = paper.create_order(
                            data_dir, symbol, order_side, account_id=account_id,
                            qty=target_qty, order_type=order_type, ref_price=float(price), source=source,
                            strategy_id=ev.get("strategy_id"), strategy_snapshot=ev.get("strategy_snapshot"),
                            buy_score=ev.get("score"),
                            exit_reason="signal" if order_side == "sell" and rule.get("match_kind") == "strategy" else None,
                            target_lot_id=target_lot_id,
                            auto_rule_id=rule["id"], strategy_scope_id=strategy_scope_id,
                            automation_source=automation_source,
                            max_positions=rule.get("max_positions"), max_total_position=float(rule.get("max_total_position", 100)) / 100,
                        )
                        if err:
                            logger.info("paper auto %s: %s 下单被拒: %s", rule["name"], symbol, err)
                            continue
                        created.append(order)
                        logger.info("paper auto %s: %s 触发 %s %d 股 (%s)", rule["name"], symbol, order_side, target_qty, order["id"])
                    remaining_qty -= target_qty
                    if order_side == "buy":
                        if symbol not in occupied_symbols:
                            remaining_slots -= 1
                            occupied_symbols.add(symbol)
                    if remaining_qty <= 0:
                        break
    return created


_ORDER_TYPE_LABEL = {"market": "即时", "next_open": "次日开盘", "close": "当日收盘"}


def auto_order_events(created: list[dict], account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    """自动跟单创建的订单/意图 → 推送事件。与成交事件同构 (source=paper 对齐
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
