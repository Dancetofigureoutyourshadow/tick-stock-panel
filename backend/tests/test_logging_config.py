"""Backend file logging regression tests."""
from __future__ import annotations

import logging
import multiprocessing
from pathlib import Path


def _raise_handler_error(_record: logging.LogRecord) -> None:
    raise RuntimeError("file logging failed")


def _write_shared_log(
    log_path: str,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
    worker_id: int,
) -> None:
    from app.logging_config import build_backend_log_handler

    handler = build_backend_log_handler(
        Path(log_path),
        max_bytes=2 * 1024,
        backup_count=3,
    )
    handler.handleError = _raise_handler_error
    logger = logging.Logger(f"shared-log-worker-{worker_id}", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    ready.put(worker_id)
    start.wait(timeout=5)
    try:
        for index in range(300):
            logger.info("worker=%s index=%s %s", worker_id, index, "x" * 80)
    finally:
        handler.close()


def test_backend_log_rotation_is_safe_across_processes(tmp_path: Path) -> None:
    """Reload handover may briefly leave two workers writing the same log."""
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    start = ctx.Event()
    log_path = tmp_path / "backend.log"
    workers = [
        ctx.Process(target=_write_shared_log, args=(str(log_path), ready, start, worker_id))
        for worker_id in range(2)
    ]

    for worker in workers:
        worker.start()
    try:
        assert {ready.get(timeout=10), ready.get(timeout=10)} == {0, 1}
        start.set()
        for worker in workers:
            worker.join(timeout=20)
        assert [worker.exitcode for worker in workers] == [0, 0]
        assert log_path.exists()
        assert list(tmp_path.glob("backend.log.*"))
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
