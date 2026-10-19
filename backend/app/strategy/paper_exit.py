"""Paper 持仓批次退出规则。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.strategy import paper, paper_intents

_REASON_LABELS = {
    "stop_loss": "止损",
    "trailing_stop": "移动止损",
    "trailing_take_profit": "移动止盈",
    "take_profit": "止盈",
    "signal": "策略卖点",
    "max_hold": "最大持有期",
}


def _state_path(data_dir: Path, account_id: str) -> Path:
    return paper._root(data_dir, account_id) / "position_exit_state.json"


def _load_state(data_dir: Path, account_id: str) -> dict[str, dict]:
    p = _state_path(data_dir, account_id)
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(data_dir: Path, account_id: str, state: dict[str, dict]) -> None:
    atomic_write_text(_state_path(data_dir, account_id), json.dumps(state, ensure_ascii=False, indent=2))


def enrich_overview(data_dir: Path, overview: dict, account_id: str) -> dict:
    state = _load_state(data_dir, account_id)
    for holding in overview.get("holdings", []) or []:
        for lot in holding.get("lots", []) or []:
            item = state.get(lot.get("lot_id"), {})
            snapshot = lot.get("strategy_snapshot") or {}
            lot.update({
                "high_water_price": item.get("high_water_price", lot.get("price")),
                "holding_days": int(item.get("holding_days", 0)),
                "pending_fingerprint": item.get("pending_fingerprint"),
                "has_exit_rules": bool(snapshot.get("exit_signals")) or any(
                    snapshot.get(key) is not None
                    for key in (
                        "stop_loss", "take_profit", "trailing_stop",
                        "trailing_take_profit_activate", "max_hold_days",
                    )
                ),
            })
        lot_state = {str(lot.get("lot_id")): lot for lot in holding.get("lots", []) or []}
        for group in holding.get("groups", []) or []:
            group_lots = []
            for lot in group.get("lots", []) or []:
                enriched = lot_state.get(str(lot.get("lot_id")), lot)
                group_lots.append(enriched)
            group["lots"] = group_lots
            group["has_exit_rules"] = any(lot.get("has_exit_rules") for lot in group_lots)
            group["pending_lots"] = [
                {
                    "lot_id": lot.get("lot_id"),
                    "reason": lot.get("pending_fingerprint"),
                }
                for lot in group_lots
                if lot.get("pending_fingerprint")
            ]
    return overview


def _positive(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _abs_pct(value) -> float | None:
    value = _positive(value)
    return value


def _rule_for_lot(data_dir: Path, lot: dict, account_id: str) -> dict | None:
    strategy_id = lot.get("strategy_id")
    if not strategy_id:
        return None
    from app.strategy import paper_auto
    rules = [
        rule for rule in paper_auto.load_auto_rules(data_dir, account_id)
        if rule.get("match_kind") == "strategy" and rule.get("match_id") == strategy_id
    ]
    return next((rule for rule in rules if rule.get("enabled", True)), None)


def _pending_action(data_dir: Path, lot_id: str, account_id: str) -> tuple[str, dict] | None:
    for order in paper.load_orders(data_dir, account_id):
        if order.get("status") == "pending" and order.get("side") == "sell" and str(order.get("target_lot_id") or "") == lot_id:
            return "order", order
    for intent in paper_intents.load(data_dir, account_id):
        if intent.get("status") == "pending" and intent.get("side") == "sell" and str(intent.get("lot_id") or "") == lot_id:
            return "intent", intent
    return None


def evaluate(
    data_dir: Path,
    rows: list[dict],
    *,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    trade_date: str,
    now_ms: int | None = None,
    daily_strategy_ids: set[str] | None = None,
) -> list[dict]:
    """并发安全地评估持仓退出规则并创建最多一个动作。"""
    with paper.PAPER_LOCK:
        return _evaluate_unlocked(
            data_dir,
            rows,
            account_id=account_id,
            trade_date=trade_date,
            now_ms=now_ms,
            daily_strategy_ids=daily_strategy_ids,
        )


def _evaluate_unlocked(
    data_dir: Path,
    rows: list[dict],
    *,
    account_id: str = paper.DEFAULT_ACCOUNT_ID,
    trade_date: str,
    now_ms: int | None = None,
    daily_strategy_ids: set[str] | None = None,
) -> list[dict]:
    account = paper.get_account(data_dir, account_id)
    if account is None:
        return []
    positions = paper.load_positions(data_dir, account_id)
    row_by_symbol = {str(row.get("symbol") or "").upper(): row for row in rows if row.get("symbol")}
    state = _load_state(data_dir, account_id)
    events: list[dict] = []
    event_ts = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
    changed = False
    # positions.json 的 available_qty 是物化缓存，跨交易日可能过期；退出评估必须按本次交易日重算。
    remaining_available = {
        symbol: paper._available_of(position, trade_date)
        for symbol, position in positions.items()
    }
    for symbol, position in positions.items():
        row = row_by_symbol.get(symbol)
        if row is None:
            continue
        close = _positive(row.get("raw_close") or row.get("last_price") or row.get("close"))
        if close is None:
            continue
        high = _positive(row.get("raw_high") or row.get("high")) or close
        low = _positive(row.get("raw_low") or row.get("low")) or close
        for lot in position.get("lots", []):
            qty = int(lot.get("qty", 0))
            if qty <= 0:
                continue
            lot_id = str(lot.get("lot_id") or f"{symbol}:{lot.get('date')}:{lot.get('price')}")
            snapshot = lot.get("strategy_snapshot") or {}
            follow_rule = _rule_for_lot(data_dir, lot, account_id)
            buy_price = _positive(lot.get("price")) or _positive(lot.get("avg_cost")) or close
            item = state.setdefault(lot_id, {
                "high_water_price": buy_price,
                "holding_days": 0,
                "last_evaluated_trade_date": None,
                "pending_fingerprint": None,
                "ignored_fingerprint": None,
                "action_id": None,
                "last_attempt_trade_date": None,
            })
            old_high = _positive(item.get("high_water_price")) or buy_price
            item["high_water_price"] = max(old_high, high)
            if trade_date > str(lot.get("date") or "") and item.get("last_evaluated_trade_date") != trade_date:
                item["holding_days"] = int(item.get("holding_days", 0)) + 1
            item["last_evaluated_trade_date"] = trade_date
            changed = True

            reason = None
            hit_signals: list[str] = []
            if trade_date > str(lot.get("date") or ""):
                risk_lines: list[tuple[float, str]] = []
                stop_loss = _abs_pct(snapshot.get("stop_loss"))
                if stop_loss is not None:
                    risk_lines.append((buy_price * (1 - stop_loss), "stop_loss"))
                trailing_stop = _abs_pct(snapshot.get("trailing_stop"))
                if trailing_stop is not None:
                    risk_lines.append((old_high * (1 - trailing_stop), "trailing_stop"))
                activate = _abs_pct(snapshot.get("trailing_take_profit_activate"))
                drawdown = _abs_pct(snapshot.get("trailing_take_profit_drawdown"))
                if activate is not None and drawdown is not None and old_high > buy_price and old_high / buy_price - 1 >= activate:
                    risk_lines.append((old_high * (1 - drawdown), "trailing_take_profit"))
                triggered = [entry for entry in risk_lines if low <= entry[0]]
                if triggered:
                    _, reason = max(triggered, key=lambda entry: entry[0])
                if reason is None:
                    take_profit = _abs_pct(snapshot.get("take_profit"))
                    if take_profit is not None and high >= buy_price * (1 + take_profit):
                        reason = "take_profit"
                daily_managed = lot.get("strategy_id") in (daily_strategy_ids or set())
                if reason is None and not daily_managed:
                    hit_signals = [
                        signal for signal in snapshot.get("exit_signals") or []
                        if bool(row.get(signal) or row.get(f"signal_{signal}"))
                    ]
                    if hit_signals:
                        reason = "signal"
                if reason is None and not daily_managed:
                    try:
                        max_hold_days = int(snapshot.get("max_hold_days")) if snapshot.get("max_hold_days") is not None else None
                    except (TypeError, ValueError):
                        max_hold_days = None
                    if max_hold_days is not None and int(item["holding_days"]) >= max_hold_days:
                        reason = "max_hold"
            fingerprint = None if reason is None else f"{reason}:{','.join(sorted(hit_signals))}"
            if fingerprint is None:
                item["pending_fingerprint"] = None
                item["action_id"] = None
                item["ignored_fingerprint"] = None
                item["last_attempt_trade_date"] = None
                continue
            if fingerprint == item.get("ignored_fingerprint"):
                continue
            if fingerprint == item.get("pending_fingerprint") and item.get("action_id"):
                continue
            if (
                fingerprint == item.get("pending_fingerprint")
                and item.get("last_attempt_trade_date") == trade_date
            ):
                continue
            pending_action = _pending_action(data_dir, lot_id, account_id)
            if pending_action is not None:
                item["pending_fingerprint"] = fingerprint
                item["action_id"] = pending_action[1]["id"]
                changed = True
                continue
            item["pending_fingerprint"] = fingerprint
            item["action_id"] = None
            item["last_attempt_trade_date"] = trade_date
            available = min(qty, remaining_available.get(symbol, 0))
            available = paper.normalize_qty(available)
            if available <= 0:
                continue
            remaining_available[symbol] = max(0, remaining_available.get(symbol, 0) - available)
            source = f"exit:{lot_id}"
            action = None
            auto_exit_enabled = bool(follow_rule and follow_rule.get("auto_exit_enabled", paper.DEFAULT_AUTO_EXIT_ENABLED))
            exit_fill = follow_rule.get("exit_fill", paper.DEFAULT_EXIT_FILL) if follow_rule else paper.DEFAULT_EXIT_FILL
            order_type = (
                "market" if reason in ("stop_loss", "trailing_stop", "trailing_take_profit", "take_profit")
                else "close" if exit_fill == "close_t" else "next_open"
            )
            if auto_exit_enabled:
                order, error = paper.create_order(
                    data_dir, symbol, "sell", account_id=account_id, qty=available,
                    order_type=order_type, ref_price=close, source=source,
                    strategy_id=lot.get("strategy_id"), strategy_snapshot=snapshot,
                    buy_score=lot.get("buy_score"), exit_reason=reason, intent_id=None, target_lot_id=lot_id,
                    auto_rule_id=follow_rule.get("id") if follow_rule else None,
                    automation_source="strategy_exit",
                    strategy_scope_id=lot.get("strategy_id"),
                    max_positions=follow_rule.get("max_positions") if follow_rule else None,
                    max_total_position=(float(follow_rule.get("max_total_position", 100)) / 100) if follow_rule else None,
                )
                if error:
                    action = {"error": error}
                elif order is not None:
                    action = order
                    item["action_id"] = order["id"]
            else:
                intent = paper_intents.create(
                    data_dir, account_id=account_id, symbol=symbol, side="sell", qty=available,
                    order_type=order_type, ref_price=close, source=source,
                    reason=_REASON_LABELS.get(reason, reason), strategy_id=lot.get("strategy_id"),
                    strategy_snapshot=snapshot, buy_score=lot.get("buy_score"), exit_reason=reason, lot_id=lot_id,
                    auto_rule_id=follow_rule.get("id") if follow_rule else None,
                    automation_source="strategy_exit",
                    strategy_scope_id=lot.get("strategy_id"),
                    max_positions=follow_rule.get("max_positions") if follow_rule else None,
                    max_total_position=(float(follow_rule.get("max_total_position", 100)) / 100) if follow_rule else None,
                )
                action = intent
                item["action_id"] = intent["id"]
            events.append({
                "ts": event_ts,
                "rule_id": f"paper_exit_{lot_id}",
                "rule_name": f"模拟盘持仓退出 · {symbol}",
                "position_id": lot_id,
                "account_id": account_id,
                "strategy_id": lot.get("strategy_id"),
                "source": "paper_exit",
                "type": "position_exit",
                "exit_reason": reason,
                "symbol": symbol,
                "name": symbol,
                "message": f"模拟盘持仓触发{_REASON_LABELS.get(reason, reason)}，" + ("已自动下卖单" if auto_exit_enabled else "等待确认卖出"),
                "price": close,
                "change_pct": row.get("change_pct"),
                "signals": hit_signals,
                "severity": "warn",
                "conditions": [],
                "logic": "or",
                "action": action,
            })
    if changed:
        _save_state(data_dir, account_id, state)
    return events


def continue_holding(data_dir: Path, lot_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    with paper.PAPER_LOCK:
        state = _load_state(data_dir, account_id)
        item = state.get(lot_id)
        if item is None:
            return None
        for order in paper.load_orders(data_dir, account_id):
            if order.get("status") == "pending" and order.get("side") == "sell" and str(order.get("target_lot_id") or "") == str(lot_id):
                order["status"] = "cancelled"
                order["reason"] = "继续持有"
                paper.save_order(data_dir, order, account_id)
        for intent in paper_intents.load(data_dir, account_id):
            if intent.get("status") == "pending" and intent.get("side") == "sell" and str(intent.get("lot_id") or "") == str(lot_id):
                paper_intents.resolve(data_dir, intent, "rejected", "继续持有", account_id)
        item["ignored_fingerprint"] = item.get("pending_fingerprint")
        item["pending_fingerprint"] = None
        item["action_id"] = None
        item["last_attempt_trade_date"] = None
        _save_state(data_dir, account_id, state)
        return item
