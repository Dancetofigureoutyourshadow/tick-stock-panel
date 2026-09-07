from __future__ import annotations

import os

from app.backtest import matrix


def test_publish_replaces_stale_incomplete_cache_directory(tmp_path, monkeypatch):
    target = tmp_path / "v4-cache"
    target.mkdir()
    (target / "matrix.bin").write_bytes(b"incomplete")
    temporary = tmp_path / ".v4-cache.build.tmp"
    temporary.mkdir()
    (temporary / "manifest.json").write_text("{}", encoding="utf-8")

    real_replace = os.replace
    calls = {"count": 0}

    def replace_with_transient_windows_error(source, destination):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(5, "access denied")
        return real_replace(source, destination)

    monkeypatch.setattr(matrix.os, "replace", replace_with_transient_windows_error)

    matrix._publish_matrix_cache_directory(temporary, target)

    assert calls["count"] == 2
    assert not temporary.exists()
    assert (target / "manifest.json").read_text(encoding="utf-8") == "{}"


def test_publish_keeps_completed_cache_when_target_wins_race(tmp_path, monkeypatch):
    target = tmp_path / "v4-cache"
    target.mkdir()
    (target / "manifest.json").write_text("{\"version\":4}", encoding="utf-8")
    temporary = tmp_path / ".v4-cache.build.tmp"
    temporary.mkdir()
    (temporary / "manifest.json").write_text("{\"version\":4}", encoding="utf-8")

    monkeypatch.setattr(matrix.os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError(5, "access denied")))

    matrix._publish_matrix_cache_directory(temporary, target)

    assert not temporary.exists()
    assert target.joinpath("manifest.json").read_text(encoding="utf-8") == "{\"version\":4}"
