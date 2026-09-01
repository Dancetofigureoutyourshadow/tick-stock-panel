"""Interactive single-stock Chan theory training service."""
from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from app.market_time import cn_today
from app.services.chan_market_data import validate_kline_quality
from app.services.chan_structure import IncrementalChanAnalyzer
from app.services.json_report_store import JsonReportStore

CONTEXT_BARS = 60
DECISION_BARS = 60
MIN_BARS = CONTEXT_BARS + DECISION_BARS + 1
TRAINING_CONTEXT_YEARS = 2
TRAINING_FUTURE_CALENDAR_DAYS = 180
TRAINING_QUERY_PADDING_DAYS = 7
INITIAL_CAPITAL = 100_000.0
FEES_PCT = 0.0002
STAMP_TAX_PCT = 0.0005
SLIPPAGE_BPS = 5.0

RECORD_DETAIL_FIELDS = (
    "training_id",
    "status",
    "symbol",
    "name",
    "seed",
    "start_date",
    "end_date",
    "bars",
    "actions",
    "structure_snapshots",
    "equity_curve",
    "summary",
    "cost_model",
    "analysis",
    "data_version",
    "training_plan",
    "replayed_from",
    "chan_algorithm_source",
    "chan_engine",
    "chan_signal_profile",
    "chan_signal_profile_id",
    "chan_event_profile",
    "chan_segments_source",
    "created_at",
    "finished_at",
    "id",
)
RECORD_LIST_FIELDS = tuple(
    field
    for field in RECORD_DETAIL_FIELDS
    if field not in {
        "bars",
        "actions",
        "structure_snapshots",
        "equity_curve",
        "training_plan",
    }
)

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_record_store = JsonReportStore("chan_training_records.json", 200, "ctr")
_report_store = JsonReportStore("chan_training_reports.json", 200, "ctar")


def _date_text(value: Any) -> str:
    return value.isoformat()[:10] if hasattr(value, "isoformat") else str(value)[:10]


def _is_eligible_name(name: str) -> bool:
    upper = name.strip().upper().replace("*", "").replace(" ", "")
    blocked = ("风险", "退", "暂停上市", "终止上市")
    return bool(upper) and not upper.startswith(("ST", "SST", "PT")) and not any(item in upper for item in blocked)


