"""Lazy MooTDX import and resilient TDX server selection."""
from __future__ import annotations

import importlib.util
import socket
from contextlib import suppress
from datetime import date, timedelta
from typing import Any, Literal

from app.market_time import cn_today

_TDX_SERVERS = [
    # Quote-capable nodes verified against this provider first. The remaining
    # nodes stay available to historical-data validation as fallbacks.
    ("180.153.18.170", 7709),
    ("180.153.18.172", 80),
    ("115.238.90.165", 7709),
    ("115.238.56.198", 7709),
    ("60.191.117.167", 7709),
    ("218.75.126.9", 7709),
    ("60.12.136.250", 7709),
    ("202.108.253.139", 80),
    ("59.36.5.11", 7709),
    ("117.34.114.13", 7709),
    ("119.97.185.59", 7709),
    ("124.70.133.119", 7709),
    ("116.205.183.150", 7709),
    ("123.60.73.44", 7709),
    ("116.205.163.254", 7709),
    ("121.36.225.169", 7709),
    ("123.60.70.228", 7709),
    ("124.71.9.153", 7709),
    ("110.41.147.114", 7709),
    ("124.71.187.122", 7709),
]

# Quote/depth startup should fail closed quickly instead of walking the entire
# fallback list. Polling can retry later and historical paths keep all nodes.
_QUOTE_SERVER_ATTEMPTS = 6


def availability() -> tuple[bool, str]:
    if importlib.util.find_spec("mootdx") is None:
        return False, "未安装 mootdx, 请安装插件 requirements.txt"
    try:
        from mootdx.quotes import Quotes  # noqa: F401
    except Exception as exc:
        return False, f"mootdx 导入失败: {exc}"
    return True, "ok (mootdx)"


def _probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _has_rows(frame: Any) -> bool:
    if frame is None:
        return False
    if hasattr(frame, "empty"):
        return not bool(frame.empty)
    try:
        return len(frame) > 0
    except TypeError:
        return False


def _has_recent_minute_rows(client: Any, today: date | None = None) -> bool:
    """Validate the minute endpoint without requiring data on a closed market day."""
    today = today or cn_today()

    # ``StdQuotes.minute`` is shorthand for ``minutes(date=today)``.  Calling
    # it on weekends makes a healthy server look unavailable, so only use the
    # same-day probe on weekdays and then check recent weekday history.
    if today.weekday() < 5:
        try:
            if _has_rows(client.minute(symbol="600519")):
                return True
        except Exception:
            pass

    for offset in range(1, 15):
        candidate = today - timedelta(days=offset)
        if candidate.weekday() >= 5:
            continue
        try:
            frame = client.minutes(symbol="600519", date=candidate.strftime("%Y%m%d"))
        except Exception:
            continue
        if _has_rows(frame):
            return True
    return False


def _has_daily_rows(client: Any) -> bool:
    """Reject quote nodes that serve minutes but have an empty daily endpoint."""
    try:
        frame = client.bars(
            symbol="600519",
            frequency=9,
            start=0,
            offset=2,
            adjust="none",
        )
    except Exception:
        return False
    return _has_rows(frame)


def _has_quote_rows(client: Any) -> bool:
    """Validate the endpoint used by realtime quotes and five-level depth."""
    try:
        frame = client.quotes(symbol=["600519"])
    except Exception:
        return False
    return _has_rows(frame)


def tdx_client(
    timeout: int = 15,
    validation: Literal["full", "quote"] = "full",
):
    """Create a MooTDX client validated for the requested endpoint family."""
    if validation not in {"full", "quote"}:
        raise ValueError(f"unsupported MooTDX validation mode: {validation}")
    try:
        from mootdx.quotes import Quotes
    except Exception as exc:
        raise RuntimeError("mootdx 不可用, 请安装插件依赖") from exc

    servers = (
        _TDX_SERVERS[:_QUOTE_SERVER_ATTEMPTS]
        if validation == "quote"
        else _TDX_SERVERS
    )
    for ip, port in servers:
        if not _probe(ip, port, timeout=min(2.0, float(timeout))):
            continue
        client = None
        try:
            client = Quotes.factory(market="std", server=(ip, port), timeout=timeout)
            if validation == "quote":
                valid = _has_quote_rows(client)
            else:
                # Historical data uses both daily data (also needed to convert
                # xdxr events) and historical minutes. Some TDX nodes serve
                # only the latter, so require both endpoint families.
                valid = _has_daily_rows(client) and _has_recent_minute_rows(client)
            if valid:
                return client
            client.close()
        except Exception:
            with suppress(Exception):
                client.close()
    raise RuntimeError("所有通达信服务器均无法返回有效 MooTDX 数据")
