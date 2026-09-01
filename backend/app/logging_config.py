"""Process-safe logging configuration helpers."""
from __future__ import annotations

from pathlib import Path

from concurrent_log_handler import ConcurrentRotatingFileHandler


def build_backend_log_handler(
    log_path: Path,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> ConcurrentRotatingFileHandler:
    """Build a handler that can rotate while reload workers overlap."""
    return ConcurrentRotatingFileHandler(
        filename=str(log_path),
        mode="a",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
