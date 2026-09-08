"""MooTDX provider exposing the package's standard-market capabilities."""
from __future__ import annotations

import logging
import math
import re
import threading
from bisect import bisect_left
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from app.data_providers.base import ProviderCapabilities
from app.data_providers.normalizer import (
    normalize_adj_factors,
    normalize_daily,
    normalize_instruments,
)
from app.market_time import cn_today, in_continuous_session
from app.plugins.mootdx import bridge
from app.plugins.mootdx.batch_scheduler import run_heap_batch

logger = logging.getLogger(__name__)
_CODE_RE = re.compile(r"^(?:(sh|sz))?(\d{6})(?:\.(sh|sz))?$", re.IGNORECASE)
_MINUTE_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]
_DATASETS = (
    "daily", "adj_factor", "minute", "full_minute", "realtime", "depth5",
    "transactions", "financial", "instruments",
)
_QUOTE_BATCH_SIZE = 100
# Keep this in sync with ``tdxpy.helper.get_security_type``.  MooTDX's
# ``stock_all`` also contains special securities (for example 4xxxxx and
# 8xxxxx) that tdxpy cannot quote and logs as NotImplementedError.
_TDX_SUPPORTED_CODE_PREFIXES = {
    0: ("00", "10", "11", "12", "13", "14", "15", "16", "20", "30", "39"),
    1: (
        "00", "01", "10", "11", "12", "13", "14", "20", "50", "51",
        "60", "68", "88", "90", "99",
    ),
}


@dataclass
class _MooTdxConfig:
    name: str = "mootdx"
    display_name: str = "MooTDX (通达信)"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _code(symbol: str) -> str:
    match = _CODE_RE.fullmatch(str(symbol or "").strip())
    if not match:
        raise ValueError(f"无法识别沪深股票代码: {symbol!r}")
    prefix, code, suffix = match.groups()
    if prefix and suffix and prefix.lower() != suffix.lower():
        raise ValueError(f"代码市场前后缀冲突: {symbol!r}")
    natural = "sh" if code.startswith(("5", "6", "7", "9")) else "sz"
    market = (prefix or suffix or natural).lower()
    if market != natural:
        raise ValueError(f"代码与市场标识冲突: {symbol!r}")
    return code


def _market(code: str) -> int:
    return 1 if code.startswith(("5", "6", "7", "9")) else 0


def _wall_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    from app.market_time import CN_TZ
    return value if value.tzinfo is None else value.astimezone(CN_TZ).replace(tzinfo=None)


def _dates(start: datetime | None, end: datetime | None) -> list[date]:
    start, end = _wall_time(start), _wall_time(end)
    if start is None and end is None:
        from app.market_time import CN_TZ
        return [datetime.now(CN_TZ).date()]
    first, last = (start or end).date(), (end or start).date()
    if last < first:
        return []
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _minute_slots(day: date) -> list[datetime]:
    slots = [datetime.combine(day, time(9, 30)) + timedelta(minutes=i) for i in range(120)]
    slots.extend(datetime.combine(day, time(13, 0)) + timedelta(minutes=i) for i in range(120))
    return slots


def _minute_frame(raw, symbol: str, day: date) -> pl.DataFrame:
    if raw is None:
        return pl.DataFrame()
    rows = (
        raw.to_dicts()
        if isinstance(raw, pl.DataFrame)
        else raw.to_dict("records")
        if hasattr(raw, "to_dict")
        else list(raw)
    )
    if not rows:
        return pl.DataFrame()
    slots = _minute_slots(day)
    out = []
    for point, row in zip(slots, rows, strict=False):
        price = row.get("price")
        volume = row.get("vol", row.get("volume"))
        if price is None or volume is None:
            continue
        price, volume = float(price), float(volume)
        out.append({
            "symbol": symbol, "datetime": point, "open": price, "high": price,
            "low": price, "close": price, "volume": volume,
            "amount": price * volume * 100,
        })
    return pl.DataFrame(out).select(_MINUTE_COLUMNS) if out else pl.DataFrame()


def _records(raw) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, pl.DataFrame):
        return raw.to_dicts()
    if hasattr(raw, "to_dict"):
        return raw.to_dict("records")
    return [dict(item) for item in raw] if isinstance(raw, list) else []


