"""统一的时区工具。

A股交易时段判断、行情缓存键等均以上海时间为准。
本模块集中提供一个获取"当前上海时间"的入口，避免各处裸用
``datetime.now()`` / ``date.today()`` 导致海外服务器（容器默认 UTC）
把交易时段误判为休市。

优先使用标准库 ``zoneinfo`` 的 ``Asia/Shanghai``；当系统缺失 tzdata
（少数精简镜像）时退化为固定 UTC+8 偏移，保证功能可用。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

# 上海时间固定偏移 UTC+8，作为 zoneinfo 不可用时的兜底。
_SHANGHAI_OFFSET = timezone(timedelta(hours=8))

try:  # pragma: no cover - 取决于运行环境
    from zoneinfo import ZoneInfo

    _SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:  # 缺 tzdata 时退化为固定偏移
    _SHANGHAI = _SHANGHAI_OFFSET


def now_shanghai() -> datetime:
    """返回带上海时区信息的当前时间。"""
    return datetime.now(_SHANGHAI)
