from __future__ import annotations

import threading
import time

import pytest

from app.services import heavy_job_limiter as limiter_module
from app.services.heavy_job_limiter import (
    HeavyJobCancelledError,
    HeavyJobLimiter,
    HeavyJobLimitTimeoutError,
    heavy_job_limiter,
    shared_heavy_job_limiter,
)


def test_weighted_capacity_and_timeout() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    assert limiter.acquire("normal", timeout=0)
    assert limiter.acquire("normal", timeout=0)
    assert limiter.available == 0
    assert not limiter.acquire("normal", timeout=0.01)
    limiter.release("normal")
    assert not limiter.acquire("mining", timeout=0)
    limiter.release("normal")
    assert limiter.acquire("mining", timeout=0)
    assert limiter.in_use == 2
    limiter.release("mining")


def test_waiting_acquire_can_be_cancelled() -> None:
    limiter = HeavyJobLimiter(capacity=2, cancel_poll_interval=0.01)
    cancel_event = threading.Event()
    assert limiter.acquire("mining", timeout=0)

    result: list[bool] = []
    waiter = threading.Thread(
        target=lambda: result.append(
            limiter.acquire("normal", timeout=1, cancel_event=cancel_event)
        )
    )
    waiter.start()
    time.sleep(0.03)
    cancel_event.set()
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [False]
    assert limiter.in_use == 2
    limiter.release("mining")


def test_context_manager_releases_after_body_error() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    with pytest.raises(ValueError, match="body failed"), limiter.slot("mining", timeout=0):
        raise ValueError("body failed")

    assert limiter.in_use == 0
    assert limiter.acquire("mining", timeout=0)
    limiter.release("mining")


def test_context_manager_distinguishes_timeout_and_cancellation() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    assert limiter.acquire("mining", timeout=0)

    with pytest.raises(HeavyJobLimitTimeoutError), limiter.slot("normal", timeout=0.01):
        pytest.fail("unreachable")

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(HeavyJobCancelledError), limiter.slot("normal", cancel_event=cancelled):
        pytest.fail("unreachable")

    limiter.release("mining")


def test_invalid_release_does_not_overfill_capacity() -> None:
    limiter = HeavyJobLimiter(capacity=2)

    with pytest.raises(RuntimeError):
        limiter.release("normal")
    assert limiter.available == 2

    assert limiter.acquire("normal", timeout=0)
    with pytest.raises(RuntimeError):
        limiter.release("mining")
    assert limiter.in_use == 1
    limiter.release("normal")


def test_module_aliases_share_the_capacity_two_singleton() -> None:
    assert heavy_job_limiter is shared_heavy_job_limiter
    assert shared_heavy_job_limiter.capacity == 2


def test_exclusive_job_waits_for_all_normal_jobs() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    assert limiter.acquire("normal", timeout=0)
    assert not limiter.acquire("exclusive", timeout=0)
    limiter.release("normal")
    with limiter.slot("exclusive", timeout=0):
        assert limiter.in_use == 2
        assert not limiter.acquire("normal", timeout=0)
    assert limiter.in_use == 0


def test_nested_cache_refresh_reuses_exclusive_reservation() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    with limiter.slot("exclusive", timeout=0):
        with limiter.slot("exclusive", timeout=0), limiter.slot("normal", timeout=0):
            assert limiter.in_use == 2
        assert limiter.in_use == 2
    assert limiter.in_use == 0


def test_nested_upgrade_fails_instead_of_deadlocking() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    with (
        limiter.slot("normal", timeout=0),
        pytest.raises(RuntimeError, match="upgrade"),
        limiter.slot("exclusive", timeout=0),
    ):
        pytest.fail("unreachable")
    assert limiter.in_use == 0


def test_waiting_exclusive_job_cannot_be_overtaken() -> None:
    limiter = HeavyJobLimiter(capacity=2)
    assert limiter.acquire("normal", timeout=0)
    entered = threading.Event()
    finish = threading.Event()

    def exclusive():
        with limiter.slot("exclusive", timeout=2):
            entered.set()
            assert finish.wait(2)

    waiter = threading.Thread(target=exclusive)
    waiter.start()
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with limiter._condition:
                if limiter._waiters:
                    break
            time.sleep(0.005)
        else:
            pytest.fail("exclusive job did not queue")
        # One normal slot is free, but belongs to the exclusive job ahead.
        assert not limiter.acquire("normal", timeout=0)
        limiter.release("normal")
        assert entered.wait(1)
    finally:
        finish.set()
        waiter.join(2)
    assert not waiter.is_alive()
    assert limiter.in_use == 0


def test_wait_reason_names_memory_admission(monkeypatch) -> None:
    """内存准入挡门时提示「内存不足」, 不能一律推给「其他计算任务」。

    线上小内存机器可用内存长期低于门槛, 槽位空着却进不去; 旧文案把排查
    方向带偏到「谁占了槽位」。
    """
    limiter = HeavyJobLimiter(capacity=2)
    monkeypatch.setattr(limiter_module, "_HEAVY_MIN_AVAILABLE_BYTES", 1024 * 2**20)
    monkeypatch.setattr(limiter_module, "_memory_admission", lambda: (False, 454 * 2**20))
    reason = limiter.wait_reason("mining")
    assert "内存不足" in reason and "1024MiB" in reason and "454MiB" in reason

    monkeypatch.setattr(limiter_module, "_memory_admission", lambda: (True, 4 * 2**30))
    assert limiter.wait_reason("mining") == "等待其他计算任务完成…"
