"""Lazy MooTDX import and resilient TDX server selection."""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
import time
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Any, Literal

from app.config import settings
from app.market_time import cn_today
from app.services.fs_utils import atomic_write_text

_TDX_SERVERS = [
    # Explicit fallback order. Each node must pass a real quote request; TCP
    # connect alone is not enough because several nodes accept handshakes but
    # return an empty payload. Keep the list broad because availability varies
    # by ISP, region, and time of day.
    ("59.36.5.11", 7709),
    ("180.153.18.170", 7709),
    ("180.153.18.172", 80),
    ("115.238.90.165", 7709),
    ("115.238.56.198", 7709),
    ("60.191.117.167", 7709),
    ("218.75.126.9", 7709),
    ("60.12.136.250", 7709),
    ("202.108.253.139", 80),
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

# tdx-mcp/TDXDataFetcher 的 DEFAULT_SERVERS。单独保留来源，避免以后维护时
# 把该项目的节点误删或与 mootdx 自带列表混为一谈。
_TDX_MCP_SERVERS = [
    ("119.147.212.81", 7709),
    ("180.153.39.51", 7709),
    ("119.147.212.82", 7709),
    ("218.75.126.9", 7709),
    ("115.238.90.165", 7709),
]

# ``None`` means quote/depth validation walks the complete candidate list. The
# previous prefix limit made a healthy server later in the list invisible.
_QUOTE_SERVER_ATTEMPTS: int | None = None
_SERVER_STATE_LOCK = threading.RLock()
_SERVER_STATE_VERSION = 1


def _server_state_path():
    return settings.data_dir / "user_data" / "mootdx_server_order.json"


def _server_key(server: tuple[str, int]) -> str:
    return f"{server[0]}:{server[1]}"


def _read_server_state() -> dict[str, dict]:
    try:
        payload = json.loads(_server_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != _SERVER_STATE_VERSION:
        return {}
    rows = payload.get("servers")
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("key")): row
        for row in rows
        if isinstance(row, dict) and row.get("key")
    }


