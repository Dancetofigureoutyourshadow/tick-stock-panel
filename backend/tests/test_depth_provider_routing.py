"""DepthService provider routing contract tests."""
from __future__ import annotations

import threading

from app.data_providers import custom as custom_sources
from app.services import preferences
from app.services.depth_service import DepthService


def test_depth_service_routes_to_selected_custom_provider(monkeypatch):
    expected = {
        "600519.SH": {
            "bid_prices": [100.0] * 5,
            "ask_prices": [101.0] * 5,
            "bid_volumes": [1.0] * 5,
            "ask_volumes": [0.0] * 5,
            "timestamp": None,
        },
    }

    class FakeDepthProvider:
        def get_depth5(self, symbols):
            assert symbols == ["600519.SH"]
            return expected

    monkeypatch.setattr(preferences, "get_depth5_data_provider", lambda: "mootdx")
    monkeypatch.setattr(
        custom_sources,
        "provider_has_dataset",
        lambda name, dataset: name == "mootdx" and dataset == "depth5",
    )
    monkeypatch.setattr(custom_sources, "get_provider", lambda _name: FakeDepthProvider())

    service = DepthService()

    assert service._has_capability() is True
    assert service._call_depth_batch(["600519.SH"]) == expected


def test_depth_boot_check_does_not_block_application_startup(monkeypatch):
    service = DepthService()
    finalize_entered = threading.Event()
    release_finalize = threading.Event()
    finalize_calls = 0

    monkeypatch.setattr(service, "_has_capability", lambda: True)
    monkeypatch.setattr(service, "_persisted_for_date", lambda _date: False)

    def slow_finalize():
        nonlocal finalize_calls
        finalize_calls += 1
        finalize_entered.set()
        release_finalize.wait(timeout=2)

    monkeypatch.setattr(service, "finalize", slow_finalize)

    caller = threading.Thread(target=service.boot_check)
    caller.start()
    assert finalize_entered.wait(timeout=0.5)

    # boot_check is called from FastAPI lifespan and therefore must return
    # while the network-backed startup backfill continues in the background.
    caller.join(timeout=0.1)
    try:
        assert caller.is_alive() is False
        service.boot_check()
        assert finalize_calls == 1
    finally:
        release_finalize.set()
        caller.join(timeout=1)
        boot_thread = getattr(service, "_boot_thread", None)
        if boot_thread is not None:
            boot_thread.join(timeout=1)
