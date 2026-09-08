from __future__ import annotations

import sys
from collections import Counter

from app.plugins.mootdx import batch_scheduler
from app.plugins.mootdx.batch_scheduler import (
    ResourceProfile,
    choose_worker_count,
    detect_resources,
    run_heap_batch,
)


def _resources(*, cpus: int, memory_workers: int, gpus: int = 0) -> ResourceProfile:
    return ResourceProfile(
        logical_cpus=cpus,
        available_memory_bytes=memory_workers * 256 * 1024 * 1024,
        gpu_count=gpus,
    )


def test_worker_count_obeys_cpu_memory_global_and_symbol_limits():
    assert choose_worker_count(100, _resources(cpus=32, memory_workers=64)) == 16
    assert choose_worker_count(100, _resources(cpus=2, memory_workers=64)) == 4
    assert choose_worker_count(100, _resources(cpus=32, memory_workers=3)) == 3
    assert choose_worker_count(2, _resources(cpus=32, memory_workers=64)) == 2
    assert choose_worker_count(0, _resources(cpus=32, memory_workers=64)) == 0


def test_gpu_inventory_does_not_change_network_worker_count():
    without_gpu = choose_worker_count(100, _resources(cpus=4, memory_workers=32, gpus=0))
    with_gpu = choose_worker_count(100, _resources(cpus=4, memory_workers=32, gpus=8))
    assert without_gpu == with_gpu == 8


def test_resource_probe_falls_back_when_psutil_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(batch_scheduler.os, "cpu_count", lambda: 3)
    monkeypatch.setattr(batch_scheduler, "detect_gpu_count", lambda: 0)

    profile = detect_resources()

    assert profile == ResourceProfile(
        logical_cpus=3,
        available_memory_bytes=256 * 1024 * 1024,
        gpu_count=0,
    )


def test_gpu_probe_failure_returns_zero(monkeypatch):
    batch_scheduler.detect_gpu_count.cache_clear()
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("NVIDIA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(batch_scheduler.shutil, "which", lambda _name: "nvidia-smi")
    monkeypatch.setattr(
        batch_scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unavailable")),
    )

    assert batch_scheduler.detect_gpu_count() == 0
    batch_scheduler.detect_gpu_count.cache_clear()


def test_heap_batch_isolates_failures_and_retries_each_failed_symbol_once():
    calls: Counter[str] = Counter()
    clients = []

    class Client:
        def __init__(self):
            self.closed = False
            clients.append(self)

        def close(self):
            self.closed = True

    def fetch(_client, symbol: str) -> str:
        calls[symbol] += 1
        if symbol == "retry" and calls[symbol] == 1:
            raise TimeoutError("transient")
        if symbol == "bad":
            raise RuntimeError("permanent")
        return symbol

    result = run_heap_batch(
        ["ok", "retry", "bad", "ok"],
        client_factory=Client,
        fetch=fetch,
        resources=_resources(cpus=1, memory_workers=2),
    )

    assert sorted(result.values) == ["ok", "retry"]
    assert result.failed == ["bad"]
    assert calls == Counter({"retry": 2, "bad": 2, "ok": 1})
    assert result.workers == 2
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_client_startup_failure_isolated_without_deadlock():
    result = run_heap_batch(
        ["600000.SH", "000001.SZ"],
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("offline")),
        fetch=lambda _client, symbol: symbol,
        resources=_resources(cpus=1, memory_workers=1),
    )

    assert result.values == []
    assert sorted(result.failed) == ["000001.SZ", "600000.SH"]
    assert result.workers == 1