def _load_rows(
    repo: Any,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    query_start = start or date(1990, 1, 1)
    query_end = end or cn_today()
    columns = [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "signal_limit_up",
        "signal_limit_down",
    ]
    batch_loader = getattr(repo, "get_daily_batch", None)
    if callable(batch_loader):
        # The batch path computes only the requested limit signals instead of
        # falling back to the complete enriched-indicator pipeline.
        frame = batch_loader([symbol], query_start, query_end, columns=columns)
    else:
        frame = repo.get_daily_asset(
            "stock", symbol, query_start, query_end, columns=columns,
        )
    if frame.is_empty():
        return []
    rows: list[dict[str, Any]] = []
    for raw in frame.sort("date").to_dicts():
        try:
            row = {
                "symbol": symbol,
                "date": _date_text(raw["date"]),
                "open": float(raw["open"]),
                "high": float(raw["high"]),
                "low": float(raw["low"]),
                "close": float(raw["close"]),
                "volume": float(raw.get("volume") or 0),
                "signal_limit_up": raw.get("signal_limit_up") is True,
                "signal_limit_down": raw.get("signal_limit_down") is True,
            }
        except (KeyError, TypeError, ValueError):
            continue
        if row["low"] > 0 and row["high"] >= row["low"] and row["close"] > 0:
            rows.append(row)
    return rows


def _training_query_range(
    listing_date: Any,
    rng: random.Random,
    today: date | None = None,
) -> tuple[date, date, date] | None:
    upper_date = today or cn_today()
    try:
        listed = date.fromisoformat(_date_text(listing_date))
    except (TypeError, ValueError):
        listed = date(1990, 1, 1)
    first_anchor = listed
    last_anchor = upper_date - timedelta(days=TRAINING_FUTURE_CALENDAR_DAYS)
    if first_anchor > last_anchor:
        return None
    anchor = first_anchor + timedelta(
        days=rng.randint(0, (last_anchor - first_anchor).days),
    )
    return (
        _years_before(anchor, TRAINING_CONTEXT_YEARS)
        - timedelta(days=TRAINING_QUERY_PADDING_DAYS),
        anchor + timedelta(days=TRAINING_FUTURE_CALENDAR_DAYS),
        anchor,
    )


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _training_indices(
    rows: list[dict[str, Any]],
    anchor: date,
) -> tuple[int, int] | None:
    dated_rows = [date.fromisoformat(_date_text(row["date"])) for row in rows]
    eligible = [index for index, row_date in enumerate(dated_rows) if row_date <= anchor]
    if not eligible:
        return None
    start_index = eligible[-1]
    if start_index + DECISION_BARS + 1 >= len(rows):
        return None
    cutoff = _years_before(dated_rows[start_index], TRAINING_CONTEXT_YEARS)
    context_start = next(
        (index for index, row_date in enumerate(dated_rows) if row_date >= cutoff),
        0,
    )
    return context_start, start_index


def _compact_training_window(state: dict[str, Any]) -> None:
    window_start = int(state["context_start"])
    window_end = int(state["decision_end"]) + 2
    state["rows"] = state["rows"][window_start:window_end]
    state["context_start"] = 0
    state["start_index"] -= window_start
    state["cursor"] -= window_start
    state["decision_end"] -= window_start
    state["analysis"]["daily_quality"] = validate_kline_quality(state["rows"])


def _with_limit_flags(
    repo: Any | None,
    symbol: str,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if repo is None or not rows or all(
        "signal_limit_up" in row and "signal_limit_down" in row
        for row in rows
    ):
        return rows
    try:
        start = date.fromisoformat(_date_text(rows[0]["date"]))
        end = date.fromisoformat(_date_text(rows[-1]["date"]))
        frame = repo.get_daily_asset(
            "stock",
            symbol,
            start,
            end,
            columns=["date", "signal_limit_up", "signal_limit_down"],
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return rows
    if frame.is_empty():
        return rows
    flags_by_date = {
        _date_text(item["date"]): (
            item.get("signal_limit_up") is True,
            item.get("signal_limit_down") is True,
        )
        for item in frame.to_dicts()
        if item.get("date") is not None
    }
    return [
        {
            **row,
            "signal_limit_up": flags_by_date.get(_date_text(row["date"]), (False, False))[0],
            "signal_limit_down": flags_by_date.get(_date_text(row["date"]), (False, False))[1],
        }
        for row in rows
    ]


def _new_state(
    symbol: str,
    name: str,
    rows: list[dict[str, Any]],
    seed: int,
    rng: random.Random,
    cost_model: dict[str, float] | None = None,
    *,
    start_index: int | None = None,
    context_start: int | None = None,
) -> dict[str, Any]:
    if start_index is None:
        start_index = rng.randint(CONTEXT_BARS - 1, len(rows) - DECISION_BARS - 2)
    if context_start is None:
        context_start = max(0, start_index - CONTEXT_BARS + 1)
    if not 0 <= context_start <= start_index:
        raise ValueError("训练上下文范围无效")
    if start_index + DECISION_BARS + 1 >= len(rows):
        raise ValueError("训练题目右侧不足 60 根日 K")
    costs = cost_model or {
        "commission_pct": FEES_PCT,
        "stamp_tax_pct": STAMP_TAX_PCT,
        "slippage_bps": SLIPPAGE_BPS,
    }
    return {
        "id": uuid.uuid4().hex,
        "status": "active",
        "symbol": symbol,
        "name": name,
        "seed": seed,
        "rows": rows,
        "context_start": context_start,
        "start_index": start_index,
        "cursor": start_index,
        "decision_end": start_index + DECISION_BARS,
        "cash": INITIAL_CAPITAL,
        "lots": [],
        "actions": [],
        "structure_snapshots": [],
        "equity_curve": [],
        "peak_equity": INITIAL_CAPITAL,
        "max_drawdown": 0.0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "cost_model": costs,
        "analysis": {
            "analysis_mode": "daily_recursive",
            "daily_quality": validate_kline_quality(rows),
            "data_warnings": [],
        },
    }


def _current_row(state: dict[str, Any]) -> dict[str, Any]:
    return state["rows"][state["cursor"]]


def _shares(state: dict[str, Any]) -> int:
    return sum(int(lot["shares"]) for lot in state["lots"])


def _available_shares(state: dict[str, Any]) -> int:
    cursor = state["cursor"]
    return sum(int(lot["shares"]) for lot in state["lots"] if lot["buy_index"] < cursor)


def _equity(state: dict[str, Any], price: float | None = None) -> float:
    close = price if price is not None else float(_current_row(state)["close"])
    return float(state["cash"] + _shares(state) * close)


def _mark_equity(state: dict[str, Any]) -> None:
    row = _current_row(state)
    value = _equity(state, float(row["close"]))
    if state["equity_curve"] and state["equity_curve"][-1]["date"] == row["date"]:
        state["equity_curve"][-1]["value"] = round(value, 4)
    else:
        state["equity_curve"].append({"date": row["date"], "value": round(value, 4)})
    state["peak_equity"] = max(float(state["peak_equity"]), value)
    drawdown = (state["peak_equity"] - value) / state["peak_equity"] if state["peak_equity"] else 0.0
    state["max_drawdown"] = max(float(state["max_drawdown"]), drawdown)


def _visible_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    # Return the full revealed prefix; dataZoom controls the chart viewport.
    # Keep the history before the decision point available for review.
    return state["rows"][:state["cursor"] + 1]


def _public_analysis(state: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    analysis = {
        "analysis_mode": "daily_recursive",
        "daily_quality": state.get("analysis", {}).get("daily_quality"),
        "data_warnings": state.get("analysis", {}).get("data_warnings", []),
    }
    if full or state.get("status") != "active":
        return analysis
    analysis["daily_quality"] = validate_kline_quality(
        state["rows"][state["context_start"]:state["cursor"] + 1],
    )
    return analysis


def _record_daily_analysis(record: dict[str, Any]) -> dict[str, Any]:
    stored = record.get("analysis", {})
    return {
        "analysis_mode": "daily_recursive",
        "daily_quality": stored.get("daily_quality")
        or validate_kline_quality(record.get("bars", [])),
        "data_warnings": [],
    }


def _position(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "shares": _shares(state),
        "available_shares": _available_shares(state),
        "market_value": round(_shares(state) * float(_current_row(state)["close"]), 4),
    }


def _combined_structure_snapshot(
    state: dict[str, Any],
    *,
    include_replay: bool = False,
) -> dict[str, Any]:
    runtime = state["_chan_runtime"]
    return runtime.snapshot(include_replay=include_replay)


def _capture_structure(state: dict[str, Any]) -> dict[str, Any]:
    runtime = state.get("_chan_runtime")
    if not isinstance(runtime, IncrementalChanAnalyzer):
        runtime = IncrementalChanAnalyzer(level="1d")
        state["_chan_runtime"] = runtime
        state["_chan_primary_cursor"] = -1

    primary_cursor = int(state.get("_chan_primary_cursor", -1))
    for source_index in range(primary_cursor + 1, state["cursor"] + 1):
        runtime.update_primary(state["rows"][source_index], source_index)
        state["_chan_primary_cursor"] = source_index

    chan = _combined_structure_snapshot(state)
    revealed = max(0, state["cursor"] - state["start_index"])
    snapshots = state["structure_snapshots"]
    if not snapshots or snapshots[-1]["revealed"] != revealed:
        snapshots.append({
            "revealed": revealed,
            "date": _current_row(state)["date"],
            "structure": chan,
        })
    return chan


def _snapshot(state: dict[str, Any], include_rows: bool = True) -> dict[str, Any]:
    rows = _visible_rows(state)
    chan = _capture_structure(state)
    out = {
        "id": state["id"],
        "status": state["status"],
        "symbol": state["symbol"],
        "name": state["name"],
        "progress": {"revealed": max(0, state["cursor"] - state["start_index"]), "total": DECISION_BARS},
        "can_next": state["status"] == "active" and state["cursor"] < state["decision_end"],
        "current_price": float(_current_row(state)["close"]),
        "cash": round(float(state["cash"]), 4),
        "position": _position(state),
        "equity": round(_equity(state), 4),
        "return_pct": round((_equity(state) / INITIAL_CAPITAL - 1) * 100, 4),
        "max_drawdown_pct": round(float(state["max_drawdown"]) * 100, 4),
        "cost_model": state["cost_model"],
        "analysis": _public_analysis(state),
        "replayed_from": state.get("replayed_from"),
        "actions": state["actions"],
        "chan": chan,
    }
    if include_rows:
        out["rows"] = rows
    return out


def _record_action(state: dict[str, Any], action: dict[str, Any]) -> None:
    runtime = state.get("_chan_runtime")
    if isinstance(runtime, IncrementalChanAnalyzer):
        structure = _combined_structure_snapshot(state)
        current_date = str(_current_row(state)["date"])
        action["chan_context"] = {
            "signals": [
                point for point in structure.get("points", [])
                if str(point.get("date", ""))[:10] == current_date[:10]
            ],
            "matched_events": [
                event for event in structure.get("events", [])
                if str(event.get("date", ""))[:10] == current_date[:10]
            ],
            "event_profile": structure.get("event_profile", []),
        }
    state["actions"].append(action)
    _mark_equity(state)


def _buy(state: dict[str, Any], percentage: float, trigger: str = "user") -> dict[str, Any]:
    row = _current_row(state)
    reference = float(row["close"])
    cost_model = state["cost_model"]
    commission_rate = float(cost_model["commission_pct"])
    slippage_rate = float(cost_model["slippage_bps"]) / 10000
    before_cash = float(state["cash"])
    budget = before_cash * percentage / 100
    shares = math.floor(budget / (reference * (1 + commission_rate + slippage_rate)) / 100) * 100
    if shares < 100:
        raise ValueError("当前现金比例不足以买入 100 股")
    gross = shares * reference
    fee = gross * commission_rate
    slippage = gross * slippage_rate
    total = gross + fee + slippage
    state["cash"] = before_cash - total
    state["lots"].append({
        "shares": shares,
        "buy_index": state["cursor"],
        "buy_date": row["date"],
        "cost": total,
    })
    action = {
        "side": "buy", "trigger": trigger, "percentage": percentage,
        "revealed": max(0, state["cursor"] - state["start_index"]),
        "date": row["date"], "reference_price": reference, "execution_price": reference,
        "shares": shares, "gross": gross, "commission": fee, "stamp_tax": 0.0,
        "slippage_amount": slippage,
        "cash_before": before_cash, "cash_after": state["cash"],
        "position_after": _shares(state), "pnl_amount": None,
    }
    _record_action(state, action)
    return action


def _sell(state: dict[str, Any], percentage: float, trigger: str = "user") -> dict[str, Any]:
    row = _current_row(state)
    reference = float(row["close"])
    cost_model = state["cost_model"]
    commission_rate = float(cost_model["commission_pct"])
    stamp_rate = float(cost_model["stamp_tax_pct"])
    slippage_rate = float(cost_model["slippage_bps"]) / 10000
    before_cash = float(state["cash"])
    available = _available_shares(state)
    shares = math.floor(available * percentage / 100 / 100) * 100
    if shares < 100:
        raise ValueError("当前没有可卖出的 T+1 持仓")

    remaining = shares
    cost_basis = 0.0
    for lot in state["lots"]:
        if remaining <= 0 or lot["buy_index"] >= state["cursor"]:
            continue
        lot_shares = int(lot["shares"])
        take = min(remaining, lot_shares)
        lot_cost = float(lot["cost"])
        sold_cost = lot_cost * take / lot_shares
        cost_basis += sold_cost
        lot["shares"] = lot_shares - take
        # Keep the cost basis attached to the unsold shares. Without this,
        # a later partial/automatic liquidation charges the original whole
        # lot again and corrupts realized P&L in the saved training record.
        lot["cost"] = max(0.0, lot_cost - sold_cost)
        remaining -= take
    state["lots"] = [lot for lot in state["lots"] if lot["shares"] > 0]
    gross = shares * reference
    fee = gross * commission_rate
    stamp = gross * stamp_rate
    slippage = gross * slippage_rate
    net = gross - fee - stamp - slippage
    state["cash"] = before_cash + net
    action = {
        "side": "sell", "trigger": trigger, "percentage": percentage,
        "revealed": max(0, state["cursor"] - state["start_index"]),
        "date": row["date"], "reference_price": reference, "execution_price": reference,
        "shares": shares, "gross": gross, "commission": fee, "stamp_tax": stamp,
        "slippage_amount": slippage,
        "cash_before": before_cash, "cash_after": state["cash"],
        "position_after": _shares(state), "pnl_amount": net - cost_basis,
    }
    _record_action(state, action)
    return action


def _summary(state: dict[str, Any]) -> dict[str, Any]:
    final_equity = _equity(state)
    return {
        "final_equity": round(final_equity, 4),
        "total_return_pct": round((final_equity / INITIAL_CAPITAL - 1) * 100, 4),
        "realized_pnl": round(sum(float(item["pnl_amount"] or 0) for item in state["actions"]), 4),
        "max_drawdown_pct": round(float(state["max_drawdown"]) * 100, 4),
        "trade_count": len(state["actions"]),
        "sell_count": sum(1 for item in state["actions"] if item["side"] == "sell"),
        "win_rate_pct": round(
            100 * sum(1 for item in state["actions"] if item["side"] == "sell" and (item["pnl_amount"] or 0) > 0)
            / max(1, sum(1 for item in state["actions"] if item["side"] == "sell")), 4,
        ),
    }


def _saved_record(state: dict[str, Any]) -> dict[str, Any]:
    latest_structure = (
        state["structure_snapshots"][-1].get("structure", {})
        if state["structure_snapshots"]
        else {}
    )
    visible_bars = _visible_rows(state)
    plan_row_count = min(len(state["rows"]), state["decision_end"] + 2)
    plan_rows = state["rows"][:plan_row_count]
    training_rows_sha256 = _training_rows_digest(plan_rows)
    visible_rows_sha256 = _training_rows_digest(visible_bars)
    analysis = _public_analysis(state, full=True)
    return {
        "training_id": state["id"],
        "status": "finished",
        "symbol": state["symbol"],
        "name": state["name"],
        "seed": state["seed"],
        "start_date": state["rows"][state["context_start"]]["date"],
        "end_date": _current_row(state)["date"],
        "bars": visible_bars,
        "actions": state["actions"],
        "structure_snapshots": state["structure_snapshots"],
        "equity_curve": state["equity_curve"],
        "summary": _summary(state),
        "cost_model": state["cost_model"],
        "analysis": analysis,
        "data_version": {
            "schema": "chan-training-data",
            "training_rows_sha256": training_rows_sha256,
            "visible_rows_sha256": visible_rows_sha256,
            "training_row_count": len(plan_rows),
            "visible_row_count": len(visible_bars),
            "first_bar_at": str(plan_rows[0]["date"]) if plan_rows else None,
            "last_bar_at": str(plan_rows[-1]["date"]) if plan_rows else None,
            "visible_last_bar_at": str(visible_bars[-1]["date"]) if visible_bars else None,
        },
        "training_plan": {
            "schema": "chan-training-plan",
            "symbol": state["symbol"],
            "name": state["name"],
            "seed": state["seed"],
            "context_start": state["context_start"],
            "start_index": state["start_index"],
            "decision_end": state["decision_end"],
            "row_count": plan_row_count,
            "remaining_rows": plan_rows[len(visible_bars):],
            "row_digest": training_rows_sha256,
            "initial_capital": INITIAL_CAPITAL,
            "cost_model": state["cost_model"],
        },
        "replayed_from": state.get("replayed_from"),
        "chan_algorithm_source": latest_structure.get("algorithm_source"),
        "chan_engine": latest_structure.get("engine"),
        "chan_signal_profile": latest_structure.get("signal_profile", {}),
        "chan_signal_profile_id": latest_structure.get("signal_profile_id"),
        "chan_event_profile": latest_structure.get("event_profile", []),
        "chan_segments_source": latest_structure.get("segments_source"),
        "created_at": state["created_at"],
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


def _training_rows_digest(rows: list[dict[str, Any]]) -> str:
    values = [
        {
            key: row.get(key)
            for key in ("symbol", "date", "open", "high", "low", "close", "volume")
        }
        for row in rows
    ]
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def create_session(
    repo: Any,
    cost_model: dict[str, float] | None = None,
) -> dict[str, Any]:
    instruments = repo.get_instruments()
    if instruments.is_empty() or "symbol" not in instruments.columns:
        raise ValueError("暂无可用股票标的")
    candidates = []
    candidate_columns = [
        column for column in ["symbol", "name", "listing_date"]
        if column in instruments.columns
    ]
    for item in instruments.select(candidate_columns).to_dicts():
        symbol = str(item.get("symbol") or "").strip()
        name = str(item.get("name") or symbol).strip()
        if symbol and _is_eligible_name(name):
            candidates.append((symbol, name, item.get("listing_date")))
    seed = random.SystemRandom().randint(0, 2**63 - 1)
    rng = random.Random(seed)
    rng.shuffle(candidates)
    for symbol, name, listing_date in candidates:
        query_range = _training_query_range(listing_date, rng)
        if query_range is None:
            continue
        query_start, query_end, anchor = query_range
        rows = _load_rows(repo, symbol, query_start, query_end)
        indices = _training_indices(rows, anchor)
        if indices is None:
            continue
        context_start, start_index = indices
        state = _new_state(
            symbol,
            name,
            rows,
            seed,
            rng,
            cost_model,
            start_index=start_index,
            context_start=context_start,
        )
        training_window = rows[state["context_start"]:state["decision_end"] + 2]
        if not validate_kline_quality(training_window)["valid"]:
            continue
        _compact_training_window(state)
        _mark_equity(state)
        with _lock:
            _sessions[state["id"]] = state
        return _snapshot(state)
    raise ValueError("暂无满足训练条件的股票日 K 数据,请先同步更多历史数据")


def _stored_record(training_id: str) -> dict[str, Any]:
    for record in _record_store.list_reports():
        if record.get("training_id") == training_id or record.get("id") == training_id:
            return record
    raise KeyError("训练记录不存在")


def replay_record(
    repo: Any,
    training_id: str,
) -> dict[str, Any]:
    """Create a fresh session from the exact serialized historical question."""
    record = _stored_record(training_id)
    plan = record.get("training_plan")
    if not isinstance(plan, dict) or plan.get("schema") != "chan-training-plan":
        raise ValueError("该训练记录不包含可重练方案")
    row_count = int(plan.get("row_count") or 0)
    visible = [dict(row) for row in record.get("bars", [])]
    rows = visible[:row_count]
    if len(rows) < row_count:
        rows.extend(dict(row) for row in plan.get("remaining_rows", []))
        rows = rows[:row_count]
    if len(rows) != row_count or _training_rows_digest(rows) != plan.get("row_digest"):
        raise ValueError("训练方案 K 线不完整或校验失败")
    rows = _with_limit_flags(repo, str(plan["symbol"]), rows)
    seed = int(plan["seed"])
    plan_start_index = int(plan["start_index"])
    plan_context_start = int(plan["context_start"])
    state = _new_state(
        str(plan["symbol"]),
        str(plan.get("name") or plan["symbol"]),
        rows,
        seed,
        random.Random(seed),
        dict(plan.get("cost_model") or record.get("cost_model") or {}),
        start_index=plan_start_index,
        context_start=plan_context_start,
    )
    state["context_start"] = plan_context_start
    state["start_index"] = plan_start_index
    state["cursor"] = state["start_index"]
    state["decision_end"] = int(plan["decision_end"])
    state["replayed_from"] = str(record.get("training_id") or training_id)
    _mark_equity(state)
    with _lock:
        _sessions[state["id"]] = state
    return _snapshot(state)


def validate_random_samples(repo: Any, count: int = 5) -> dict[str, Any]:
    """Analyze random local stocks without creating sessions or persisted records."""
    instruments = repo.get_instruments()
    if instruments.is_empty() or "symbol" not in instruments.columns:
        raise ValueError("暂无可校验股票标的")
    candidates = [
        (str(item.get("symbol") or "").strip(), str(item.get("name") or item.get("symbol") or "").strip())
        for item in instruments.select([c for c in ["symbol", "name"] if c in instruments.columns]).to_dicts()
    ]
    candidates = [item for item in candidates if item[0] and _is_eligible_name(item[1])]
    random.SystemRandom().shuffle(candidates)
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    for symbol, name in candidates:
        if len(results) >= count:
            break
        rows = _load_rows(repo, symbol)
        if len(rows) < MIN_BARS:
            continue
        sample_started = time.perf_counter()
        runtime = IncrementalChanAnalyzer(level="1d")
        for source_index, row in enumerate(rows):
            runtime.update_primary(row, source_index)
        structure = runtime.snapshot()
        dates = {str(row["date"]) for row in rows}
        issues: list[str] = []
        for key in ("fractals", "points"):
            if any(str(item["date"])[:10] not in dates for item in structure[key]):
                issues.append(f"{key}: annotation date missing from source bars")
        for key in ("strokes", "segments", "centers"):
            if any(
                str(item["start_date"])[:10] not in dates
                or str(item["end_date"])[:10] not in dates
                for item in structure[key]
            ):
                issues.append(f"{key}: range date missing from source bars")
        if any(
            int(item.get("confirmed_at", 0)) >= len(rows)
            for key in ("fractals", "strokes", "segments", "centers", "points")
            for item in structure[key]
        ):
            issues.append("future confirmation index")
        results.append({
            "symbol": symbol,
            "name": name,
            "bars": len(rows),
            "elapsed_ms": round((time.perf_counter() - sample_started) * 1000, 2),
            "summary": structure["summary"],
            "quality": validate_kline_quality(rows),
            "issues": issues,
            "passed": not issues,
        })
    if not results:
        raise ValueError("暂无满足校验条件的本地日 K 数据")
    return {
        "requested": count,
        "checked": len(results),
        "passed": sum(1 for item in results if item["passed"]),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "persisted": False,
        "results": results,
    }


def get_session(session_id: str) -> dict[str, Any]:
    with _lock:
        state = _sessions.get(session_id)
        if state is None:
            raise KeyError("训练 session 不存在或已失效")
        return state


def snapshot_session(session_id: str) -> dict[str, Any]:
    with _lock:
        return _snapshot(get_session(session_id))


def discard_session(session_id: str) -> bool:
    """Discard an unfinished session without creating a training record."""
    with _lock:
        state = _sessions.get(session_id)
        if state is None:
            raise KeyError("训练 session 不存在或已失效")
        if state.get("status") != "active":
            raise ValueError("只能放弃未结束的训练")
        _sessions.pop(session_id, None)
        return True


def session_diagnostics(session_id: str) -> dict[str, Any]:
    with _lock:
        state = get_session(session_id)
        _capture_structure(state)
        return {
            "training_id": state["id"],
            "structure": _combined_structure_snapshot(state, include_replay=True),
            "data_quality": {
                "daily": _public_analysis(state).get("daily_quality"),
            },
        }


def next_bar(session_id: str) -> dict[str, Any]:
    with _lock:
        state = get_session(session_id)
        if state["status"] != "active":
            raise ValueError("训练已经结束")
        if state["cursor"] >= state["decision_end"]:
            raise ValueError("已经揭示完本题目")
        state["cursor"] += 1
        _mark_equity(state)
        return _snapshot(state)


def execute_action(session_id: str, side: str, percentage: float) -> dict[str, Any]:
    with _lock:
        state = get_session(session_id)
        if state["status"] != "active":
            raise ValueError("训练已经结束")
        if side not in {"buy", "sell"}:
            raise ValueError("side 必须是 buy 或 sell")
        if not math.isfinite(percentage) or percentage <= 0 or percentage > 100:
            raise ValueError("percentage 必须在 0 到 100 之间")
        action = _buy(state, percentage) if side == "buy" else _sell(state, percentage)
        return {"action": action, "session": _snapshot(state)}


def finish_session(session_id: str) -> dict[str, Any]:
    with _lock:
        state = get_session(session_id)
        if state["status"] != "active":
            raise ValueError("训练已经结束")
        if _shares(state):
            if _available_shares(state) < _shares(state):
                state["cursor"] = min(state["cursor"] + 1, len(state["rows"]) - 1)
                _mark_equity(state)
                _capture_structure(state)
            _sell(state, 100.0, trigger="auto_liquidation")
        state["status"] = "finished"
        record = _saved_record(state)
        saved = _record_store.save_report(record)
        current_structure = _combined_structure_snapshot(state)
        _sessions.pop(session_id, None)
        return {
            "session": _snapshot(state),
            "record": {**saved, "current_structure": current_structure},
        }


def list_records() -> list[dict[str, Any]]:
    return [
        {
            **{key: record[key] for key in RECORD_LIST_FIELDS if key in record},
            "analysis": _record_daily_analysis(record),
        }
        for record in _record_store.list_reports()
    ]


def _recompute_record_structure(
    record: dict[str, Any],
    *,
    include_replay: bool = False,
    repo: Any | None = None,
) -> dict[str, Any]:
    runtime = IncrementalChanAnalyzer(level="1d")
    for source_index, row in enumerate(record.get("bars", [])):
        runtime.update_primary(row, source_index)
    return runtime.snapshot(include_replay=include_replay)


def _recompute_action_structures(record: dict[str, Any]) -> list[dict[str, Any]]:
    action_dates = {
        str(action.get("date", ""))[:10]
        for action in record.get("actions", [])
        if action.get("date")
    }
    if not action_dates:
        return []
    runtime = IncrementalChanAnalyzer(level="1d")
    snapshots: list[dict[str, Any]] = []
    captured_dates: set[str] = set()
    for source_index, row in enumerate(record.get("bars", [])):
        runtime.update_primary(row, source_index)
        date_text = str(row.get("date", ""))[:10]
        if date_text in action_dates and date_text not in captured_dates:
            snapshots.append({"date": date_text, "structure": runtime.snapshot()})
            captured_dates.add(date_text)
    return snapshots


def get_record(training_id: str, repo: Any | None = None) -> dict[str, Any]:
    record = _stored_record(training_id)
    record = {
        **record,
        "analysis": _record_daily_analysis(record),
        "bars": _with_limit_flags(
            repo,
            str(record.get("symbol") or ""),
            [dict(row) for row in record.get("bars", [])],
        ),
    }
    reports = [
        item
        for item in _report_store.list_reports()
        if item.get("training_id") == record.get("training_id")
    ]
    current_structure = _recompute_record_structure(record, repo=repo)
    visible_record = {
        key: record[key]
        for key in RECORD_DETAIL_FIELDS
        if key in record
    }
    return {
        **visible_record,
        "ai_reports": reports,
        # Historical snapshots remain the audit trail for each
        # revealed bar; the detail chart must use the current CZSC
        # implementation so it cannot display stale algorithm output.
        "current_structure": current_structure,
        "chan_algorithm_source": current_structure.get("algorithm_source"),
        "chan_engine": current_structure.get("engine"),
        "chan_signal_profile": current_structure.get("signal_profile", {}),
        "chan_signal_profile_id": current_structure.get("signal_profile_id"),
        "chan_event_profile": current_structure.get("event_profile", []),
        "chan_segments_source": current_structure.get("segments_source"),
    }


def record_diagnostics(training_id: str, repo: Any | None = None) -> dict[str, Any]:
    record = get_record(training_id, repo)
    return {
        "training_id": record.get("training_id"),
        "structure": _recompute_record_structure(
            record,
            include_replay=True,
            repo=repo,
        ),
        "data_quality": {
            "daily": record.get("analysis", {}).get("daily_quality")
            or validate_kline_quality(record.get("bars", [])),
        },
    }


def delete_record(training_id: str) -> bool:
    target = next(
        (
            item for item in _record_store.list_reports()
            if item.get("training_id") == training_id or item.get("id") == training_id
        ),
        None,
    )
    if target is None:
        raise KeyError("训练记录不存在")
    record_id = str(target.get("id") or training_id)
    target_training_id = str(target.get("training_id") or training_id)
    removed = _record_store.delete_report(record_id)
    for report in _report_store.list_reports():
        if report.get("training_id") == target_training_id and report.get("id"):
            _report_store.delete_report(str(report["id"]))
    return removed


def record_store() -> JsonReportStore:
    return _report_store


def build_ai_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    import json
    current_structure = record.get("current_structure")
    if not isinstance(current_structure, dict):
        current_structure = _recompute_record_structure(record)
    system = (
        "你是一名缠论训练教练。请根据用户一次历史训练中的真实操作和缠论结构快照，"  # noqa: RUF001
        "分析交易习惯与训练表现。输出中文 Markdown，包含仓位管理、买卖时机、规则执行、"  # noqa: RUF001
        "缠论结构偏差、做得好的地方、需要改进的地方和下一步练习建议。不要提供现实投资买卖指令。"
    )
    payload = {
        "symbol": record.get("symbol"),
        "period": [record.get("start_date"), record.get("end_date")],
        "actions": record.get("actions", []),
        "summary": record.get("summary", {}),
        "cost_model": record.get("cost_model", {}),
        "analysis": record.get("analysis", {}),
        "chan_algorithm_source": current_structure.get("algorithm_source"),
        "chan_signal_profile": current_structure.get("signal_profile", {}),
        "chan_signal_profile_id": current_structure.get("signal_profile_id"),
        "chan_event_profile": current_structure.get("event_profile", []),
        "visible_bars": record.get("bars", []),
        "current_structure": current_structure,
        "action_structures": _recompute_action_structures(record),
    }
    return [{"role": "system", "content": system}, {
        "role": "user", "content": "训练记录(JSON)：\n" + json.dumps(payload, ensure_ascii=False),  # noqa: RUF001
    }]
