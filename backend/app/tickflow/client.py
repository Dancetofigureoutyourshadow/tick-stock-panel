"""TickFlow SDK 封装(§5)。

进程内单例;Key 来源(优先级):secrets.json > .env。
用户改 Key 后需要 `reset_clients()`,然后 `get_client()` 会拿新的。
"""
from __future__ import annotations

import os

from tickflow import AsyncTickFlow, TickFlow

from app import secrets_store

_sync_client: TickFlow | None = None
_async_client: AsyncTickFlow | None = None


def _base_url() -> str | None:
    """从 secrets.json 读取用户自定义端点,没有则返回 None(用 SDK 默认)。"""
    return secrets_store.load().get("tickflow_base_url") or None


def get_client() -> TickFlow:
    """同步客户端。能力探测、盘后管道用。"""
    global _sync_client
    if _sync_client is None:
        key = secrets_store.get_tickflow_key()
        if not key:
            # Free 模式:付费端点 URL 不可用,忽略 base_url 走 SDK 默认 free-api
            _sync_client = TickFlow.free()
        else:
            _sync_client = TickFlow(api_key=key, base_url=_base_url())
    return _sync_client


def get_async_client() -> AsyncTickFlow:
    """异步客户端。FastAPI 请求路径上用。"""
    global _async_client
    if _async_client is None:
        key = secrets_store.get_tickflow_key()
        if not key:
            # Free 模式:付费端点 URL 不可用,忽略 base_url 走 SDK 默认 free-api
            _async_client = AsyncTickFlow.free()
        else:
            _async_client = AsyncTickFlow(api_key=key, base_url=_base_url())
    return _async_client


def reset_clients() -> None:
    """Key 变化后调用 — 让下一次 get_client() 拿新实例。"""
    global _sync_client, _async_client
    _sync_client = None
    _async_client = None


def current_mode() -> str:
    """供 UI 显示当前模式。"""
    return "api_key" if secrets_store.get_tickflow_key() else "free"


def current_endpoint() -> str:
    """返回当前显示用的端点 URL(对应 endpoints.json 列表项)。

    注:SDK 的 TickFlow.free() 内部实际走 free-api,但 UI 显示统一用默认
    节点(api.tickflow.org),使"当前使用"始终对得上端点列表里的某一项。
    """
    # 自定义端点(付费模式测速切换后):优先返回
    base = _base_url()
    if base:
        return base.rstrip("/")
    # Free 模式或未自定义:统一显示默认节点
    return "https://api.tickflow.org"
