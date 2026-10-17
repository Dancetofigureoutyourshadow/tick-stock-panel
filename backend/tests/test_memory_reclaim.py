from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.backtest import worker
from app.services import memory_reclaim
from app.tickflow.repository import DataStore, KlineRepository


def test_trim_runs_after_collecting_dead_objects_and_keeps_live_cache(monkeypatch):
    import gc
    import weakref

    class Object:
        pass

    dead = Object()
    dead.cycle = dead
    reference = weakref.ref(dead)
    del dead
    live = bytearray(1024)
    live[0] = 42

    def trim(padding):
        assert reference() is None
        assert padding == 0
        assert live[0] == 42
        return 0

    monkeypatch.setattr(memory_reclaim.sys, "platform", "linux")
    monkeypatch.setattr(memory_reclaim.ctypes, "CDLL", lambda _: SimpleNamespace(malloc_trim=trim))
    gc.disable()
    try:
        memory_reclaim.release_unused_memory()
    finally:
        gc.enable()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_unsupported_platform_or_allocator_does_not_break_task(monkeypatch, platform):
    monkeypatch.setattr(memory_reclaim.sys, "platform", platform)
    monkeypatch.setattr(memory_reclaim.ctypes, "CDLL", lambda _: SimpleNamespace())
    monkeypatch.setattr(memory_reclaim.gc, "collect", lambda: pytest.fail("unsupported allocator"))
    memory_reclaim.release_unused_memory()


def test_worker_reclaims_parent_before_spawning(monkeypatch):
    calls = []
    monkeypatch.setattr(memory_reclaim, "release_unused_memory", lambda: calls.append("reclaim"))

    class Process:
        def start(self):
            assert calls == ["reclaim"]
            raise RuntimeError("spawn failure")

    events = SimpleNamespace(close=lambda: None, join_thread=lambda: None)
    monkeypatch.setattr(worker.mp, "get_context", lambda _: SimpleNamespace(
        Queue=lambda: events, Event=threading.Event, Process=lambda **_: Process(),
    ))
    with pytest.raises(RuntimeError, match="spawn failure"):
        worker.run_worker_task({"kind": "backtest"})


@pytest.mark.parametrize("fails", [False, True])
def test_background_warmup_reclaims_before_publishing_ready(monkeypatch, tmp_path, fails):
    repo = KlineRepository(DataStore(tmp_path))
    cache = object()
    repo._enriched_history_cache = cache
    calls = []

    def refresh():
        calls.append("refresh")
        if fails:
            raise RuntimeError("warmup failure")

    def reclaim():
        assert repo._enriched_warming
        assert repo._enriched_history_cache is cache
        calls.append("reclaim")

    monkeypatch.setattr(memory_reclaim, "release_unused_memory", reclaim)
    monkeypatch.setattr(repo, "_refresh_enriched", refresh)
    repo._on_warmup_done = lambda: calls.append("ready")
    repo._start_enriched_warmup()
    repo._warmup_thread.join(timeout=5)
    assert not repo._warmup_thread.is_alive()
    assert calls == ["refresh", "reclaim", "ready"]
    assert not repo._enriched_warming
    repo.db.close()
