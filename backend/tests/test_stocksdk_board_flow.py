from __future__ import annotations

from datetime import datetime

from app.services import sector_rotation, stocksdk_board_flow


def _rows(net: float, pct: float = 0.01) -> list[dict]:
    return [{
        "code": "BK0001",
        "name": "半导体",
        "pct": pct,
        "inflow": None,
        "outflow": None,
        "net": net,
        "member_count": 0,
        "kind": "concept",
    }]


def test_parse_stocksdk_rows_uses_main_net_inflow_in_yuan():
    rows = stocksdk_board_flow._parse_rows(
        [{"code": "BK0001", "name": "半导体", "mainNetInflow": 226600000, "changePercent": 4.31}],
        "industry",
    )
    assert rows == [{
        "code": "BK0001",
        "name": "半导体",
        "pct": 0.0431,
        "inflow": None,
        "outflow": None,
        "net": 226600000.0,
        "member_count": 0,
        "kind": "industry",
    }]


def test_snapshot_builds_minute_series_and_adjacent_net_deltas(monkeypatch):
    stocksdk_board_flow.invalidate_cache()
    calls = iter([
        (_rows(5e7), datetime(2026, 9, 18, 9, 35)),
        (_rows(8e7, 0.02), datetime(2026, 9, 18, 9, 36)),
    ])
    monkeypatch.setattr(stocksdk_board_flow, "_fetch", lambda kind: next(calls))
    monkeypatch.setattr(stocksdk_board_flow, "_POLL_TTL", 0.0)
    first = stocksdk_board_flow.snapshot("concept")
    second = stocksdk_board_flow.snapshot("concept")
    assert first["status"] == second["status"] == "ok"
    assert second["series"]["buckets"][0] == "09:30"
    assert second["series"]["buckets"][-1] == "15:00"
    assert second["series"]["observed_buckets"] == ["09:35", "09:36"]
    net_index = second["series"]["buckets"].index("09:36")
    assert second["series"]["net"][net_index][0] == 8e7
    assert second["series"]["delta_net"][net_index][0] == 3e7
    assert second["series"]["inflow"][net_index][0] is None


def test_session_axis_skips_lunch_and_keeps_fixed_window():
    buckets = stocksdk_board_flow._session_buckets()
    assert buckets[0] == "09:30"
    assert buckets[-1] == "15:00"
    assert "11:30" in buckets and "13:00" in buckets
    assert "12:00" not in buckets
    assert len(buckets) == 242


def test_session_history_persists_and_reloads_after_restart(tmp_path, monkeypatch):
    stocksdk_board_flow.invalidate_cache()
    data_dir = tmp_path / "data"
    calls = iter([
        (_rows(5e7), datetime(2026, 9, 18, 9, 30)),
        (_rows(7e7), datetime(2026, 9, 18, 9, 31)),
    ])
    monkeypatch.setattr(stocksdk_board_flow, "_fetch", lambda kind: next(calls))
    monkeypatch.setattr(stocksdk_board_flow, "_POLL_TTL", 0.0)
    first = stocksdk_board_flow.snapshot("industry", data_dir=data_dir)
    assert first["history_complete"] is True
    assert (data_dir / "sector_flow_stocksdk" / "date=2026-09-18" / "industry.json").exists()

    stocksdk_board_flow.invalidate_cache()
    second = stocksdk_board_flow.snapshot("industry", data_dir=data_dir)
    assert second["series"]["observed_buckets"] == ["09:30", "09:31"]
    assert second["history_complete"] is True


def test_unavailable_is_not_disguised_as_zero(monkeypatch):
    stocksdk_board_flow.invalidate_cache()
    monkeypatch.setattr(stocksdk_board_flow, "_fetch", lambda kind: (_ for _ in ()).throw(RuntimeError("node unavailable")))
    result = stocksdk_board_flow.snapshot("industry")
    assert result["status"] == "unavailable"
    assert result["reason"] == "source_unavailable"
    assert "rows" not in result


def test_stock_sdk_source_is_used_by_sector_rotation(monkeypatch):
    called: list[str] = []

    def fake(kind: str, **_: object) -> dict:
        called.append(kind)
        return {"status": "unavailable", "reason": "source_unavailable", "kind": kind}

    monkeypatch.setattr(sector_rotation, "stocksdk_flow_snapshot", fake)
    assert sector_rotation.stocksdk_flow_snapshot("concept")["reason"] == "source_unavailable"
    assert called == ["concept"]
