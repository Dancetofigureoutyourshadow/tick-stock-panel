from __future__ import annotations

import threading

from app.services.quote_service import QuoteService


def test_pause_waits_for_inflight_fetch_and_blocks_late_fetch() -> None:
    service = QuoteService()
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    fetch_calls = 0

    def fake_fetch(**_kwargs) -> None:
        nonlocal fetch_calls
        fetch_calls += 1
        fetch_started.set()
        assert release_fetch.wait(timeout=2)

    service._fetch_full_market_quotes = fake_fetch  # type: ignore[method-assign]
    worker = threading.Thread(target=service._fetch_quotes)
    worker.start()
    assert fetch_started.wait(timeout=2)

    pause_finished = threading.Event()

    def pause() -> None:
        service.pause()
        pause_finished.set()

    pauser = threading.Thread(target=pause)
    pauser.start()
    try:
        assert not pause_finished.wait(timeout=0.05)
    finally:
        release_fetch.set()
        worker.join(timeout=2)
        pauser.join(timeout=2)
    assert pause_finished.is_set()

    service._fetch_quotes()
    assert fetch_calls == 1
