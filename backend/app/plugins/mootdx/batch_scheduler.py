"""Resource-aware, heap-backed scheduler for MooTDX minute batches."""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import count
from typing import Any, TypeVar

_MAX_WORKERS = 16
_MEMORY_PER_WORKER = 256 * 1024 * 1024
_QUEUE_DEPTH_PER_WORKER = 4

T = TypeVar("T")


@dataclass(frozen=True)
class ResourceProfile:
    logical_cpus: int
    available_memory_bytes: int
    gpu_count: int


@dataclass(frozen=True)
class BatchResult:
    values: list[Any]
    failed: list[str]
    workers: int
    resources: ResourceProfile


@dataclass(order=True)
class _Task:
    priority: int
    sequence: int
    symbol: str | None = field(compare=False)
    attempt: int = field(compare=False, default=0)


@lru_cache(maxsize=1)
def detect_gpu_count() -> int:
    """Best-effort GPU inventory used for diagnostics, never worker sizing."""
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is None:
        visible = os.environ.get("NVIDIA_VISIBLE_DEVICES")
    if visible is not None:
        devices = [item.strip() for item in visible.split(",") if item.strip()]
        if not devices or any(item.lower() in {"-1", "none", "void"} for item in devices):
            return 0
        return len(devices)

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return 0
    try:
        probe = subprocess.run(
            [executable, "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if probe.returncode != 0:
        return 0
    return sum(1 for line in probe.stdout.splitlines() if line.strip())


def detect_resources() -> ResourceProfile:
    """Probe logical CPUs and available memory with conservative fallbacks."""
    logical_cpus = max(1, int(os.cpu_count() or 1))
    available_memory = _MEMORY_PER_WORKER
    try:
        import psutil

        logical_cpus = max(1, int(psutil.cpu_count(logical=True) or logical_cpus))
        available_memory = max(1, int(psutil.virtual_memory().available))
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        pass
    return ResourceProfile(
        logical_cpus=logical_cpus,
        available_memory_bytes=available_memory,
        gpu_count=detect_gpu_count(),
    )


def choose_worker_count(symbol_count: int, resources: ResourceProfile | None = None) -> int:
    """Apply the fixed CPU/memory formula; GPUs intentionally do not affect it."""
    if symbol_count <= 0:
        return 0
    profile = resources or detect_resources()
    memory_workers = max(1, profile.available_memory_bytes // _MEMORY_PER_WORKER)
    return max(
        1,
        min(
            _MAX_WORKERS,
            2 * max(1, profile.logical_cpus),
            memory_workers,
            symbol_count,
        ),
    )


def run_heap_batch(
    symbols: Iterable[str],
    *,
    client_factory: Callable[[], Any],
    fetch: Callable[[Any, str], T],
    resources: ResourceProfile | None = None,
) -> BatchResult:
    """Fetch symbols with a bounded priority heap and one retry per failure.

    Every worker owns one client for its entire lifetime. Initial work is fully
    drained before retry work is enqueued, so retries cannot starve untouched
    symbols. The PriorityQueue supplies the bounded heap required by the batch
    scheduler while keeping queued work proportional to the worker count.
    """
    requested = list(dict.fromkeys(symbols))
    profile = resources or detect_resources()
    workers = choose_worker_count(len(requested), profile)
    if workers == 0:
        return BatchResult([], [], 0, profile)

    tasks: queue.PriorityQueue[_Task] = queue.PriorityQueue(
        maxsize=max(workers, workers * _QUEUE_DEPTH_PER_WORKER),
    )
    sequence = count()
    values: list[T] = []
    first_failures: list[str] = []
    final_failures: list[str] = []
    result_lock = threading.Lock()

    def worker() -> None:
        client = None
        client_error: Exception | None = None
        try:
            try:
                client = client_factory()
            except Exception as exc:
                client_error = exc
            while True:
                task = tasks.get()
                try:
                    if task.symbol is None:
                        return
                    if client_error is not None:
                        raise client_error
                    value = fetch(client, task.symbol)
                    with result_lock:
                        values.append(value)
                except Exception:
                    with result_lock:
                        target = first_failures if task.attempt == 0 else final_failures
                        target.append(task.symbol)
                finally:
                    tasks.task_done()
        finally:
            if client is not None:
                with suppress(Exception):
                    client.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()

    for symbol in requested:
        tasks.put(_Task(0, next(sequence), symbol, 0))
    tasks.join()

    # Phase two: every failed symbol gets exactly one lower-priority retry.
    for symbol in first_failures:
        tasks.put(_Task(1, next(sequence), symbol, 1))
    tasks.join()

    for _ in threads:
        tasks.put(_Task(99, next(sequence), None, 0))
    tasks.join()
    for thread in threads:
        thread.join()

    return BatchResult(list(values), list(final_failures), workers, profile)
