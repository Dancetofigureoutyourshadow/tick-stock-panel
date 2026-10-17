import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.api import data as data_api


@pytest.fixture(autouse=True)
def isolated_status_snapshot(monkeypatch):
    for name, value in {
        "_status_snapshot": None, "_status_snapshot_dir": None, "_status_snapshot_ts": 0.0,
        "_status_generation": 0, "_status_refreshing": False,
    }.items():
        monkeypatch.setattr(data_api, name, value)
    yield
    _wait_for_refresh()


def _wait_for_refresh():
    deadline = time.monotonic() + 5
    while data_api._status_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not data_api._status_refreshing


def _request(root, ready=True):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=root)), indicators_ready=ready,
    )))


def test_restart_serves_last_snapshot_during_indicator_warmup(tmp_path, monkeypatch):
    path = tmp_path / ".data_statistics" / "status.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "data": {"daily": {"rows": 12}, "checked_at": "previous"}}))

    def unexpected(request):
        raise AssertionError("warmup must not start another heavy statistics job")

    monkeypatch.setattr(data_api, "_compute_status", unexpected)
    result = data_api.status(_request(tmp_path, ready=False))
    assert result["daily"] == {"rows": 12}
    assert result["checked_at"] == "previous"
    assert result["statistics_loading"] and not result["indicators_ready"]


def test_first_request_returns_while_slow_statistics_refreshes_once(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def compute(request):
        calls.append(True)
        started.set()
        assert release.wait(5)
        return {"daily": {"rows": 12}, "checked_at": "fresh"}

    monkeypatch.setattr(data_api, "_compute_status", compute)
    try:
        result = data_api.status(_request(tmp_path))
        assert started.wait(1)
        assert result["statistics_loading"] and result["daily"] is None
        assert data_api.status(_request(tmp_path))["statistics_loading"]
        assert calls == [True]
    finally:
        release.set()
    _wait_for_refresh()
    assert data_api.status(_request(tmp_path))["daily"] == {"rows": 12}
    assert json.loads((tmp_path / ".data_statistics/status.json").read_text())["data"]["checked_at"] == "fresh"


def test_invalidation_rejects_inflight_snapshot(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def compute(request):
        started.set()
        assert release.wait(5)
        return {"daily": {"rows": 1}}

    monkeypatch.setattr(data_api, "_compute_status", compute)
    try:
        data_api.status(_request(tmp_path))
        assert started.wait(1)
        data_api.invalidate_data_cache("daily")
    finally:
        release.set()
    _wait_for_refresh()
    assert data_api._status_snapshot is None
    assert not (tmp_path / ".data_statistics/status.json").exists()


def test_empty_repository_can_finish_statistics_without_ready_indicators(tmp_path, monkeypatch):
    request = _request(tmp_path, ready=False)
    request.app.state.repo._enriched_warming = False
    monkeypatch.setattr(data_api, "_compute_status", lambda request: {"daily": None, "checked_at": "fresh"})
    data_api.status(request)
    _wait_for_refresh()
    assert data_api.status(request)["statistics_loading"] is False
