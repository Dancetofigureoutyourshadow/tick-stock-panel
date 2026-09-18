from __future__ import annotations

import json
import random
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import blind_training


def _rows(count: int = 130) -> list[dict[str, object]]:
    start = date(2024, 1, 1)
    return [
        {
            "symbol": "000001",
            "date": (start + timedelta(days=index)).isoformat(),
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0,
            "volume": 100000.0,
        }
        for index in range(count)
    ]


def _state() -> dict[str, object]:
    state = blind_training._new_state("000001", "测试股票", _rows(), 7, random.Random(1))
    state["start_index"] = 59
    state["context_start"] = 0
    state["cursor"] = 59
    state["decision_end"] = 119
    blind_training._mark_equity(state)
    return state


@pytest.mark.parametrize("name", ["ST测试", "*ST测试", "S*ST测试", "PT测试", "退市测试", "风险警示"])
def test_risk_warning_names_are_not_eligible(name: str) -> None:
    assert blind_training._is_eligible_name(name) is False


def test_ordinary_stock_name_is_eligible() -> None:
    assert blind_training._is_eligible_name("贵州茅台") is True


def test_daily_loader_requests_all_a_share_history() -> None:
    requested: dict[str, object] = {}

    class EmptyFrame:
        def is_empty(self) -> bool:
            return True

    class Repo:
        def get_daily_asset(self, asset_type: str, symbol: str, start: date, end: date, **kwargs: object) -> EmptyFrame:
            requested.update(asset_type=asset_type, symbol=symbol, start=start, end=end, **kwargs)
            return EmptyFrame()

    assert blind_training._load_rows(Repo(), "000001.SZ") == []
    assert requested["asset_type"] == "stock"
    assert requested["symbol"] == "000001.SZ"
    assert requested["start"] == date(1990, 1, 1)
    assert requested["end"] == blind_training.cn_today()
    assert "signal_limit_up" in requested["columns"]
    assert "signal_limit_down" in requested["columns"]


def test_daily_loader_uses_limit_signal_batch_fast_path() -> None:
    requested: dict[str, object] = {}

    class EmptyFrame:
        def is_empty(self) -> bool:
            return True

    class Repo:
        def get_daily_batch(
            self,
            symbols: list[str],
            start: date,
            end: date,
            **kwargs: object,
        ) -> EmptyFrame:
            requested.update(symbols=symbols, start=start, end=end, **kwargs)
            return EmptyFrame()

        def get_daily_asset(self, *_args: object, **_kwargs: object) -> EmptyFrame:
            raise AssertionError("single-symbol enriched fallback should not run")

    start = date(2020, 1, 1)
    end = date(2020, 12, 31)
    assert blind_training._load_rows(Repo(), "000001.SZ", start, end) == []
    assert requested["symbols"] == ["000001.SZ"]
    assert requested["start"] == start
    assert requested["end"] == end
    assert "signal_limit_up" in requested["columns"]


def test_daily_loader_preserves_limit_flags_for_chart_coloring() -> None:
    class Repo:
        def get_daily_asset(self, *_args: object, **_kwargs: object) -> pl.DataFrame:
            return pl.DataFrame([
                {
                    "symbol": "000001.SZ",
                    "date": date(2024, 1, 2),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 10.0,
                    "close": 11.0,
                    "volume": 100000.0,
                    "signal_limit_up": True,
                    "signal_limit_down": False,
                },
                {
                    "symbol": "000001.SZ",
                    "date": date(2024, 1, 3),
                    "open": 10.0,
                    "high": 10.0,
                    "low": 9.0,
                    "close": 9.0,
                    "volume": 120000.0,
                    "signal_limit_up": None,
                    "signal_limit_down": True,
                },
            ])

    rows = blind_training._load_rows(Repo(), "000001.SZ")

    assert rows[0]["signal_limit_up"] is True
    assert rows[0]["signal_limit_down"] is False
    assert rows[1]["signal_limit_up"] is False
    assert rows[1]["signal_limit_down"] is True


