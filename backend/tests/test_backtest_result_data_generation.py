"""Completed SSE results must not survive a change of their market data."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import backtest
from app.backtest import worker
from app.config import settings
from app.tickflow.repository import KlineRepository


def test_completed_backtest_is_recomputed_after_data_changes(monkeypatch):
    generation = ["first"]
    app = FastAPI()
    app.include_router(backtest.router)
    app.state.repo = SimpleNamespace(get_backtest_data_generation=lambda _: generation[0])
    monkeypatch.setattr(backtest, "_running_jobs", {})
    monkeypatch.setattr(backtest, "_guard_minute_strategy_backtest", lambda *args: None)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    execute = Mock(side_effect=[{"stats": {"marker": 1}}, {"stats": {"marker": 2}}])
    monkeypatch.setattr(worker, "run_worker_task", execute)
    params = {"strategy_id": "test", "start": "2020-01-01", "end": "2020-12-31"}

    with TestClient(app) as client:
        assert '"marker": 1' in client.get("/api/backtest/strategy/stream", params=params).text
        assert '"marker": 1' in client.get("/api/backtest/strategy/stream", params=params).text
        assert execute.call_count == 1
        generation[0] = "second"
        assert '"marker": 2' in client.get("/api/backtest/strategy/stream", params=params).text
    assert execute.call_count == 2


def test_backtest_generation_tracks_enriched_and_benchmark(monkeypatch, tmp_path):
    repo = object.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    generation = ["first"]
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _: generation[0])
    first = repo.get_backtest_data_generation("stock")
    assert repo.get_backtest_data_generation("stock") == first
    generation[0] = "second"
    second = repo.get_backtest_data_generation("stock")
    assert second != first
    index = tmp_path / "kline_index_daily/date=2026-10-16/part.parquet"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"benchmark-a")
    third = repo.get_backtest_data_generation("stock")
    assert third != second
    index.write_bytes(b"benchmark-updated")
    assert repo.get_backtest_data_generation("stock") != third


def test_reconnect_keeps_running_job_even_if_data_generation_changes(monkeypatch):
    generation = ["first"]
    app = FastAPI()
    app.state.repo = SimpleNamespace(get_backtest_data_generation=lambda _: generation[0])
    monkeypatch.setattr(backtest, "_running_jobs", {})
    monkeypatch.setattr(backtest, "_guard_minute_strategy_backtest", lambda *args: None)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    request = Request({"type": "http", "app": app})

    async def connect():
        return await backtest.strategy_stream(request, "test", start="2020-01-01", end="2020-12-31")

    asyncio.run(connect())
    job = next(iter(backtest._running_jobs.values()))
    generation[0] = "second"
    asyncio.run(connect())
    assert list(backtest._running_jobs.values()) == [job]
    assert job.done is False
