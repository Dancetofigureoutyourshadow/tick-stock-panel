"""数据状态接口 busy 降级 — 同步任务期间 /status 不再重算聚合。

回归 (2026-10-28 用户反馈): 同步任务运行时前端把 /api/data/status 轮询加速到
2s, 而聚合里的 DuckDB 查询与管道共用 repository 单连接全局锁 — 管道持锁跑长
查询时请求在锁上排队堆积, 反复触发前端 30s 超时 (请求超时（30s）/api/data/status)。
修复: busy (活跃任务/指标预热中) 时聚合退化为纯缓存读 (最后一次非空值), 不进锁。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import data as data_api
from app.services.pipeline_jobs import job_store


@pytest.fixture()
def clean_caches(monkeypatch):
    monkeypatch.setattr(job_store, "active_id", lambda: None)
    monkeypatch.setattr(data_api, "_status_snapshot_dir", None)
    data_api.invalidate_data_cache(None, discard_stale=True)
    yield
    data_api.invalidate_data_cache(None, discard_stale=True)


# ---------- _get_table_stats: busy 语义 ----------

def test_busy_serves_stale_without_fetch(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": 42}

    first = data_api._get_table_stats("daily", fetch)
    assert first == {"rows": 42} and len(calls) == 1
    # 失效事件 (stage 写完) 清 TTL 缓存, 但 stale 兜底保留
    data_api.invalidate_data_cache("daily")
    got = data_api._get_table_stats("daily", fetch, busy=True)
    assert got == {"rows": 42}
    assert len(calls) == 1  # busy 期间绝不重算 → 不进 DuckDB 锁


def test_busy_without_stale_returns_none_and_skips_fetch(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": 1}

    assert data_api._get_table_stats("minute", fetch, busy=True) is None
    assert calls == []


def test_not_busy_still_refetches_after_invalidation(clean_caches):
    calls = []

    def fetch():
        calls.append(1)
        return {"rows": len(calls)}

    data_api._get_table_stats("daily", fetch)
    data_api.invalidate_data_cache("daily")
    got = data_api._get_table_stats("daily", fetch)
    assert got == {"rows": 2} and len(calls) == 2  # 原有 TTL+事件语义不变


def test_get_storage_busy_serves_stale(clean_caches, tmp_path):
    data_api._get_storage(tmp_path)          # 首次计算并缓存
    data_api.invalidate_data_cache(None)     # 全清 (storage 一并清空)
    got = data_api._get_storage(tmp_path, busy=True)
    assert got is not None                   # stale 兜底仍在


@pytest.mark.parametrize("stale", [None, {"rows": 42}])
def test_busy_returns_without_waiting_for_inflight_table_refresh(clean_caches, stale):
    if stale is not None:
        data_api._get_table_stats("daily", lambda: stale)
        data_api.invalidate_data_cache("daily")
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch():
        calls.append(True)
        started.set()
        assert release.wait(5)
        return {"rows": 99}

    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh = pool.submit(data_api._get_table_stats, "daily", fetch)
        try:
            assert started.wait(5)  # fetch holds the per-table refresh lock
            busy_read = pool.submit(data_api._get_table_stats, "daily", fetch, busy=True)
            assert busy_read.result(timeout=1) == stale
            assert not refresh.done()
            assert calls == [True]
        finally:
            # A regression must fail with a timeout, not hang executor shutdown.
            release.set()
        assert refresh.result(timeout=5) == {"rows": 99}


@pytest.mark.parametrize("table", ["daily", "minute"])
def test_empty_table_result_obeys_ttl_and_preserves_last_nonempty_stats(
    clean_caches, monkeypatch, table,
):
    now = [1000.0]
    monkeypatch.setattr(data_api.time, "time", lambda: now[0])
    data_api._get_table_stats(table, lambda: {"rows": 42})
    data_api.invalidate_data_cache(table)
    calls = []

    def fetch():
        calls.append(True)
        return None

    assert data_api._get_table_stats(table, fetch) is None
    ttl = data_api._TABLE_TTL_LARGE if table == "minute" else data_api._TABLE_TTL
    now[0] += ttl - 1
    assert data_api._get_table_stats(table, fetch) is None
    assert calls == [True]
    assert data_api._get_table_stats(table, fetch, busy=True) == {"rows": 42}
    now[0] += 2
    assert data_api._get_table_stats(table, fetch) is None
    assert len(calls) == 2
    data_api.invalidate_data_cache(table)
    assert data_api._get_table_stats(table, fetch) is None
    assert len(calls) == 3


@pytest.mark.parametrize("scope", ["daily", None])
def test_invalidation_during_table_fetch_does_not_refill_current_or_stale(
    clean_caches, scope,
):
    previous = {"rows": 42}
    data_api._get_table_stats("daily", lambda: previous)
    data_api.invalidate_data_cache("daily")

    def fetch():
        data_api.invalidate_data_cache(scope)
        return {"rows": 99}

    assert data_api._get_table_stats("daily", fetch) == {"rows": 99}
    assert data_api._table_cache["daily"] is None
    assert data_api._table_cache_ts["daily"] == 0
    assert data_api._table_cache_stale["daily"] == previous
    assert data_api._get_table_stats("daily", fetch, busy=True) == previous
    assert data_api._get_table_stats("daily", lambda: {"rows": 2}) == {"rows": 2}
    assert data_api._get_table_stats("daily", fetch, busy=True) == {"rows": 2}


def test_invalidation_during_storage_scan_does_not_refill_current_or_stale(
    clean_caches, monkeypatch, tmp_path,
):
    previous = {"total_size_mb": 42}
    monkeypatch.setattr(data_api, "_compute_storage", lambda root: previous)
    assert data_api._get_storage(tmp_path) == previous
    data_api.invalidate_data_cache(None)

    def compute(root):
        assert root == tmp_path
        data_api.invalidate_data_cache(None)
        return {"total_size_mb": 99}

    monkeypatch.setattr(data_api, "_compute_storage", compute)
    assert data_api._get_storage(tmp_path) == {"total_size_mb": 99}
    assert data_api._storage_cache is None
    assert data_api._storage_cache_ts == 0
    assert data_api._storage_cache_stale == previous
    assert data_api._get_storage(tmp_path, busy=True) == previous
    monkeypatch.setattr(data_api, "_compute_storage", lambda root: {"total_size_mb": 2})
    assert data_api._get_storage(tmp_path) == {"total_size_mb": 2}
    assert data_api._get_storage(tmp_path, busy=True) == {"total_size_mb": 2}


def test_get_storage_busy_cold_start_returns_none(clean_caches, tmp_path):
    """服务刚启动、storage 缓存从未热过时, busy 期间返回 None (契约)。

    /api/data/status 的消费方 (如数据页存储卡片) 必须按 null 降级 —
    直接 s.storage.total_size_mb 会在预热/同步期间抛 TypeError
    (v0.3.22 修复 "Cannot read properties of null (reading 'total_size_mb')" 的依据)。
    """
    assert data_api._get_storage(tmp_path, busy=True) is None


# ---------- busy 判定 ----------

def _req(indicators_ready=True):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(indicators_ready=indicators_ready))
    )


def test_busy_when_indicators_warming(clean_caches):
    assert data_api._data_pipeline_busy(_req(indicators_ready=False)) is True


def test_busy_follows_active_job(monkeypatch, clean_caches):
    monkeypatch.setattr(job_store, "active_id", lambda: None)
    assert data_api._data_pipeline_busy(_req()) is False
    monkeypatch.setattr(job_store, "active_id", lambda: "abc123")
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "running"})
    assert data_api._data_pipeline_busy(_req()) is True
    # 终态任务不算 busy (防僵尸 active_id 把 /status 永久钉死在缓存)
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "succeeded"})
    assert data_api._data_pipeline_busy(_req()) is False


# ---------- status 端点: busy 接线 ----------

def test_status_endpoint_busy_skips_aggregates(monkeypatch, clean_caches, tmp_path):
    """indicators_ready=False → busy: 聚合全部跳过 (不触碰 repo), 轻字段照常返回。"""
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path)))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=repo, scheduler=None, indicators_ready=False,
    )))
    # 任何聚合尝试都算失败 — busy 下不应被调用
    def _boom(*_a, **_k):
        raise AssertionError("busy 下不应重算聚合")
    for fn in data_api._table_cache:
        monkeypatch.setattr(data_api, f"_safe_aggregate_{fn}", _boom)
    monkeypatch.setattr(data_api, "_compute_storage", _boom)
    monkeypatch.setattr(data_api, "threading", SimpleNamespace(Thread=_boom))
    res = data_api.status(req)
    assert res["indicators_ready"] is False
    assert res["checked_at"] is None
    assert res["statistics_loading"] is True
    # 上游契约 (v0.3.22): null 统计是「待定」而非「本地无数据」, 降级返回同样要带标记
    assert res["stats_pending"] is True
    assert all(res[name] is None for name in data_api._table_cache)
    assert not data_api._status_refreshing
    assert not (tmp_path / ".data_statistics" / "status.json").exists()


@pytest.mark.parametrize("job_status", ["pending", "running"])
def test_stats_pending_tracks_active_job(monkeypatch, clean_caches, tmp_path, job_status):
    """同步/修复仍在运行时, null 统计不代表无数据; 任务终态后统计照常补齐。

    dev 口径下 status() 只回「最近一次完成」的快照, 聚合在后台刷新线程里算,
    因此这里直接验证计算层 _compute_status 的 stats_pending 契约。
    """
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=Path(tmp_path))),
        scheduler=None, indicators_ready=True,
    )))
    monkeypatch.setattr(job_store, "active_id", lambda: "repair-job")
    monkeypatch.setattr(job_store, "get", lambda jid: {"status": job_status})
    monkeypatch.setattr(data_api, "_last_finished", lambda label: None)
    monkeypatch.setattr(
        data_api, "_get_storage",
        lambda root, busy=False: None if busy else {"total_size_mb": 1},
    )
    calls: list[str] = []

    def aggregate(table: str):
        def _fn(_repo):
            calls.append(table)
            return {"trading_days": 240}
        return _fn

    for table in data_api._table_cache:
        monkeypatch.setattr(data_api, f"_safe_aggregate_{table}", aggregate(table))

    busy = data_api._compute_status(req)
    assert busy["indicators_ready"] is True
    assert busy["stats_pending"] is True
    assert busy["daily"] is None and busy["enriched"] is None
    assert calls == []  # busy 下不重算 → 不进 DuckDB 锁

    monkeypatch.setattr(job_store, "get", lambda jid: {"status": "succeeded"})
    ready = data_api._compute_status(req)
    assert ready["stats_pending"] is False
    assert ready["daily"]["trading_days"] == 240
    assert ready["enriched"]["trading_days"] == 240
    assert sorted(calls) == sorted(data_api._table_cache)
