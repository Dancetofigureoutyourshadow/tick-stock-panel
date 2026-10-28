"""Partial network failures must reach the pipeline, not remain log-only."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl

from app.services import kline_sync


def test_persist_daily_reports_failed_symbols_and_keeps_successful_rows(monkeypatch, tmp_path):
    frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 10, 16)], "close": [10.0]})

    def fetch(*args, **kwargs):
        kwargs["failed_out"].append("000001.SZ")
        return frame

    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "sync_daily_batch", fetch)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *args, **kwargs: SimpleNamespace(batch=100, rpm=None))
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), append_daily=Mock(),
        refresh_view=Mock(), db=Mock(),
    )
    failed = []

    written = kline_sync.sync_and_persist_daily_batch(
        ["600000.SH", "000001.SZ"], repo, SimpleNamespace(has=lambda _: True), failed_out=failed,
    )

    assert written == 1
    assert failed == ["000001.SZ"]
    repo.append_daily.assert_called_once()


def test_adj_factor_reports_failed_batches(monkeypatch, tmp_path):
    client = SimpleNamespace(klines=SimpleNamespace(ex_factors=Mock(side_effect=RuntimeError("network failed"))))
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)
    monkeypatch.setattr(kline_sync, "resolve_limit", lambda *args, **kwargs: SimpleNamespace(batch=1, rpm=None))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *args: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    failed = []

    assert kline_sync.sync_adj_factor(
        ["600000.SH", "000001.SZ"], repo, SimpleNamespace(has=lambda _: True), failed_out=failed,
    ) == (0, [])
    assert failed == ["600000.SH", "000001.SZ"]
