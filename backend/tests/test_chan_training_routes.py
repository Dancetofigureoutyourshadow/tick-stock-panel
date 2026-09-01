from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.custom import chan_training as routes


async def _events(response: Any) -> list[dict[str, Any]]:
    body = b""
    async for chunk in response.body_iterator:
        body += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    return [json.loads(line) for line in body.decode("utf-8").splitlines()]


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=object())))


class _ReportStore:
    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    def save_report(self, report: dict[str, Any]) -> dict[str, Any]:
        saved = {**report, "id": f"report-{len(self.saved) + 1}"}
        self.saved.append(saved)
        return saved


@pytest.mark.asyncio
async def test_ai_unconfigured_keeps_training_record_and_does_not_save_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {"training_id": "training-1", "symbol": "000001.SZ", "name": "测试"}
    store = _ReportStore()

    async def unavailable(*_args: Any, **_kwargs: Any):
        raise RuntimeError("AI 未配置")
        yield ""  # pragma: no cover - keep this an async generator

    monkeypatch.setattr(routes.chan_training, "get_record", lambda *_args: record)
    monkeypatch.setattr(routes.chan_training, "build_ai_messages", lambda _record: [])
    monkeypatch.setattr(routes.chan_training, "record_store", lambda: store)
    monkeypatch.setattr(routes, "stream_ai_text", unavailable)

    response = await routes.analyze("training-1", _request())
    events = await _events(response)

    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["message"] == "AI 未配置"
    assert store.saved == []
    assert record["training_id"] == "training-1"


@pytest.mark.asyncio
async def test_ai_failure_can_retry_and_only_success_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = {"training_id": "training-2", "symbol": "000001.SZ", "name": "测试"}
    store = _ReportStore()
    attempts = 0

    async def flaky(*_args: Any, **_kwargs: Any):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("上游暂时失败")
        yield "仓位管理分析"

    monkeypatch.setattr(routes.chan_training, "get_record", lambda *_args: record)
    monkeypatch.setattr(routes.chan_training, "build_ai_messages", lambda _record: [])
    monkeypatch.setattr(routes.chan_training, "record_store", lambda: store)
    monkeypatch.setattr(routes, "stream_ai_text", flaky)

    first = await _events(await routes.analyze("training-2", _request()))
    second = await _events(await routes.analyze("training-2", _request()))

    assert [event["type"] for event in first] == ["meta", "error"]
    assert [event["type"] for event in second] == ["meta", "delta", "done"]
    assert attempts == 2
    assert len(store.saved) == 1
    assert store.saved[0]["training_id"] == "training-2"
    assert store.saved[0]["content"] == "仓位管理分析"
