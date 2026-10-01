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
    reason: str | None = None,
    strategy_id: str | None = None,
    strategy_snapshot: dict | None = None,
    buy_score: float | str | None = None,
    lot_id: str | None = None,
    position_symbol: str | None = None,
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
        "reason": reason,
        "strategy_id": strategy_id,
        "strategy_snapshot": strategy_snapshot or {},
        "buy_score": buy_score,
        "lot_id": lot_id,
        "position_symbol": position_symbol or symbol,
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
