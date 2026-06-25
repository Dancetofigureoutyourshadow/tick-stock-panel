"""AI 财务分析报告持久化存储。

存储位置: data/user_data/ai_reports.json (数组,按 created_at 降序)
保留最近 MAX_REPORTS 条;超出自动裁剪最旧的。

每条报告结构:
{
  "id": "rpt_xxx",           # 唯一 id
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "focus": "",               # 用户追加的关心点(可为空)
  "content": "# ...markdown", # 报告正文
  "periods": 4,              # 基于几期数据生成
  "summary": "metrics: 1期...",  # 数据摘要
  "created_at": "2026-06-25T10:00:00"
}
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_REPORTS = 20


def _path() -> Path:
    from app.config import settings
    p = settings.data_dir / "user_data" / "ai_reports.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def list_reports() -> list[dict]:
    """返回全部报告(按 created_at 降序)。"""
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return sorted(data, key=lambda r: r.get("created_at", ""), reverse=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("ai_reports.json malformed: %s", e)
    return []


def _save_all(reports: list[dict]) -> None:
    """全量写入(裁剪到 MAX_REPORTS)。"""
    # 保持降序
    reports.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    if len(reports) > MAX_REPORTS:
        reports = reports[:MAX_REPORTS]
    _path().write_text(
        json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def save_report(report: dict) -> dict:
    """新增一条报告并持久化。返回保存后的报告(含 id / created_at)。

    自动补全 id 与 created_at(若缺),并裁剪到上限。
    """
    reports = list_reports()
    if not report.get("id"):
        report["id"] = f"rpt_{int(time.time() * 1000)}_{report.get('symbol', 'x')}"
    if not report.get("created_at"):
        report["created_at"] = _now_iso()
    reports.append(report)
    _save_all(reports)
    logger.info("AI report saved: %s (%s), total %d", report.get("symbol"), report.get("id"), len(reports))
    return report


def delete_report(report_id: str) -> bool:
    """删除指定报告。返回是否删除成功。"""
    reports = list_reports()
    before = len(reports)
    reports = [r for r in reports if r.get("id") != report_id]
    if len(reports) < before:
        _save_all(reports)
        return True
    return False


def clear_reports() -> int:
    """清空全部报告。返回删除数量。"""
    reports = list_reports()
    n = len(reports)
    if n > 0:
        _save_all([])
    return n


def _now_iso() -> str:
    """当前本地时间 ISO 字符串(带秒精度,前端 toLocaleString 友好)。"""
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