def test_limit_flags_remain_in_active_and_saved_chart_rows() -> None:
    state = _state()
    state["rows"][0]["signal_limit_up"] = True
    state["rows"][1]["signal_limit_down"] = True

    active = blind_training._snapshot(state)
    saved = blind_training._saved_record(state)

    assert active["rows"][0]["signal_limit_up"] is True
    assert active["rows"][1]["signal_limit_down"] is True
    assert saved["bars"][0]["signal_limit_up"] is True
    assert saved["bars"][1]["signal_limit_down"] is True


def test_record_detail_backfills_limit_flags_without_rewriting_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "id": "record-limit-colors",
        "training_id": "training-limit-colors",
        "symbol": "000001.SZ",
        "bars": _rows(2),
    }

    class Store:
        def list_reports(self) -> list[dict[str, object]]:
            return [stored]

    class Repo:
        def get_daily_asset(self, *_args: object, **_kwargs: object) -> pl.DataFrame:
            return pl.DataFrame([
                {
                    "date": stored["bars"][0]["date"],
                    "signal_limit_up": True,
                    "signal_limit_down": False,
                },
                {
                    "date": stored["bars"][1]["date"],
                    "signal_limit_up": False,
                    "signal_limit_down": True,
                },
            ])

    monkeypatch.setattr(blind_training, "_record_store", Store())
    monkeypatch.setattr(blind_training, "_report_store", Store())

    detail = blind_training.get_record("training-limit-colors", Repo())

    assert detail["bars"][0]["signal_limit_up"] is True
    assert detail["bars"][1]["signal_limit_down"] is True
    assert "signal_limit_up" not in stored["bars"][0]


def test_buy_and_sell_use_lots_and_t_plus_one() -> None:
    state = _state()

    buy = blind_training._buy(state, 100)
    assert buy["shares"] % 100 == 0
    assert buy["shares"] > 0
    assert buy["execution_price"] == pytest.approx(10.0)
    assert buy["slippage_amount"] > 0
    assert state["cash"] < blind_training.INITIAL_CAPITAL

    with pytest.raises(ValueError, match=r"T\+1"):
        blind_training._sell(state, 100)

    state["cursor"] = 60
    sell = blind_training._sell(state, 50)
    assert sell["shares"] % 100 == 0
    assert 0 < sell["shares"] < buy["shares"]
    assert sell["commission"] > 0
    assert sell["stamp_tax"] > 0


def test_custom_cost_model_is_used_for_execution() -> None:
    state = _state()
    state["cost_model"] = {
        "commission_pct": 0.001,
        "stamp_tax_pct": 0.002,
        "slippage_bps": 0.0,
    }

    buy = blind_training._buy(state, 10)
    assert buy["execution_price"] == pytest.approx(10.0)
    assert buy["commission"] == pytest.approx(buy["gross"] * 0.001)
    assert buy["slippage_amount"] == 0
    state["cursor"] = 60
    sell = blind_training._sell(state, 100)
    assert sell["stamp_tax"] == pytest.approx(sell["gross"] * 0.002)


