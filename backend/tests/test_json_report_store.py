from __future__ import annotations

import json

import pytest

from app.services import json_report_store
from app.services.json_report_store import JsonReportStore


def test_json_report_store_uses_atomic_replace_and_reads_saved_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage: dict[str, str] = {}

    class MemoryPath:
        def __init__(self, name: str) -> None:
            self.name = name

        @property
        def suffix(self) -> str:
            index = self.name.rfind(".")
            return self.name[index:] if index >= 0 else ""

        def with_suffix(self, suffix: str) -> MemoryPath:
            base = self.name[:-len(self.suffix)] if self.suffix else self.name
            return MemoryPath(base + suffix)

        def write_text(self, value: str, encoding: str) -> None:
            assert encoding == "utf-8"
            storage[self.name] = value

        def read_text(self, encoding: str) -> str:
            assert encoding == "utf-8"
            return storage[self.name]

        def exists(self) -> bool:
            return self.name in storage

    path = MemoryPath("chan_training_records.json")
    store = JsonReportStore(path.name, 10, "ctr")
    monkeypatch.setattr(store, "_path", lambda: path)
    replacements: list[tuple[str, str]] = []

    def replace(source: MemoryPath, target: MemoryPath) -> None:
        replacements.append((source.name, target.name))
        storage[target.name] = storage.pop(source.name)

    monkeypatch.setattr(json_report_store.os, "replace", replace)

    saved = store.save_report({"training_id": "training-1", "created_at": "2026-08-28T12:00:00"})

    assert replacements == [("chan_training_records.json.tmp", "chan_training_records.json")]
    assert "chan_training_records.json.tmp" not in storage
    assert json.loads(storage["chan_training_records.json"])[0]["id"] == saved["id"]
    assert store.list_reports() == [saved]
