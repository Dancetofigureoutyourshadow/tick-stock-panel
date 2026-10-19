"""待确认交易意图。

意图不进入 Paper 订单或现金/持仓账本，只有确认时才调用 paper.create_order。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from app.market_time import cn_now
from app.services.fs_utils import atomic_write_text
from app.strategy import paper


def _dir(data_dir: Path, account_id: str) -> Path:
    d = paper._root(data_dir, account_id) / "intents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return cn_now().isoformat()


def _new_id() -> str:
    return f"intent_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def load(data_dir: Path, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> list[dict]:
    out: list[dict] = []
    for p in sorted(_dir(data_dir, account_id).glob("intent_*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda item: (item.get("created_at", ""), item.get("id", "")), reverse=True)


def get(data_dir: Path, intent_id: str, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict | None:
    p = _dir(data_dir, account_id) / f"{intent_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save(data_dir: Path, intent: dict, account_id: str = paper.DEFAULT_ACCOUNT_ID) -> dict:
    atomic_write_text(
        _dir(data_dir, account_id) / f"{intent['id']}.json",
        json.dumps(intent, ensure_ascii=False, indent=2),
    )
    return intent


def create(
    data_dir: Path,
    *,
    account_id: str,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    ref_price: float | None,
    source: str,
    automation_source: str | None = None,
    reason: str | None = None,
    strategy_id: str | None = None,
    strategy_snapshot: dict | None = None,
    buy_score: float | str | None = None,
    exit_reason: str | None = None,
    lot_id: str | None = None,
    position_symbol: str | None = None,
    auto_rule_id: str | None = None,
    strategy_scope_id: str | None = None,
    max_positions: int | None = None,
    max_total_position: float | None = None,
    confirmation: dict | None = None,
) -> dict:
    if side not in ("buy", "sell"):
        raise ValueError("意图方向非法")
    if order_type not in ("market", "next_open", "close"):
        raise ValueError("意图订单类型非法")
    if int(qty) <= 0 or int(qty) % paper.LOT_SIZE != 0:
        raise ValueError(f"数量必须是 {paper.LOT_SIZE} 的整数倍")
    intent = {
        "id": _new_id(),
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "qty": int(qty),
        "order_type": order_type,
        "ref_price": ref_price,
        "source": source,
        "automation_source": automation_source or ("manual" if source == "manual" else None),
        "reason": reason,
        "strategy_id": strategy_id,
        "strategy_snapshot": strategy_snapshot or {},
        "buy_score": buy_score,
        "exit_reason": exit_reason,
        "lot_id": lot_id,
        "position_symbol": position_symbol or symbol,
        "auto_rule_id": auto_rule_id,
        "strategy_scope_id": strategy_scope_id,
        "max_positions": max_positions,
        "max_total_position": max_total_position,
        "confirmation": confirmation,
        "status": "pending",
        "created_at": _now_iso(),
        "resolved_at": None,
        "resolution_reason": None,
    }
    return save(data_dir, intent, account_id)


def resolve(data_dir: Path, intent: dict, status: str, reason: str | None, account_id: str) -> dict:
    if status not in ("confirmed", "rejected", "expired"):
        raise ValueError("意图状态非法")
    intent["status"] = status
    intent["resolved_at"] = _now_iso()
    intent["resolution_reason"] = reason
    return save(data_dir, intent, account_id)


def confirm(
    data_dir: Path,
    intent_id: str,
    account_id: str,
    *,
    qty: int,
    order_type: str,
    ref_price: float | None,
    asset_type: str,
) -> tuple[dict | None, dict | None, str | None]:
    """原子确认意图，避免两个并发请求各自把同一意图转成订单。"""
    with paper.PAPER_LOCK:
        intent = get(data_dir, intent_id, account_id)
        if intent is None:
            return None, None, "待确认意图不存在"
        if intent.get("status") != "pending":
            return intent, None, f"意图状态为 {intent.get('status')}, 不可确认"
        if intent.get("daily_review_required"):
            return intent, None, "旧操作归属待核对，请拒绝该操作后重新扫描"
        if intent.get("confirmation"):
            from app.strategy.paper_daily import submission_error
            error = submission_error(intent["confirmation"], order_type, data_dir=data_dir)
            if error or order_type != intent["order_type"]:
                if error and "无法确认" not in error:
                    resolve(data_dir, intent, "expired", error, account_id)
                return intent, None, error or "确认批次不能更改成交方式"
        account = paper.get_account(data_dir, account_id)
        if account is not None and not account.get("queue_limit_orders"):
            limit_error = paper.limit_reference_error(
                data_dir, intent["symbol"], intent["side"], asset_type, ref_price,
            )
            if limit_error:
                intent["last_validation_error"] = limit_error
                save(data_dir, intent, account_id)
                return intent, None, limit_error
        order, error = paper.create_order(
            data_dir, intent["symbol"], intent["side"], account_id=account_id,
            qty=qty, order_type=order_type, asset_type=asset_type, ref_price=ref_price,
            source=intent.get("source", "confirmed-intent"),
            automation_source=intent.get("automation_source"),
            strategy_id=intent.get("strategy_id"),
            strategy_snapshot=intent.get("strategy_snapshot"),
            buy_score=intent.get("buy_score"), exit_reason=intent.get("exit_reason"), intent_id=intent_id,
            target_lot_id=intent.get("lot_id"),
            auto_rule_id=intent.get("auto_rule_id"),
            strategy_scope_id=intent.get("strategy_scope_id"),
            max_positions=intent.get("max_positions"),
            max_total_position=intent.get("max_total_position"),
            confirmation=intent.get("confirmation"),
        )
        if error:
            intent["last_validation_error"] = error
            save(data_dir, intent, account_id)
            return intent, None, error
        intent.pop("last_validation_error", None)
        resolve(data_dir, intent, "confirmed", f"订单 {order['id']}", account_id)
        return intent, order, None


def reject(
    data_dir: Path,
    intent_id: str,
    account_id: str,
    reason: str = "用户拒绝",
) -> tuple[dict | None, str | None]:
    """原子拒绝意图，避免与确认请求交叉写入导致状态回退。"""
    with paper.PAPER_LOCK:
        intent = get(data_dir, intent_id, account_id)
        if intent is None:
            return None, "待确认意图不存在"
        if intent.get("status") != "pending":
            return intent, f"意图状态为 {intent.get('status')}, 不可拒绝"
        return resolve(data_dir, intent, "rejected", reason, account_id), None