def test_position_shortcuts_use_current_cash_and_current_position() -> None:
    state = _state()
    first = blind_training._buy(state, 50)
    cash_before_second = float(state["cash"])
    second = blind_training._buy(state, 50)
    second_cost = second["gross"] + second["commission"] + second["slippage_amount"]
    assert second_cost <= cash_before_second * 0.5
    assert second_cost > 0

    state["cursor"] = 60
    total_shares = blind_training._shares(state)
    sold = blind_training._sell(state, 50)
    assert sold["shares"] == (total_shares // 200) * 100
    assert sold["shares"] > 0
    assert first["shares"] + second["shares"] == total_shares


def test_partial_sell_reduces_remaining_lot_cost_basis() -> None:
    state = _state()
    buy = blind_training._buy(state, 25)
    state["cursor"] = 60

    first_sell = blind_training._sell(state, 50)
    remaining_lot = state["lots"][0]
    assert remaining_lot["shares"] == buy["shares"] - first_sell["shares"]
    assert remaining_lot["cost"] == pytest.approx(
        (buy["gross"] + buy["commission"] + buy["slippage_amount"])
        * remaining_lot["shares"] / buy["shares"],
    )

    second_sell = blind_training._sell(state, 100)
    assert blind_training._shares(state) == 0
    assert first_sell["pnl_amount"] + second_sell["pnl_amount"] == pytest.approx(
        blind_training._equity(state) - blind_training.INITIAL_CAPITAL,
    )



def test_discard_session_removes_active_state_without_saving_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state()

    class Store:
        def save_report(self, _report: dict[str, object]) -> dict[str, object]:
            raise AssertionError("discard must not persist a training record")

    monkeypatch.setattr(blind_training, "_record_store", Store())
    with blind_training._lock:
        blind_training._sessions[state["id"]] = state

    assert blind_training.discard_session(state["id"]) is True
    with pytest.raises(KeyError, match="不存在"):
        blind_training.get_session(state["id"])



def test_active_session_uses_daily_analysis_only() -> None:
    state = _state()

    snapshot = blind_training._snapshot(state)
    record = blind_training._saved_record(state)

    assert snapshot["analysis"]["analysis_mode"] == "blind_test"
    assert snapshot["analysis"]["daily_quality"]["valid"] is True
    assert set(snapshot["analysis"]) == {"analysis_mode", "daily_quality", "data_warnings"}
    assert record["analysis"]["analysis_mode"] == "blind_test"
    assert "minute_source" not in record["data_version"]


def test_finish_advances_for_t_plus_one_and_auto_liquidates(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state()
    blind_training._buy(state, 50)

    class Store:
        def save_report(self, report: dict[str, object]) -> dict[str, object]:
            report["id"] = "ctr_test"
            return report

    monkeypatch.setattr(blind_training, "_record_store", Store())
    with blind_training._lock:
        blind_training._sessions[state["id"]] = state
    try:
        result = blind_training.finish_session(state["id"])
    finally:
        with blind_training._lock:
            blind_training._sessions.pop(state["id"], None)

    assert result["session"]["status"] == "finished"
    assert result["session"]["position"]["shares"] == 0
    assert result["record"]["status"] == "finished"
    assert result["record"]["actions"][-1]["trigger"] == "auto_liquidation"
    assert result["record"]["actions"][-1]["date"] == "2024-03-01"


def test_saved_training_plan_replays_the_exact_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _state()
    record = blind_training._saved_record(original)
    data_version = record["data_version"]
    assert data_version["schema"] == "blind-training-data"
    assert data_version["training_rows_sha256"] == record["training_plan"]["row_digest"]
    assert data_version["visible_rows_sha256"] == blind_training._training_rows_digest(record["bars"])
    assert data_version["training_row_count"] == record["training_plan"]["row_count"]
    assert data_version["visible_row_count"] == len(record["bars"])

    class Store:
        def list_reports(self) -> list[dict[str, object]]:
            return [record]

    monkeypatch.setattr(blind_training, "_record_store", Store())

    replay = blind_training.replay_record(object(), original["id"])
    try:
        replay_state = blind_training.get_session(replay["id"])
        plan = record["training_plan"]
        planned_rows = replay_state["rows"]

        assert replay["id"] != original["id"]
        assert replay["replayed_from"] == original["id"]
        assert replay["progress"] == {"revealed": 0, "total": 60}
        assert replay["cost_model"] == original["cost_model"]
        assert replay_state["start_index"] == original["start_index"]
        assert replay_state["decision_end"] == original["decision_end"]
        assert blind_training._training_rows_digest(planned_rows) == plan["row_digest"]
        assert replay["rows"] == original["rows"][:original["start_index"] + 1]
    finally:
        with blind_training._lock:
            blind_training._sessions.pop(replay["id"], None)


def test_replay_rejects_tampered_serialized_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _state()
    record = blind_training._saved_record(original)
    record["training_plan"]["row_digest"] = "tampered"

    class Store:
        def list_reports(self) -> list[dict[str, object]]:
            return [record]

    monkeypatch.setattr(blind_training, "_record_store", Store())

    with pytest.raises(ValueError, match="校验失败"):
        blind_training.replay_record(object(), original["id"])


def test_training_indices_keep_two_calendar_years_before_initial_bar() -> None:
    rows = _rows(1600)
    anchor = date(2027, 1, 1)

    context_start, start_index = blind_training._training_indices(rows, anchor)

    assert rows[start_index]["date"] == anchor.isoformat()
    assert rows[context_start]["date"] == date(2025, 1, 1).isoformat()
    assert start_index + blind_training.DECISION_BARS + 1 < len(rows)


def test_create_session_filters_risky_and_insufficient_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    valid_rows = _rows()
    requested_ranges: list[tuple[date, date]] = []

    class Frame:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def is_empty(self) -> bool:
            return not self._rows

        def sort(self, _column: str) -> Frame:
            return self

        def to_dicts(self) -> list[dict[str, object]]:
            return self._rows

    class Repo:
        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame([
                {"symbol": "000001", "name": "平安银行"},
                {"symbol": "000002", "name": "ST风险股"},
                {"symbol": "000003", "name": "退市股票"},
            ])

        def get_daily_asset(self, *_args: object, **_kwargs: object) -> Frame:
            requested_ranges.append((_args[2], _args[3]))
            return Frame(valid_rows if _args[1] == "000001" else [])

    monkeypatch.setattr(blind_training.random, "SystemRandom", lambda: random.Random(3))
    monkeypatch.setattr(
        blind_training,
        "_training_query_range",
        lambda *_args: (date(2022, 2, 22), date(2024, 8, 28), date(2024, 3, 1)),
    )
    result = blind_training.create_session(Repo())
    try:
        assert result["symbol"] == "000001"
        assert result["rows"][0]["date"] == valid_rows[0]["date"]
        assert result["rows"][-1]["date"] == "2024-03-01"
        assert result["progress"] == {"revealed": 0, "total": blind_training.DECISION_BARS}
        state = blind_training.get_session(result["id"])
        assert state["context_start"] == 0
        assert state["start_index"] == 60
        assert state["decision_end"] == 120
        assert requested_ranges
        query_start, query_end = requested_ranges[-1]
        assert query_start == date(2022, 2, 22)
        assert query_end == date(2024, 8, 28)
    finally:
        with blind_training._lock:
            blind_training._sessions.pop(result["id"], None)


def test_create_session_skips_invalid_training_window(monkeypatch: pytest.MonkeyPatch) -> None:
    invalid_rows = [{**row, "open": 20.0, "high": 10.5} for row in _rows()]
    valid_rows = _rows()

    class Frame:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self._rows = rows

        def is_empty(self) -> bool:
            return not self._rows

        def sort(self, _column: str) -> Frame:
            return self

        def to_dicts(self) -> list[dict[str, object]]:
            return self._rows

    class Repo:
        def get_instruments(self) -> pl.DataFrame:
            return pl.DataFrame([
                {"symbol": "000001", "name": "无效样本"},
                {"symbol": "000002", "name": "有效样本"},
            ])

        def get_daily_asset(self, *_args: object, **_kwargs: object) -> Frame:
            return Frame(invalid_rows if _args[1] == "000001" else valid_rows)

    source = SimpleNamespace(randint=lambda *_args: 0)
    monkeypatch.setattr(blind_training.random, "SystemRandom", lambda: source)
    monkeypatch.setattr(
        blind_training,
        "_training_query_range",
        lambda *_args: (date(2022, 2, 22), date(2024, 8, 28), date(2024, 3, 1)),
    )

    result = blind_training.create_session(Repo())
    try:
        assert result["symbol"] == "000002"
    finally:
        with blind_training._lock:
            blind_training._sessions.pop(result["id"], None)


def test_delete_record_also_removes_its_ai_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def __init__(self, reports: list[dict[str, object]]) -> None:
            self.reports = reports

        def list_reports(self) -> list[dict[str, object]]:
            return list(self.reports)

        def delete_report(self, report_id: str) -> bool:
            before = len(self.reports)
            self.reports = [item for item in self.reports if item.get("id") != report_id]
            return len(self.reports) < before

    record_store = Store([{"id": "record-1", "training_id": "training-1"}])
    report_store = Store([
        {"id": "ai-1", "training_id": "training-1"},
        {"id": "ai-2", "training_id": "training-2"},
    ])
    monkeypatch.setattr(blind_training, "_record_store", record_store)
    monkeypatch.setattr(blind_training, "_report_store", report_store)

    assert blind_training.delete_record("training-1") is True
    assert record_store.reports == []
    assert report_store.reports == [{"id": "ai-2", "training_id": "training-2"}]


def test_delete_record_by_record_id_also_removes_its_ai_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def __init__(self, reports: list[dict[str, object]]) -> None:
            self.reports = reports

        def list_reports(self) -> list[dict[str, object]]:
            return list(self.reports)

        def delete_report(self, report_id: str) -> bool:
            before = len(self.reports)
            self.reports = [item for item in self.reports if item.get("id") != report_id]
            return len(self.reports) < before

    record_store = Store([{"id": "record-1", "training_id": "training-1"}])
    report_store = Store([{"id": "ai-1", "training_id": "training-1"}])
    monkeypatch.setattr(blind_training, "_record_store", record_store)
    monkeypatch.setattr(blind_training, "_report_store", report_store)

    assert blind_training.delete_record("record-1") is True
    assert record_store.reports == []
    assert report_store.reports == []


def test_record_list_omits_heavy_detail_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def list_reports(self) -> list[dict[str, object]]:
            return [{
                "id": "record-1",
                "training_id": "training-1",
                "symbol": "000001.SZ",
                "summary": {"trade_count": 2},
                "obsolete_metadata": {"value": 2},
                "bars": [{"date": "2024-01-01"}],
                "actions": [{"side": "buy"}],
                "equity_curve": [{"date": "2024-01-01", "value": 100000}],
            }]

    monkeypatch.setattr(blind_training, "_record_store", Store())

    records = blind_training.list_records()

    assert records[0]["training_id"] == "training-1"
    assert "bars" not in records[0]
    assert "actions" not in records[0]
    assert "equity_curve" not in records[0]
    assert "obsolete_metadata" not in records[0]

def test_record_detail_returns_only_current_contract_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        def __init__(self, reports: list[dict[str, object]]) -> None:
            self.reports = reports

        def list_reports(self) -> list[dict[str, object]]:
            return list(self.reports)

    record = {
        "id": "record-current",
        "training_id": "training-current",
        "bars": _rows(20),
        "analysis": {"analysis_mode": "blind_test"},
        "obsolete_metadata": {"value": 2},
    }
    monkeypatch.setattr(blind_training, "_record_store", Store([record]))
    monkeypatch.setattr(blind_training, "_report_store", Store([]))

    detail = blind_training.get_record("record-current")

    assert detail["analysis"]["analysis_mode"] == "blind_test"
    assert "obsolete_metadata" not in detail


def test_ai_payload_contains_only_blind_training_data() -> None:
    record = {
        "training_id": "training-1",
        "analysis": {"analysis_mode": "blind_test"},
        "bars": _rows(20),
        "actions": [{"date": _rows(20)[-1]["date"], "side": "buy"}],
        "summary": {"total_return_pct": 1.25},
        "equity_curve": [{"date": "2024-01-01", "value": 100000}],
        "private_backend_metadata": {"value": "not-for-ai"},
    }

    messages = blind_training.build_ai_messages(record)
    payload = json.loads(messages[1]["content"].split("\n", 1)[1])

    assert "盲测交易复盘教练" in messages[0]["content"]
    assert payload["analysis"]["analysis_mode"] == "blind_test"
    assert payload["equity_curve"] == record["equity_curve"]
    assert "private_backend_metadata" not in payload
