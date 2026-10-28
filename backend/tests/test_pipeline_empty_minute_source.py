"""An empty full-market source also disables post-close bulk minute sync."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.jobs import daily_pipeline
from app.services import data_integrity, preferences
from app.strategy import paper
from app.tickflow.capabilities import Cap


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    prefs_path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: prefs_path)
    preferences._invalidate_cache()
    preferences.save({
        "pipeline_pull_a_share": False,
        "pipeline_pull_index": False,
        "pipeline_pull_etf": False,
        "pipeline_regime_enabled": False,
        "adj_factor_provider": "tickflow",
        "minute_sync_enabled": True,
    })
    # 默认用例不依赖测试运行时钟；盘后确认用例会显式开启该分支。
    monkeypatch.setattr(daily_pipeline, "is_after_official_daily_cutoff", lambda *args: False)
    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", lambda _: 0)
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda *args: ["600000.SH"])
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *args, **kwargs: [])
    monkeypatch.setattr(daily_pipeline, "run_pipeline", lambda **kwargs: 0)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *args: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *args: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *args: None)
    monkeypatch.setattr(daily_pipeline, "warn_if_enriched_too_thin", lambda *args: 0)
    monkeypatch.setattr(paper, "list_account_ids", lambda *args: [])
    resolve = Mock(return_value=["600000.SH"])
    fetch = Mock(return_value=0)
    monkeypatch.setattr(daily_pipeline, "_resolve_minute_symbols", resolve)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_minute", fetch)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: None)
    capset = SimpleNamespace(has=lambda cap: cap == Cap.KLINE_MINUTE_BATCH)
    yield repo, capset, resolve, fetch
    preferences._invalidate_cache()


@pytest.mark.parametrize("minute_enabled", [True, False])
def test_empty_source_skips_pipeline_minute_without_fetching(pipeline, minute_enabled):
    repo, capset, resolve, fetch = pipeline
    preferences.save({"full_minute_data_provider": "none", "minute_sync_enabled": minute_enabled})
    progress = Mock()

    result = daily_pipeline.run_now(repo, capset, on_progress=progress)

    assert result["minute_rows"] == 0
    assert "sync_minute" in result["skipped_stages"]
    assert result["stage_errors"] == []
    resolve.assert_not_called()
    fetch.assert_not_called()
    assert all(call.args[0] != "sync_minute" for call in progress.call_args_list)


@pytest.mark.parametrize("provider", ["tickflow", "fuyao"])
def test_enabled_source_still_reports_real_zero_row_failure(pipeline, provider):
    repo, capset, resolve, fetch = pipeline
    preferences.save({"full_minute_data_provider": provider})

    with pytest.raises(daily_pipeline.PipelineStageError, match="sync_minute: no rows written"):
        daily_pipeline.run_now(repo, capset)

    resolve.assert_called_once()
    fetch.assert_called_once()


def test_enabled_source_keeps_successful_pipeline_minute_sync(pipeline):
    repo, capset, resolve, fetch = pipeline
    preferences.save({"full_minute_data_provider": "tickflow"})
    fetch.return_value = 240

    result = daily_pipeline.run_now(repo, capset)

    assert result["minute_rows"] == 240
    assert "sync_minute" not in result["skipped_stages"]
    assert result["stage_errors"] == []
    resolve.assert_called_once()
    fetch.assert_called_once()


@pytest.mark.parametrize("stage", ["daily", "adj"])
def test_pipeline_reports_partial_fetch_failure(pipeline, monkeypatch, stage):
    repo, _, _, _ = pipeline
    preferences.save({"pipeline_pull_a_share": True, "full_minute_data_provider": "none"})
    caps = SimpleNamespace(has=lambda cap: cap in {Cap.KLINE_DAILY_BATCH, Cap.ADJ_FACTOR})

    def daily(*args, **kwargs):
        if stage == "daily":
            kwargs.get("failed_out", []).append("600000.SH")
        return 1

    def adj(*args, **kwargs):
        if stage == "adj":
            kwargs.get("failed_out", []).append("600000.SH")
        return 0, []

    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_daily_batch", daily)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_adj_factor", adj)

    with pytest.raises(daily_pipeline.PipelineStageError, match=f"sync_{stage}"):
        daily_pipeline.run_now(repo, caps)


def test_paper_confirmation_waits_for_successful_full_sync(pipeline, monkeypatch):
    repo, capset, _, _ = pipeline
    preferences.save({"full_minute_data_provider": "none"})
    monkeypatch.setattr(daily_pipeline, "is_after_official_daily_cutoff", lambda: True)
    calls = []
    repo.refresh_cache = lambda: calls.append("refresh")
    monkeypatch.setattr(daily_pipeline, "_settle_paper_day", lambda *args: calls.append("settle") or {})
    monkeypatch.setattr(daily_pipeline, "_run_paper_daily_phase",
                        lambda phase, *args, **kwargs: calls.append((phase, kwargs)))

    daily_pipeline.run_now(repo, capset)

    # 收盘卖出先用同步后的权威数据确认并提交, 再结算; 次日开盘模式在结算后确认。
    assert calls == [
        "refresh",
        ("post_close_sell", {"skip_reason": "未完成全量A股日K同步"}),
        "settle",
        ("post_close", {"skip_reason": "未完成全量A股日K同步"}),
    ]


def test_paper_confirmation_skips_failed_pipeline_before_settlement(pipeline, monkeypatch):
    repo, capset, _, _ = pipeline
    preferences.save({"full_minute_data_provider": "tickflow"})
    monkeypatch.setattr(daily_pipeline, "is_after_official_daily_cutoff", lambda: True)
    monkeypatch.setattr(daily_pipeline, "_settle_paper_day", lambda *args: pytest.fail("failed pipeline must not settle"))
    skip = Mock()
    monkeypatch.setattr(daily_pipeline, "_run_paper_daily_phase", skip)

    with pytest.raises(daily_pipeline.PipelineStageError):
        daily_pipeline.run_now(repo, capset)

    assert [call.args[0] for call in skip.call_args_list] == ["post_close_sell", "post_close"]
    assert "全量同步未完成" in skip.call_args.kwargs["skip_reason"]


@pytest.mark.parametrize("mode", ["initial", "today", "repair"])
def test_zero_row_daily_sync_fails_before_paper_settlement(pipeline, monkeypatch, mode):
    """上游返回成功但全市场零行时，不能拿旧快照结算并宣告管道成功。"""
    repo, _, _, _ = pipeline
    preferences.save({"pipeline_pull_a_share": True, "full_minute_data_provider": "none"})
    caps = SimpleNamespace(has=lambda cap: cap == Cap.KLINE_DAILY_BATCH)
    monkeypatch.setattr(daily_pipeline, "is_after_official_daily_cutoff", lambda: True)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_daily_batch", lambda *a, **kw: 0)
    repo.refresh_cache = Mock()
    if mode != "initial":
        repo.latest_daily_date = daily_pipeline.cn_today
    settle = Mock(return_value={})
    confirm = Mock()
    monkeypatch.setattr(daily_pipeline, "_settle_paper_day", settle)
    monkeypatch.setattr(daily_pipeline, "_run_paper_daily_phase", confirm)

    with pytest.raises(daily_pipeline.PipelineStageError, match="sync_daily: no rows written"):
        daily_pipeline.run_now(
            repo, caps,
            override_start_date=daily_pipeline.cn_today() if mode == "repair" else None,
        )

    settle.assert_not_called()
    assert "全量同步未完成" in confirm.call_args.kwargs["skip_reason"]
