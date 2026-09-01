"""Minute-K and five-level stream for the currently focused instrument."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any

from app.market_time import cn_now, cn_today, in_continuous_session

logger = logging.getLogger(__name__)


def _market_phase(now: datetime | None = None) -> str:
    """Return the Beijing market phase used by the focused-detail stream."""
    now = now or cn_now()
    if now.weekday() >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "pre_open"
    if minutes <= 11 * 60 + 30:
        return "morning"
    if minutes < 13 * 60:
        return "lunch"
    if minutes <= 15 * 60:
        return "afternoon"
    return "closed"


def _json_value(value: Any) -> Any:
    """Convert minute/depth values into JSON-serializable primitives."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _levels(value: Any) -> list[Any]:
    """Normalize a five-level array and preserve missing levels as None."""
    values = list(value) if isinstance(value, (list, tuple)) else []
    return [_json_value(values[i]) if i < len(values) else None for i in range(5)]


class FocusMarketStreamService:
    """Share one polling task per symbol and broadcast snapshots to SSE clients."""

    def __init__(self, repo, depth_service) -> None:
        self._repo = repo
        self._depth_service = depth_service
        self._subscribers: dict[str, set[asyncio.Queue[dict]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._last_payload: dict[str, dict] = {}
        self._last_depth: dict[str, dict] = {}
        self._last_transactions: dict[str, list[dict]] = {}
        self._off_session_checked: set[str] = set()
        self._closed = False

    def subscribe(self, symbol: str) -> asyncio.Queue[dict]:
        if self._closed:
            raise RuntimeError("focus market stream is closed")
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=2)
        clients = self._subscribers.setdefault(symbol, set())
        clients.add(queue)
        task = self._tasks.get(symbol)
        if task is None or task.done():
            self._tasks[symbol] = asyncio.create_task(
                self._poll_symbol_loop(symbol),
                name=f"focus-market-{symbol}",
            )
        return queue

    def unsubscribe(self, symbol: str, queue: asyncio.Queue[dict]) -> None:
        clients = self._subscribers.get(symbol)
        if clients is None:
            return
        clients.discard(queue)
        if clients:
            return
        self._subscribers.pop(symbol, None)
        self._last_payload.pop(symbol, None)
        self._off_session_checked.discard(symbol)
        task = self._tasks.pop(symbol, None)
        if task is not None and not task.done():
            task.cancel()

    def initial_status(self, symbol: str) -> dict:
        now = cn_now()
        cached_depth = self._last_depth.get(symbol)
        cached_transactions = self._last_transactions.get(symbol)
        transactions_provider = self._transactions_provider()
        return {
            "symbol": symbol,
            "trade_date": str(now.date()),
            "ts": int(time.time() * 1000),
            "poll_interval_s": self._poll_interval(),
            "market_open": in_continuous_session(now),
            "market_phase": _market_phase(now),
            "minute": {"ok": False, "row": None, "source": None, "error": None},
            "depth": {
                "ok": cached_depth is not None,
                "available": self._depth_service is not None,
                "snapshot": dict(cached_depth) if cached_depth is not None else None,
                "provider": self._depth_provider_name(),
                "error": None,
            },
            "transactions": {
                "ok": cached_transactions is not None,
                "available": transactions_provider is not None,
                "rows": list(cached_transactions) if cached_transactions is not None else [],
                "provider": self._transactions_provider_name(),
                "error": None,
            },
        }

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._subscribers.clear()
        self._last_payload.clear()
        self._last_depth.clear()
        self._last_transactions.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_symbol_loop(self, symbol: str) -> None:
        current = asyncio.current_task()
        try:
            while not self._closed and self._subscribers.get(symbol):
                if in_continuous_session():
                    # 午休/收盘后重新进入盘中时允许下一次非交易时段状态再次发送，
                    # 确保页面从盘中持续打开到收盘时能收到收盘状态。
                    self._off_session_checked.discard(symbol)
                    payload = await asyncio.to_thread(self._poll_symbol, symbol)
                    if payload is not None:
                        self._publish(symbol, payload)
                elif symbol not in self._off_session_checked and (
                    symbol not in self._last_depth or symbol not in self._last_transactions
                ):
                    self._off_session_checked.add(symbol)
                    payload = await asyncio.to_thread(self._poll_depth_only, symbol)
                    if payload is not None:
                        self._publish(symbol, payload)
                await asyncio.sleep(self._poll_interval())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("focus market stream loop failed for %s", symbol)
        finally:
            if self._tasks.get(symbol) is current:
                self._tasks.pop(symbol, None)

    def _poll_symbol(self, symbol: str) -> dict | None:
        now = cn_now()
        if not in_continuous_session(now):
            return None

        minute = self._fetch_minute(symbol)
        depth = self._fetch_depth(symbol)
        transactions = self._fetch_transactions(symbol)
        payload = {
            "symbol": symbol,
            "trade_date": str(now.date()),
            "ts": int(time.time() * 1000),
            "poll_interval_s": self._poll_interval(),
            "market_open": True,
            "market_phase": _market_phase(now),
            "minute": minute,
            "depth": depth,
            "transactions": transactions,
        }

        comparable = {"minute": minute, "depth": depth, "transactions": transactions}
        if comparable == self._last_payload.get(symbol):
            return None
        self._last_payload[symbol] = comparable
        return payload

    def _poll_depth_only(self, symbol: str) -> dict:
        """Prime the last available quote when a stream starts outside market hours."""
        now = cn_now()
        return {
            "symbol": symbol,
            "trade_date": str(now.date()),
            "ts": int(time.time() * 1000),
            "poll_interval_s": self._poll_interval(),
            "market_open": False,
            "market_phase": _market_phase(now),
            "minute": {"ok": False, "row": None, "source": None, "error": None},
            "depth": self._fetch_depth(symbol),
            "transactions": self._fetch_transactions(symbol),
        }

    def _fetch_minute(self, symbol: str) -> dict:
        result = {"ok": False, "row": None, "source": None, "error": None}
        try:
            from app.services import kline_sync

            asset_type = self._repo.resolve_asset_type(symbol)
            frame = kline_sync.fetch_minute_single(
                symbol, cn_today(), asset_type=asset_type,
            )
            if frame.is_empty():
                result["error"] = "分钟行情暂无数据"
                return result
            rows = frame.tail(1).to_dicts()
            if not rows:
                result["error"] = "分钟行情暂无数据"
                return result
            result.update({"ok": True, "row": _json_value(rows[0]), "source": "live"})
        except Exception as exc:
            logger.warning("focus minute fetch failed for %s: %s", symbol, exc)
            result["error"] = "分钟行情获取失败"
        return result

    def _fetch_depth(self, symbol: str) -> dict:
        result = {
            "ok": False,
            "available": self._depth_service is not None,
            "snapshot": None,
            "provider": self._depth_provider_name(),
            "error": None,
        }
        if self._depth_service is None:
            result["available"] = False
            result["error"] = "五档盘口不可用"
            return result
        try:
            snapshot = self._depth_service.get_snapshot(symbol)
            if not snapshot:
                result["available"] = False
                result["error"] = "五档盘口不可用"
                return result
            normalized = dict(snapshot)
            normalized["symbol"] = symbol
            normalized["bid_prices"] = _levels(normalized.get("bid_prices"))
            normalized["ask_prices"] = _levels(normalized.get("ask_prices"))
            normalized["bid_volumes"] = _levels(normalized.get("bid_volumes"))
            normalized["ask_volumes"] = _levels(normalized.get("ask_volumes"))
            self._last_depth[symbol] = dict(_json_value(normalized))
            result.update({"ok": True, "available": True, "snapshot": _json_value(normalized)})
        except Exception as exc:
            logger.warning("focus depth fetch failed for %s: %s", symbol, exc)
            result["error"] = "五档盘口获取失败"
        return result

    def _fetch_transactions(self, symbol: str) -> dict:
        result = {
            "ok": False,
            "available": False,
            "rows": [],
            "provider": self._transactions_provider_name(),
            "error": None,
        }
        provider = self._transactions_provider()
        if provider is None:
            result["error"] = "分时成交不可用"
            return result
        result["available"] = True
        try:
            rows = provider.get_transactions(symbol, limit=800)
            if not isinstance(rows, list) or not rows:
                result["error"] = "暂无分时成交"
                return result
            normalized = [dict(row) for row in rows if isinstance(row, dict)]
            if not normalized:
                result["error"] = "暂无分时成交"
                return result
            self._last_transactions[symbol] = list(normalized)
            result.update({"ok": True, "rows": _json_value(normalized)})
        except Exception as exc:
            logger.warning("focus transactions fetch failed for %s: %s", symbol, exc)
            result["error"] = "分时成交获取失败"
        return result

    def _publish(self, symbol: str, payload: dict) -> None:
        for queue in list(self._subscribers.get(symbol, ())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Keep only the newest snapshot so slow clients do not block polling.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except asyncio.QueueEmpty:
                    pass

    @staticmethod
    def _poll_interval() -> float:
        from app.services import preferences

        try:
            value = float(preferences.get_realtime_quote_interval())
        except (TypeError, ValueError):
            value = 6.0
        return max(1.0, min(60.0, value))

    @staticmethod
    def _depth_provider_name() -> str | None:
        try:
            from app.services import preferences

            return preferences.get_depth5_data_provider()
        except Exception:
            return None

    @staticmethod
    def _transactions_provider_name() -> str | None:
        try:
            from app.services import preferences

            return preferences.get_depth5_data_provider()
        except Exception:
            return None

    @staticmethod
    def _transactions_provider():
        from app.data_providers import custom as custom_sources

        provider_name = FocusMarketStreamService._transactions_provider_name()
        if not provider_name or provider_name == "tickflow":
            return None
        try:
            if not custom_sources.provider_has_dataset(provider_name, "transactions"):
                return None
            provider = custom_sources.get_provider(provider_name)
        except Exception as exc:
            logger.warning("custom transactions provider '%s' unavailable: %s", provider_name, exc)
            return None
        if not callable(getattr(provider, "get_transactions", None)):
            logger.warning("custom transactions provider '%s' has no get_transactions contract", provider_name)
            return None
        return provider
