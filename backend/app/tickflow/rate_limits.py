"""TickFlow capability rate-limit helpers.

This module centralizes the small pieces of batch/rpm resolution used by
TickFlow-backed services. It intentionally does not manage custom data sources.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TypeVar

from app.tickflow.capabilities import Cap, CapabilitySet

T = TypeVar("T")


@dataclass(frozen=True)
class ResolvedLimit:
    batch: int | None
    rpm: int | None


def resolve_limit(
    capset: CapabilitySet,
    cap: Cap,
    *,
    default_batch: int | None = None,
    default_rpm: int | None = None,
    default_rpm_when_unset: bool = True,
) -> ResolvedLimit:
    """Return a capability's batch/rpm with caller-provided fallbacks."""
    lim = capset.limits(cap)
    if lim is None:
        return ResolvedLimit(batch=default_batch, rpm=default_rpm)
    return ResolvedLimit(
        batch=lim.batch if lim.batch else default_batch,
        rpm=lim.rpm if lim.rpm else (default_rpm if default_rpm_when_unset else None),
    )


def batch_interval(rpm: int | None, *, default: float = 0.0) -> float:
    """Return the existing uniform batch interval formula: 60 / rpm."""
    return 60.0 / rpm if rpm and rpm > 0 else default


def chunked(items: list[T], batch_size: int | None) -> list[list[T]]:
    """Split items by batch size, preserving the existing None-as-one-batch behavior."""
    if batch_size is None:
        return [items]
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def sleep_between_batches(index: int, rpm: int | None, *, default_interval: float = 0.0) -> None:
    """Sleep before every batch after the first, using the existing interval formula."""
    if index <= 0:
        return
    interval = batch_interval(rpm, default=default_interval)
    if interval > 0:
        time.sleep(interval)


def min_batch(preferred: int, limit: ResolvedLimit) -> int:
    """Clamp a user-preferred batch size by a resolved capability batch limit."""
    return min(preferred, limit.batch) if limit.batch else preferred