def _write_server_state(order: list[tuple[str, int]], rows: dict[str, dict]) -> None:
    path = _server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _SERVER_STATE_VERSION,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "servers": [rows[_server_key(server)] for server in order],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _ordered_servers(candidates: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Apply the persisted order, appending newly discovered nodes in source order."""
    with _SERVER_STATE_LOCK:
        rows = _read_server_state()
    by_key = {_server_key(server): server for server in candidates}
    persisted = [
        by_key[key]
        for key in rows
        if key in by_key
    ]
    persisted_keys = {_server_key(server) for server in persisted}
    return persisted + [
        server for server in candidates
        if _server_key(server) not in persisted_keys
    ]


def _record_server_result(
    server: tuple[str, int], *, success: bool, latency_ms: float | None = None,
) -> None:
    """Persist health and move successful nodes to the front, failures to the end."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _SERVER_STATE_LOCK:
        candidates = _server_candidates()
        rows = _read_server_state()
        order = _ordered_servers(candidates)
        key = _server_key(server)
        row = dict(rows.get(key) or {
            "key": key,
            "ip": server[0],
            "port": server[1],
            "successes": 0,
            "failures": 0,
        })
        if success:
            row["successes"] = int(row.get("successes", 0)) + 1
            row["last_success_at"] = now
            if latency_ms is not None:
                row["last_latency_ms"] = round(latency_ms, 1)
            order = [server, *[item for item in order if item != server]]
        else:
            row["failures"] = int(row.get("failures", 0)) + 1
            row["last_failure_at"] = now
            order = [item for item in order if item != server] + [server]
        rows[key] = row
        # 新版本新增的节点也写入状态文件，保证下次顺序合并稳定。
        for item in order:
            item_key = _server_key(item)
            rows.setdefault(item_key, {
                "key": item_key,
                "ip": item[0],
                "port": item[1],
                "successes": 0,
                "failures": 0,
            })
        try:
            _write_server_state(order, rows)
        except OSError:
            # 节点排序是增强能力，不能因为用户数据目录暂时不可写而让
            # 一个已经通过实时校验的 MooTDX 客户端失效。
            pass


def _server_candidates() -> list[tuple[str, int]]:
    """Return the merged TDX HQ list without requiring top-level imports.

    MooTDX ships a maintained cloud list and its tdxpy dependency ships the
    broader legacy/broker list. Merge both at runtime so a package upgrade can
    add nodes without another application release. The local list remains the
    first tier for deterministic fallback order and compatibility with older
    mootdx versions.
    """
    candidates = list(_TDX_SERVERS)
    try:
        from mootdx.consts import HQ_HOSTS as mootdx_hosts
    except Exception:
        mootdx_hosts = []
    try:
        from tdxpy.constants import hq_hosts as tdxpy_hosts
    except Exception:
        tdxpy_hosts = []

    seen = set(candidates)
    for host in [*_TDX_MCP_SERVERS, *mootdx_hosts, *tdxpy_hosts]:
        if isinstance(host, tuple) and len(host) == 2:
            candidate = (str(host[0]), int(host[1]))
            if candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
            continue
        try:
            candidate = (str(host[1]), int(host[2]))
        except (IndexError, TypeError, ValueError):
            continue
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def availability() -> tuple[bool, str]:
    if importlib.util.find_spec("mootdx") is None:
        return False, "未安装 mootdx, 请安装插件 requirements.txt"
    try:
        from mootdx.quotes import Quotes  # noqa: F401
    except Exception as exc:
        return False, f"mootdx 导入失败: {exc}"
    # Import success only proves that the wrapper is installed.  TDX nodes can
    # still accept TCP connections while returning an empty/short response.
    # Reuse the same quote validation as the realtime provider so the plugin
    # status does not advertise a source that cannot actually serve data.
    try:
        client = tdx_client(timeout=3, validation="quote")
    except Exception as exc:
        return False, f"通达信行情不可用: {exc}"
    with suppress(Exception):
        client.close()
    return True, "ok (mootdx)"


def _probe(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _reachable_servers(
    servers: list[tuple[str, int]], timeout: float,
) -> list[tuple[str, int]]:
    """Probe every candidate concurrently while retaining configured order."""
    if not servers:
        return []
    worker_count = min(32, len(servers))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tdx-probe") as pool:
        reachable = pool.map(
            lambda server: _probe(server[0], server[1], timeout=timeout),
            servers,
        )
        return [server for server, ok in zip(servers, reachable) if ok]


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

    all_servers = _ordered_servers(_server_candidates())
    servers = (
        all_servers
        if validation == "full" or _QUOTE_SERVER_ATTEMPTS is None
        else all_servers[:_QUOTE_SERVER_ATTEMPTS]
    )
    attempted = len(servers)
    reachable_servers = _reachable_servers(servers, timeout=min(2.0, float(timeout)))
    for ip, port in reachable_servers:
        started = time.perf_counter()
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
                _record_server_result(
                    (ip, port),
                    success=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                return client
            _record_server_result((ip, port), success=False)
            client.close()
        except Exception:
            _record_server_result((ip, port), success=False)
            with suppress(Exception):
                client.close()

    # Last-resort compatibility paths for users whose mootdx config has a
    # working BESTIP entry. These are deliberately after the explicit list so
    # a stale/empty BESTIP value cannot hide the maintained fallback order.
    for kwargs in ({"bestip": True}, {}):
        client = None
        try:
            client = Quotes.factory(market="std", **kwargs, timeout=timeout)
            if validation == "quote":
                valid = _has_quote_rows(client)
            else:
                valid = _has_daily_rows(client) and _has_recent_minute_rows(client)
            if valid:
                return client
        except Exception:
            pass
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()
    raise RuntimeError(
        f"所有通达信服务器均无法返回有效 MooTDX 数据（已尝试 {attempted} 台显式服务器，"
        "并已回退 BESTIP）"
    )