def _empty(raw) -> bool:
    if raw is None:
        return True
    if isinstance(raw, pl.DataFrame):
        return raw.is_empty()
    empty = getattr(raw, "empty", None)
    return bool(empty) if isinstance(empty, bool) else False


def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _finite_number(value) -> float:
    parsed = _number(value)
    return parsed if parsed is not None and math.isfinite(parsed) else 0.0


def _row_date(row: dict, *columns: str) -> date | None:
    for column in columns:
        value = row.get(column)
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value is not None:
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                pass
    try:
        return date(int(row["year"]), int(row["month"]), int(row["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def _xdxr_events(raw) -> list[dict]:
    """Aggregate TDX category-1 events into the fields needed by its ex-price formula."""
    grouped: dict[date, dict[str, float | date]] = {}
    for row in _records(raw):
        category = _number(row.get("category"))
        event_date = _row_date(row, "trade_date", "date", "datetime")
        if category != 1.0 or event_date is None:
            continue
        cash = _finite_number(row.get("fenhong"))
        rights = _finite_number(row.get("peigu"))
        rights_price = _finite_number(row.get("peigujia"))
        bonus = _finite_number(row.get("songzhuangu"))
        if not any((cash, rights, bonus)):
            continue
        event = grouped.setdefault(event_date, {
            "trade_date": event_date,
            "cash": 0.0,
            "rights": 0.0,
            "rights_cost": 0.0,
            "bonus": 0.0,
        })
        event["cash"] += cash
        event["rights"] += rights
        event["rights_cost"] += rights * rights_price
        event["bonus"] += bonus
    return [grouped[key] for key in sorted(grouped)]


def _daily_closes(raw) -> tuple[list[date], list[float]]:
    by_date: dict[date, float] = {}
    for row in _records(raw):
        trading_date = _row_date(row, "datetime", "date", "trade_date")
        close = _number(row.get("close"))
        if trading_date is not None and close is not None and close > 0:
            by_date[trading_date] = close
    dates = sorted(by_date)
    return dates, [by_date[current] for current in dates]


def _events_to_factors(symbol: str, events: list[dict], raw_daily) -> list[dict]:
    dates, closes = _daily_closes(raw_daily)
    factors = []
    for event in events:
        event_date = event["trade_date"]
        previous_index = bisect_left(dates, event_date) - 1
        if previous_index < 0:
            continue
        previous_close = closes[previous_index]
        denominator = 10.0 + event["rights"] + event["bonus"]
        numerator = previous_close * 10.0 - event["cash"] + event["rights_cost"]
        if denominator <= 0 or numerator <= 0:
            continue
        ex_price = numerator / denominator
        ex_factor = previous_close / ex_price
        if math.isfinite(ex_factor) and ex_factor > 0:
            factors.append({
                "symbol": symbol,
                "trade_date": event_date,
                "ex_factor": ex_factor,
            })
    return factors


def _quote_code(row: dict) -> str:
    code = str(row.get("code") or row.get("symbol") or "").strip()
    code = code.replace(".SH", "").replace(".SZ", "")
    return code.zfill(6) if code.isdigit() else ""


def _is_tdx_supported_code(code: str) -> bool:
    """Return whether tdxpy can classify this six-digit quote code."""
    if len(code) != 6 or not code.isdigit():
        return False
    market = _market(code)
    return code[:2] in _TDX_SUPPORTED_CODE_PREFIXES[market]


def _normalize_quotes(rows: list[dict], requested: list[str] | None = None) -> list[dict]:
    requested = requested or []
    requested_by_code = {_code(symbol): symbol for symbol in requested}
    normalized = []
    for index, row in enumerate(rows):
        code = _quote_code(row)
        symbol = requested_by_code.get(code)
        if symbol is None and code:
            exchange = "SH" if _market(code) == 1 else "SZ"
            symbol = f"{code}.{exchange}"
        if symbol is None and index < len(requested):
            symbol = requested[index]
        if symbol is None:
            continue
        last = _number(row.get("last_price", row.get("price")))
        prev = _number(row.get("prev_close", row.get("last_close")))
        if last is None or prev is None:
            continue
        change_amount = _number(row.get("change_amount"))
        if change_amount is None:
            change_amount = last - prev
        change_pct = _number(row.get("change_pct"))
        if change_pct is None:
            change_pct = change_amount / prev if prev else None
        volume = _number(row.get("volume"))
        if volume is None:
            volume = (_number(row.get("vol")) or 0.0) * 100
        normalized.append({
            **row,
            "symbol": symbol,
            "last_price": last,
            "prev_close": prev,
            "open": _number(row.get("open")),
            "high": _number(row.get("high")),
            "low": _number(row.get("low")),
            "volume": volume,
            "amount": _number(row.get("amount")),
            "change_amount": change_amount,
            "change_pct": change_pct,
        })
    return normalized


class MooTdxProvider:
    name = "mootdx"
    builtin = True
    capabilities = ProviderCapabilities(
        instruments=True, daily=True, adj_factor=True, minute=True, realtime=True,
        depth5=True, transactions=True, financial=True,
    )

    def __init__(self) -> None:
        self.config = _MooTdxConfig()
        self._client = None
        self._quote_client = None
        self._client_lock = threading.Lock()
        self._quote_client_lock = threading.Lock()
        # tdxpy installs its own StreamHandler and leaves propagation enabled,
        # so every package log is otherwise emitted by both handlers.
        logging.getLogger("tdxpy").propagate = False

    def _get_client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = bridge.tdx_client()
        return self._client

    def _get_quote_client(self):
        if self._quote_client is None:
            with self._quote_client_lock:
                if self._quote_client is None:
                    # Five-level depth is part of the quote response. It must
                    # not wait for daily and historical-minute server probes.
                    self._quote_client = bridge.tdx_client(timeout=3, validation="quote")
        return self._quote_client

    def close(self) -> None:
        closed: list[object] = []
        for client in (self._client, self._quote_client):
            if client is None or any(client is item for item in closed):
                continue
            with suppress(Exception):
                client.close()
            closed.append(client)
        self._client = None
        self._quote_client = None

    # Native MooTDX surface -------------------------------------------------
    def quotes(self, symbol=None, **kwargs):
        return self._get_quote_client().quotes(symbol=symbol, **kwargs)

    def bars(self, symbol="000001", frequency=9, start=0, offset=800, **kwargs):
        return self._get_client().bars(
            symbol=_code(symbol), frequency=frequency, start=start, offset=offset, **kwargs
        )

    def stock_count(self, market=1):
        return self._get_client().stock_count(market=market)

    def stocks(self, market=1):
        return self._get_client().stocks(market=market)

    def stock_all(self):
        return self._get_client().stock_all()

    def pool(self):
        return self._get_client().pool()

    def traffic(self):
        return self._get_client().traffic()

    def reconnect(self):
        return self._get_client().reconnect()

    def index_bars(self, symbol="000001", frequency=9, start=0, offset=800, **kwargs):
        return self._get_client().index_bars(
            symbol=str(symbol), frequency=frequency, start=start, offset=offset, **kwargs
        )

    def index(self, symbol="000001", frequency=9, start=0, offset=800, **kwargs):
        return self._get_client().index(
            symbol=str(symbol), frequency=frequency, start=start, offset=offset, **kwargs
        )

    def minute(self, symbol=None, **kwargs):
        return self._get_client().minute(symbol=_code(symbol), **kwargs)

    def minutes(self, symbol="000001", date="20191023", **kwargs):
        return self._get_client().minutes(symbol=_code(symbol), date=str(date), **kwargs)

    def transaction(self, symbol="", start=0, offset=800, **kwargs):
        return self._get_client().transaction(
            symbol=_code(symbol), start=start, offset=offset, **kwargs
        )

    def transactions(self, symbol="", start=0, offset=800, date="20170209", **kwargs):
        return self._get_client().transactions(
            symbol=_code(symbol), start=start, offset=offset, date=str(date), **kwargs
        )

    def F10C(self, symbol=""):  # noqa: N802
        return self._get_client().F10C(symbol=_code(symbol))

    def F10(self, symbol="", name=""):  # noqa: N802
        return self._get_client().F10(symbol=_code(symbol), name=name)

    def xdxr(self, symbol=""):
        return self._get_client().xdxr(symbol=_code(symbol))

    def finance(self, symbol="000001", **kwargs):
        return self._get_client().finance(symbol=_code(symbol), **kwargs)

    def k(self, symbol="", begin=None, end=None, **kwargs):
        return self._get_client().k(symbol=_code(symbol), begin=begin, end=end, **kwargs)

    def get_k_data(self, code, start_date, end_date):
        return self._get_client().get_k_data(
            code=_code(code), start_date=start_date, end_date=end_date
        )

    def ohlc(self, **kwargs):
        return self._get_client().ohlc(**kwargs)

    def block(self, tofile="block.dat", **kwargs):
        return self._get_client().block(tofile=tofile, **kwargs)

    # Project Provider contract --------------------------------------------
    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None):
        if asset_type != "stock":
            raise ValueError("MooTDX daily provider only supports stock")
        frames = []
        for current, symbol in enumerate(symbols, 1):
            try:
                if start_time or end_time:
                    try:
                        raw = self.k(
                            symbol, begin=start_time.strftime("%Y-%m-%d") if start_time else None,
                            end=end_time.strftime("%Y-%m-%d") if end_time else None, adjust="none",
                        )
                    except KeyError as exc:
                        # MooTDX 0.11.x raises here when the server sends an
                        # empty response without a datetime column.
                        if exc.args != ("datetime",):
                            raise
                        raw = None
                else:
                    raw = self.bars(symbol, frequency=9, offset=800, adjust="none")
                frame = (pl.DataFrame() if _empty(raw) else
                         normalize_daily(raw, default_symbol=symbol, source=self.name))
                if not frame.is_empty():
                    frames.append(frame)
            finally:
                if on_chunk_done:
                    on_chunk_done(current, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_minute(
        self,
        symbols,
        start_time,
        end_time,
        asset_type="stock",
        freq="1m",
        on_chunk_done=None,
    ):
        if asset_type != "stock" or str(freq).lower() not in {"1m", "1min", "1minute"}:
            raise ValueError("MooTDX minute provider supports stock 1m only")
        start, end = _wall_time(start_time), _wall_time(end_time)
        frames = []
        for current, symbol in enumerate(symbols, 1):
            code = _code(symbol)
            for day in _dates(start_time, end_time):
                frame = _minute_frame(self.minutes(code, day.strftime("%Y%m%d")), symbol, day)
                if not frame.is_empty():
                    frames.append(frame)
            if on_chunk_done:
                on_chunk_done(current, len(symbols))
        frame = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        if not frame.is_empty() and start is not None:
            frame = frame.filter(pl.col("datetime") >= pl.lit(start))
        if not frame.is_empty() and end is not None:
            frame = frame.filter(pl.col("datetime") <= pl.lit(end))
        return frame

    def get_intraday_batch(self, symbols, count=300, asset_type="stock"):
        """Return current-day 1-minute rows for the full-minute repair round.

        MooTDX exposes one symbol per ``minutes`` request, so the plugin performs
        the fan-out locally with resource-aware worker isolation. Each worker
        owns its own TCP client; one bad symbol/server does not discard rows
        already fetched by the other workers.
        """
        if asset_type != "stock":
            raise ValueError("MooTDX full-minute provider only supports stock")
        requested_count = int(count)
        if requested_count <= 0:
            raise ValueError("MooTDX full-minute count must be positive")
        requested = list(dict.fromkeys(symbols or []))
        if not requested:
            return pl.DataFrame()

        day = cn_today()
        day_text = day.strftime("%Y%m%d")

        def _fetch(client, symbol: str) -> pl.DataFrame:
            raw = client.minutes(symbol=_code(symbol), date=day_text)
            frame = _minute_frame(raw, symbol, day)
            return frame.tail(requested_count) if frame.height > requested_count else frame

        result = run_heap_batch(
            requested,
            client_factory=bridge.tdx_client,
            fetch=_fetch,
        )
        frames = [frame for frame in result.values if not frame.is_empty()]
        logger.info(
            "MooTDX full-minute batch: %d/%d symbols, workers=%d, cpu=%d, "
            "available_memory=%d, gpu=%d, failed=%d",
            len(frames), len(requested), result.workers,
            result.resources.logical_cpus, result.resources.available_memory_bytes,
            result.resources.gpu_count, len(result.failed),
        )
        if result.failed:
            logger.warning(
                "MooTDX full-minute skipped %d symbols after one retry: %s",
                len(result.failed), ", ".join(result.failed[:20]),
            )
        return (
            pl.concat(frames, how="diagonal_relaxed").sort("symbol", "datetime")
            if frames else pl.DataFrame()
        )

    def get_realtime(self, universes=None, symbols=None):
        if universes and symbols:
            raise ValueError("MooTDX accepts either universes or symbols")
        requested = list(symbols or [])
        if not requested:
            stock_rows = _records(self.stock_all())
            requested = [
                f"{code}.{'SH' if _market(code) == 1 else 'SZ'}"
                for row in stock_rows
                if (code := _quote_code(row)) and _is_tdx_supported_code(code)
            ]
            requested = list(dict.fromkeys(requested))
        else:
            # Explicit requests must follow the same provider capability
            # boundary as the full-market path.
            requested = [
                symbol for symbol in requested if _is_tdx_supported_code(_code(symbol))
            ]
        if not requested:
            return []
        rows: list[dict] = []
        for start in range(0, len(requested), _QUOTE_BATCH_SIZE):
            batch = requested[start:start + _QUOTE_BATCH_SIZE]
            rows.extend(_normalize_quotes(
                _records(self.quotes([_code(symbol) for symbol in batch])),
                batch,
            ))
        return rows

    def get_depth5(self, symbols: list[str]) -> dict[str, dict]:
        """Adapt MooTDX quote rows to the project's five-level contract."""
        requested = list(dict.fromkeys(symbols or []))
        if not requested:
            return {}

        snapshots: dict[str, dict] = {}
        for row in self.get_realtime(symbols=requested):
            symbol = row.get("symbol")
            if not symbol:
                continue
            snapshots[symbol] = {
                "bid_prices": [_number(row.get(f"bid{i}")) for i in range(1, 6)],
                "ask_prices": [_number(row.get(f"ask{i}")) for i in range(1, 6)],
                "bid_volumes": [_number(row.get(f"bid_vol{i}")) for i in range(1, 6)],
                "ask_volumes": [_number(row.get(f"ask_vol{i}")) for i in range(1, 6)],
                # TDX's servertime is a wall-clock string, so DepthService
                # records its own fetch time when no epoch timestamp exists.
                "timestamp": row.get("timestamp")
                if isinstance(row.get("timestamp"), (int, float)) else None,
            }
        return snapshots

    def get_transactions(self, symbol: str, limit: int = 800) -> list[dict]:
        """Normalize current-day MooTDX time-and-sales rows.

        MooTDX's live ``transaction`` endpoint is only available during a
        continuous trading session.  During lunch and after the close, use
        the historical endpoint for today's date so a first load can still
        retrieve the morning's transactions.
        """
        if not symbol:
            return []
        count = max(1, min(int(limit), 2000))
        code = _code(symbol)
        if in_continuous_session():
            raw = self.transaction(code, start=0, offset=count)
        else:
            raw = self.transactions(
                code,
                start=0,
                offset=count,
                date=cn_today().strftime("%Y%m%d"),
            )
        rows = _records(raw)
        directions = {0: "buy", 1: "sell", 2: "neutral"}
        normalized = []
        for row in rows:
            price = _number(row.get("price"))
            volume = _number(row.get("vol", row.get("volume")))
            if price is None or volume is None:
                continue
            raw_direction = _number(row.get("buyorsell"))
            direction_code = int(raw_direction) if raw_direction is not None and raw_direction.is_integer() else raw_direction
            normalized.append({
                "time": str(row.get("time") or ""),
                "price": price,
                "volume": volume,
                "trade_count": _number(row.get("num")),
                "direction": directions.get(direction_code, "unknown"),
                "direction_code": direction_code,
            })
        return normalized

    def get_instruments(self, asset_type="stock"):
        if asset_type != "stock":
            return pl.DataFrame()
        raw = self.stock_all()
        rows = _records(raw)
        normalized = []
        for row in rows:
            code = str(row.get("code") or row.get("symbol") or "").zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            exchange = "SH" if _market(code) == 1 else "SZ"
            normalized.append({"symbol": f"{code}.{exchange}", "name": row.get("name"),
                               "code": code, "exchange": exchange})
        return normalize_instruments(normalized, asset_type="stock", source=self.name)

    def get_financials(self, table, symbols, latest_only=True):
        frames = []
        for symbol in symbols:
            raw = self.finance(symbol)
            rows = _records(raw)
            if rows:
                frames.append(pl.DataFrame(rows).with_columns(
                    pl.lit(symbol).alias("symbol"), pl.lit(table).alias("table"),
                ))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_adj_factors(
        self,
        symbols,
        start_time,
        end_time,
        asset_type="stock",
        on_chunk_done=None,
    ):
        """Convert native TDX xdxr events to project event adjustment factors."""
        if asset_type != "stock":
            raise ValueError("MooTDX adjustment-factor provider only supports stock")
        if not symbols:
            return pl.DataFrame()

        start, end = _wall_time(start_time), _wall_time(end_time)
        first = start.date() if start else None
        last = end.date() if end else None
        rows: list[dict] = []
        for current, symbol in enumerate(symbols, 1):
            try:
                events = _xdxr_events(self.xdxr(symbol))
                if first is not None:
                    events = [event for event in events if event["trade_date"] >= first]
                if last is not None:
                    events = [event for event in events if event["trade_date"] <= last]
                if not events:
                    continue

                # The TDX event payload has dividend/rights/bonus terms but no
                # previous close. Fetch raw daily prices from the same MooTDX
                # client and apply TDX's theoretical ex-price formula.
                price_end = max(event["trade_date"] for event in events) + timedelta(days=1)
                price_start = min(event["trade_date"] for event in events) - timedelta(days=370)
                raw_daily = self.k(
                    symbol,
                    begin=price_start.isoformat(),
                    end=price_end.isoformat(),
                    adjust="none",
                )
                converted = _events_to_factors(symbol, events, raw_daily)

                # A very long suspension can leave the bounded window without
                # a pre-event close. Retry with full history, still via MooTDX.
                if len(converted) < len(events) and price_start > date(1990, 1, 1):
                    raw_daily = self.k(
                        symbol,
                        begin="1990-01-01",
                        end=price_end.isoformat(),
                        adjust="none",
                    )
                    converted = _events_to_factors(symbol, events, raw_daily)
                rows.extend(converted)
            except Exception as exc:
                logger.warning("MooTDX adj_factor failed for %s: %s", symbol, exc)
            finally:
                if on_chunk_done:
                    on_chunk_done(current, len(symbols))
        return normalize_adj_factors(rows, source=self.name) if rows else pl.DataFrame()

    def test_dataset(self, dataset, symbols=None):
        symbols = symbols or ["600519.SH"]
        if dataset == "daily":
            frame = self.get_daily(symbols, None, None)
        elif dataset == "minute":
            frame = self.get_minute(symbols, None, None)
        elif dataset == "instruments":
            frame = self.get_instruments("stock")
        elif dataset == "financial":
            frame = self.get_financials("income", symbols)
        elif dataset == "adj_factor":
            frame = self.get_adj_factors(symbols, None, None)
        elif dataset == "realtime":
            rows = self.get_realtime(symbols=symbols)
            return {"provider": self.name, "dataset": dataset, "rows": len(rows),
                    "columns": list(rows[0]) if rows else [], "preview": rows[:5]}
        elif dataset == "depth5":
            snapshots = self.get_depth5(symbols or ["600519.SH"])
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": len(snapshots),
                "columns": [
                    "symbol", "bid_prices", "ask_prices",
                    "bid_volumes", "ask_volumes", "timestamp",
                ],
                "preview": [
                    {"symbol": symbol, **snapshot}
                    for symbol, snapshot in list(snapshots.items())[:5]
                ],
            }
        elif dataset == "transactions":
            rows = self.get_transactions(symbols[0] if symbols else "600519.SH")
            return {
                "provider": self.name,
                "dataset": dataset,
                "rows": len(rows),
                "columns": ["time", "price", "volume", "trade_count", "direction", "direction_code"],
                "preview": rows[:5],
            }
        else:
            raise ValueError(f"MooTDX does not support dataset: {dataset}")
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": frame.height,
            "columns": frame.columns,
            "preview": frame.head(5).to_dicts() if not frame.is_empty() else [],
        }
