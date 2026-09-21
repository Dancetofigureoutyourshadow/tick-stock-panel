"""stock-sdk 行业/概念板块即时资金流。

stock-sdk 的 ``fundFlow.sectorRank`` 返回当前交易日板块累计主力净流入。
接口本身不提供分钟历史，因此本服务按轮询时间保存快照，形成当天
09:30-15:00 的净流入序列；不读取个股资金流，也不按成分股聚合。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.plugins.stocksdk import bridge

logger = logging.getLogger(__name__)

_POLL_TTL = 25.0
_MAX_HISTORY = 400
_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()
_BJ = ZoneInfo("Asia/Shanghai")
_SOURCE = "stock_sdk_sector_rank"
_SESSION_OPEN = (9, 30)
_MORNING_CLOSE = (11, 30)
_AFTERNOON_OPEN = (13, 0)
_SESSION_CLOSE = (15, 0)


def _now() -> datetime:
    return datetime.now(_BJ)


def _session_bucket(value: datetime) -> str | None:
    current = (value.hour, value.minute)
    if current < _SESSION_OPEN:
        return None
    if current <= _MORNING_CLOSE or _AFTERNOON_OPEN <= current <= _SESSION_CLOSE:
        return f"{value.hour:02d}:{value.minute:02d}"
    if current > _SESSION_CLOSE:
        return "15:00"
    return None


def _session_buckets() -> list[str]:
    buckets: list[str] = []
    current = _SESSION_OPEN[0] * 60 + _SESSION_OPEN[1]
    morning_end = _MORNING_CLOSE[0] * 60 + _MORNING_CLOSE[1]
    while current <= morning_end:
        buckets.append(f"{current // 60:02d}:{current % 60:02d}")
        current += 1
    current = _AFTERNOON_OPEN[0] * 60 + _AFTERNOON_OPEN[1]
    afternoon_end = _SESSION_CLOSE[0] * 60 + _SESSION_CLOSE[1]
    while current <= afternoon_end:
        buckets.append(f"{current // 60:02d}:{current % 60:02d}")
        current += 1
    return buckets


_SESSION_ORDER = {bucket: index for index, bucket in enumerate(_session_buckets())}


def _history_path(data_dir: Path | None, kind: str, date: str) -> Path | None:
    if data_dir is None:
        return None
    # 使用独立目录，避免把其他来源的历史快照混入当前序列。
    return data_dir / "sector_flow_stocksdk" / f"date={date}" / f"{kind}.json"


def _load_history(data_dir: Path | None, kind: str, date: str) -> list[dict[str, Any]]:
    path = _history_path(data_dir, kind, date)
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("source") != _SOURCE:
            return []
        history = payload.get("history")
        if not isinstance(history, list):
            return []
        return [
            item
            for item in history
            if isinstance(item, dict) and item.get("date") == date and item.get("rows")
        ]
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("stock-sdk %s board flow history cannot be read: %s", kind, path)
        return []


def _save_history(data_dir: Path | None, kind: str, date: str, history: list[dict[str, Any]]) -> None:
    path = _history_path(data_dir, kind, date)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"source": _SOURCE, "kind": kind, "date": date, "history": history}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError:
        logger.warning("stock-sdk %s board flow history cannot be saved: %s", kind, path)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _parse_rows(raw_rows: Any, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        net = _number(raw.get("mainNetInflow"))
        if not name or net is None:
            continue
        change = _number(raw.get("changePercent"))
        rows.append({
            "code": str(raw.get("code") or ""),
            "name": name,
            "pct": change / 100.0 if change is not None else None,
            "inflow": None,
            "outflow": None,
            "net": net,
            "member_count": 0,
            "kind": kind,
        })
    return rows


def _fetch(kind: str) -> tuple[list[dict[str, Any]], datetime]:
    sector_type = {"industry": "industry", "concept": "concept"}.get(kind)
    if sector_type is None:
        raise ValueError(f"不支持的 stock-sdk 板块维度: {kind}")
    result = bridge.run_job({"op": "sector_flow", "sectorType": sector_type, "indicator": "today"}, timeout=60)
    return _parse_rows(result.get("rows"), kind), _now()


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return current - previous


def _series(history: list[dict[str, Any]]) -> dict[str, Any]:
    names = sorted({name for item in history for name in item.get("rows", {})})
    buckets = _session_buckets()
    observed = {item["time"]: item["rows"] for item in history}
    fields = {field: [] for field in ("inflow", "outflow", "net", "delta_inflow", "delta_outflow", "delta_net")}
    previous: dict[str, dict[str, float | None]] = {}
    observed_buckets: list[str] = []
    for bucket in buckets:
        current_rows = observed.get(bucket)
        for field in fields:
            fields[field].append([])
        if current_rows is None:
            for _ in names:
                for field in fields:
                    fields[field][-1].append(None)
            continue
        observed_buckets.append(bucket)
        for name in names:
            current = current_rows.get(name, {})
            before = previous.get(name, {})
            fields["inflow"][-1].append(None)
            fields["outflow"][-1].append(None)
            fields["net"][-1].append(current.get("net"))
            fields["delta_inflow"][-1].append(None)
            fields["delta_outflow"][-1].append(None)
            fields["delta_net"][-1].append(_delta(current.get("net"), before.get("net")))
        previous = {name: current_rows.get(name, {}) for name in names}
    return {
        "buckets": buckets,
        "sectors": names,
        "observed_buckets": observed_buckets,
        "observed_count": len(observed_buckets),
        "history_start": observed_buckets[0] if observed_buckets else None,
        "history_end": observed_buckets[-1] if observed_buckets else None,
        "history_contiguous": bool(observed_buckets)
        and observed_buckets == buckets[: _SESSION_ORDER[observed_buckets[-1]] + 1],
        **fields,
    }


def _stale_result(kind: str, data_dir: Path | None, error: Exception) -> dict[str, Any] | None:
    date = _now().strftime("%Y-%m-%d")
    with _LOCK:
        cached = _CACHE.get(kind)
        if cached and cached.get("history"):
            return {
                **cached["result"],
                "status": "stale",
                "reason": "source_stale",
                "error": str(error),
                "stale_seconds": round(max(0.0, time.time() - cached["last_success_epoch"]), 1),
            }
    history = _load_history(data_dir, kind, date)
    if not history:
        return None
    latest = max(history, key=lambda item: _SESSION_ORDER.get(item.get("time", ""), -1))
    series = _series(history)
    return {
        "status": "stale",
        "reason": "source_stale",
        "error": str(error),
        "kind": kind,
        "date": date,
        "as_of": f"{date}T{latest.get('time', '09:30')}:00+08:00",
        "source": _SOURCE,
        "rows": list((latest.get("rows") or {}).values()),
        "series": series,
        "session_open": "09:30",
        "session_close": "15:00",
        "history_persisted": True,
        "history_complete": bool(series.get("history_contiguous")),
    }


def snapshot(kind: str, data_dir: Path | None = None) -> dict[str, Any]:
    """获取 stock-sdk 板块累计净流入并保存当天的连续竞价快照。"""
    now_mono = time.monotonic()
    with _LOCK:
        cached = _CACHE.get(kind)
        if cached and now_mono - cached["fetched_mono"] < _POLL_TTL:
            return cached["result"]
    try:
        rows, fetched_at = _fetch(kind)
        if not rows:
            raise RuntimeError("stock-sdk 板块资金流返回空表")
    except Exception as exc:
        logger.warning("stock-sdk %s board flow unavailable: %s", kind, exc)
        stale = _stale_result(kind, data_dir, exc)
        if stale is not None:
            return stale
        return {"status": "unavailable", "reason": "source_unavailable", "error": str(exc), "kind": kind}

    date = fetched_at.strftime("%Y-%m-%d")
    minute = _session_bucket(fetched_at)
    row_map = {row["name"]: row for row in rows}
    with _LOCK:
        cached = _CACHE.get(kind)
        history = list(cached.get("history", [])) if cached else _load_history(data_dir, kind, date)
        if minute is not None:
            point = {"date": date, "time": minute, "rows": row_map}
            existing = next((index for index, item in enumerate(history) if item.get("time") == minute), None)
            if existing is None:
                history.append(point)
            else:
                history[existing] = point
        history.sort(key=lambda item: _SESSION_ORDER.get(item.get("time", ""), -1))
        history = history[-_MAX_HISTORY:]
        if minute is not None:
            _save_history(data_dir, kind, date, history)
        series = _series(history)
        result = {
            "status": "ok",
            "kind": kind,
            "date": date,
            "as_of": fetched_at.isoformat(timespec="seconds"),
            "source": _SOURCE,
            "partial": False,
            "skipped_pages": [],
            "rows": rows,
            "series": series,
            "session_open": "09:30",
            "session_close": "15:00",
            "history_persisted": data_dir is not None,
            "history_complete": bool(series.get("history_contiguous")),
        }
        _CACHE[kind] = {
            "fetched_mono": time.monotonic(),
            "last_success_epoch": fetched_at.timestamp(),
            "history": history,
            "result": result,
        }
        return result


def invalidate_cache() -> None:
    with _LOCK:
        _CACHE.clear()
