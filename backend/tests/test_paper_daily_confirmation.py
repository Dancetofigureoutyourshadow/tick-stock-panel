"""Daily signals are confirmed from full results, independently of monitor events."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto, paper_daily

DAY = date(2026, 10, 19)


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = [datetime(2026, 10, 19, 14, 55, tzinfo=CN_TZ)]
    monkeypatch.setattr(paper_daily, "cn_now", lambda: clock[0])
    monkeypatch.setattr(paper, "cn_now", lambda: clock[0])
    monkeypatch.setattr(paper, "cn_today", lambda: clock[0].date())
    monkeypatch.setattr(paper_auto, "cn_now", lambda: clock[0])
    monkeypatch.setattr(paper_daily.trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(paper_daily, "_next_trade_date", lambda *args, **kwargs: date(2026, 10, 20))
    paper.create_account(tmp_path, 100_000)
    rule = paper_auto.create_auto_rule(tmp_path, {
        "name": "daily", "match_kind": "strategy", "match_id": "daily",
        "side": "auto", "entry_fill": "close_t", "exit_fill": "close_t",
        "position_sizing": "equal", "max_positions": 1, "max_total_position": 90,
        "auto_follow_enabled": True, "auto_exit_enabled": True,
    })
    strategy = SimpleNamespace(meta={"timeframes": ["1d"]}, execution_backend="polars_expr")
    engine = SimpleNamespace(get=lambda sid: strategy)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    coordinator = paper_daily.DailyCoordinator(repo, engine)
    return tmp_path, clock, rule, coordinator, monkeypatch


def confirm(env, symbols=("600000.SH",), *, age=0):
    _path, clock, _rule, coordinator, monkeypatch = env
    frame = pl.DataFrame({
        "symbol": list(symbols), "date": [DAY] * len(symbols),
        "raw_close": [10.0] * len(symbols),
        "quote_ts": [int(clock[0].timestamp() * 1000) - age * 1000] * len(symbols),
    })
    result = SimpleNamespace(
        as_of=DAY, rows=[{"symbol": s, "score": 90 - i} for i, s in enumerate(symbols)],
        entry_signal_hits=[{"symbol": s, "signals": []} for s in symbols],
        exit_signal_hits=[],
    )
    monkeypatch.setattr(coordinator, "_snapshot", lambda phase, day: frame)
    monkeypatch.setattr(coordinator, "_run_strategy", lambda sid, frame, day: (result, {}))
    return coordinator.scan("confirm")


def test_confirm_drafts_then_submit_once(env):
    path, clock, rule, coordinator, _monkeypatch = env
    confirm(env, ("600001.SH", "600000.SH"))
    assert paper.load_orders(path) == []
    assert coordinator.latest("default", rule["id"])["status"] == "confirmed"
    coordinator.submit()
    assert paper.load_orders(path) == []
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    coordinator.submit()
    orders = paper.load_orders(path)
    assert len(orders) == 1 and orders[0]["symbol"] == "600001.SH"
    assert orders[0]["order_type"] == "close"
    assert orders[0]["confirmation"]["signal_date"] == DAY.isoformat()


@pytest.mark.parametrize("age", [61, 86_400, -5])
def test_stale_or_future_quotes_do_not_confirm(env, age):
    path, clock, rule, coordinator, _monkeypatch = env
    confirm(env, age=age)
    assert coordinator.latest("default", rule["id"])["status"] == "skipped"
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert paper.load_orders(path) == []


def test_no_catchup_after_submit_window(env):
    path, clock, rule, coordinator, _monkeypatch = env
    confirm(env)
    clock[0] = clock[0].replace(minute=58)
    coordinator.submit()
    assert paper.load_orders(path) == []
    assert coordinator.latest("default", rule["id"])["status"] == "skipped"


def test_empty_final_results_do_not_reuse_prescan(env):
    path, clock, _rule, coordinator, _monkeypatch = env
    confirm(env)
    confirm(env, ())
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert paper.load_orders(path) == []
    assert coordinator.latest("default", env[2]["id"])["status"] == "skipped"


def test_intraday_events_exclude_daily_only(env):
    path, _clock, _rule, _coordinator, _monkeypatch = env
    events = [{"source": "strategy", "strategy_id": "daily", "type": "buy_signal",
               "symbol": "600000.SH", "price": 10}]
    assert paper_auto.on_rule_events(path, events, excluded_strategy_ids={"daily"}) == []
    assert paper.load_orders(path) == []


def test_migration_preserves_manual_and_risk_orders(env):
    path, _clock, rule, coordinator, _monkeypatch = env
    manual, _ = paper.create_order(path, "600000.SH", "buy", qty=100, ref_price=10)
    old, _ = paper.create_order(path, "600001.SH", "buy", qty=100, ref_price=10,
                                source=f"auto:{rule['id']}")
    coordinator.migrate()
    coordinator.migrate()
    assert paper.get_order(path, manual["id"])["status"] == "pending"
    assert paper.get_order(path, old["id"])["status"] == "cancelled"


def test_submit_rechecks_freeze(env):
    path, clock, _rule, coordinator, _monkeypatch = env
    confirm(env)
    account = paper.get_account(path)
    account["status"] = "frozen"
    paper.save_account(path, account)
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert paper.load_orders(path) == []


def test_submit_recovers_order_written_before_batch_update(env, monkeypatch):
    path, clock, rule, coordinator, _ = env
    confirm(env)
    clock[0] = clock[0].replace(minute=57)
    save = coordinator._save
    failed = [False]

    def fail_once(batch):
        if batch["status"] == "partial" and not failed[0]:
            failed[0] = True
            raise OSError("simulated crash after order persistence")
        save(batch)

    monkeypatch.setattr(coordinator, "_save", fail_once)
    with pytest.raises(OSError):
        coordinator.submit()
    recovered = paper_daily.DailyCoordinator(coordinator.repo, coordinator.engine)
    recovered.submit()
    assert len(paper.load_orders(path)) == 1
    assert recovered.latest("default", rule["id"])["status"] == "submitted"


def test_configuration_change_invalidates_confirmation(env):
    path, clock, rule, coordinator, _ = env
    confirm(env)
    paper_auto.update_auto_rule(path, rule["id"], {"max_positions": 2})
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert paper.load_orders(path) == []
    assert "配置已改变" in coordinator.latest("default", rule["id"])["reason"]


def test_post_close_uses_full_results_and_never_same_day_open(env):
    path, clock, rule, coordinator, monkeypatch = env
    paper_auto.update_auto_rule(path, rule["id"], {"entry_fill": "open_t+1"})
    clock[0] = clock[0].replace(hour=15, minute=40)
    frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [DAY], "raw_close": [10.0]})
    result = SimpleNamespace(as_of=DAY, rows=[{"symbol": "600000.SH", "score": 90}],
                             entry_signal_hits=[{"symbol": "600000.SH"}], exit_signal_hits=[])
    monkeypatch.setattr(coordinator, "_snapshot", lambda phase, day: frame)
    monkeypatch.setattr(coordinator, "_run_strategy", lambda *args: (result, {}))
    coordinator.scan("post_close")
    coordinator.submit(mode="open")
    orders = paper.load_orders(path)
    assert len(orders) == 1 and orders[0]["order_type"] == "next_open"
    assert paper._placed_after_price_time(orders[0], str(DAY))
    assert orders[0]["confirmation"]["confirmed_at"].startswith(f"{DAY}T15:40")


def test_failed_pipeline_does_not_confirm_next_open(env):
    path, clock, rule, coordinator, monkeypatch = env
    paper_auto.update_auto_rule(path, rule["id"], {"entry_fill": "open_t+1"})
    clock[0] = clock[0].replace(hour=15, minute=40)
    monkeypatch.setattr(coordinator, "_snapshot", lambda *args: pytest.fail("must not read stale cache"))
    coordinator.scan("post_close", skip_reason="全量同步未完成")
    coordinator.submit(mode="open")
    assert paper.load_orders(path) == []
    assert coordinator.latest("default", rule["id"])["reason"] == "全量同步未完成"


def _seed_lot(path, *, snapshot=None):
    paper._append_fill(path, {
        "seq": 1, "ts": "", "date": "2026-10-16", "order_id": "lot_buy",
        "symbol": "600000.SH", "asset_type": "stock", "side": "buy", "qty": 100,
        "price": 10, "fee": 5, "kind": "fill", "strategy_id": "daily",
        "strategy_snapshot": snapshot or {"exit_signals": ["sell_now"], "stop_loss": 0.1},
    })
    paper._materialize(path)


def test_daily_signals_are_deferred_but_intraday_stop_remains(env):
    from app.strategy import paper_exit

    path, _, _, _, _ = env
    _seed_lot(path)
    row = {"symbol": "600000.SH", "raw_close": 10, "raw_high": 10, "raw_low": 10,
           "sell_now": True}
    assert paper_exit.evaluate(path, [row], trade_date=str(DAY), daily_strategy_ids={"daily"}) == []
    row.update(raw_close=8.9, raw_low=8.9)
    events = paper_exit.evaluate(path, [row], trade_date=str(DAY), daily_strategy_ids={"daily"})
    assert len(events) == 1 and events[0]["exit_reason"] == "stop_loss"
    assert paper.load_orders(path)[0]["order_type"] == "market"


def test_pending_risk_exit_prevents_duplicate_daily_sell(env):
    from app.strategy import paper_exit

    path, clock, _, coordinator, monkeypatch = env
    _seed_lot(path)
    frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [DAY], "raw_close": [10.0],
                          "quote_ts": [int(clock[0].timestamp() * 1000)]})
    result = SimpleNamespace(as_of=DAY, rows=[], entry_signal_hits=[],
                             exit_signal_hits=[{"symbol": "600000.SH"}])
    monkeypatch.setattr(coordinator, "_snapshot", lambda *args: frame)
    monkeypatch.setattr(coordinator, "_run_strategy", lambda *args: (result, {}))
    coordinator.scan("confirm")
    paper_exit.evaluate(path, [{"symbol": "600000.SH", "raw_close": 8.9, "raw_low": 8.9}],
                        trade_date=str(DAY), daily_strategy_ids={"daily"})
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert len(paper.load_orders(path)) == 1
    assert paper.load_orders(path)[0]["exit_reason"] == "stop_loss"


def test_shared_strategy_runs_once_for_multiple_accounts(env):
    from unittest.mock import Mock

    path, clock, rule, coordinator, monkeypatch = env
    paper.create_account(path, 100_000, account_id="second")
    paper_auto.create_auto_rule(path, {k: v for k, v in rule.items() if k != "id"}, account_id="second")
    confirm(env)
    original = coordinator._run_strategy
    run = Mock(side_effect=original)
    monkeypatch.setattr(coordinator, "_run_strategy", run)
    coordinator.scan("confirm")
    assert run.call_count == 1
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert len(paper.load_orders(path, "second")) == len(paper.load_orders(path)) == 1


def test_authoritative_close_required_and_metadata_reaches_fill(env):
    path, clock, rule, coordinator, _ = env
    confirm(env)
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    raw = path / "kline_daily" / f"date={DAY}" / "part.parquet"
    raw.parent.mkdir(parents=True)
    values = {"symbol": ["600000.SH"], "date": [DAY], "open": [10.0], "close": [10.0]}
    pl.DataFrame({**values, "quote_ts": [int(clock[0].timestamp() * 1000)]}).write_parquet(raw)
    assert paper.settle_day(path, str(DAY))["filled"] == 0
    pl.DataFrame(values).write_parquet(raw)
    assert paper.settle_day(path, str(DAY))["filled"] == 1
    fill = paper.load_fills(path)[0]
    assert fill["confirmation"]["signal_date"] == str(DAY)
    assert coordinator.latest("default", rule["id"])["status"] == "filled"


def test_holiday_never_scans_or_submits(env):
    path, clock, _, coordinator, monkeypatch = env
    confirm(env)
    monkeypatch.setattr(paper_daily.trading_day, "is_trading_day", lambda now=None: False)
    monkeypatch.setattr(coordinator, "_snapshot", lambda *args: pytest.fail("holiday must not fetch"))
    assert coordinator.scan("confirm") == []
    clock[0] = clock[0].replace(minute=57)
    assert coordinator.submit() == []
    assert paper.load_orders(path) == []


def test_pending_intent_carries_confirmation_and_expires_after_window(env):
    from app.strategy import paper_intents

    path, clock, rule, coordinator, _ = env
    paper_auto.update_auto_rule(path, rule["id"], {"auto_follow_enabled": False})
    confirm(env)
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    intent = paper_intents.load(path)[0]
    clock[0] = clock[0].replace(minute=58)
    _, order, error = paper_intents.confirm(path, intent["id"], "default", qty=intent["qty"],
                                           order_type="close", ref_price=10, asset_type="stock")
    assert order is None and "提交窗口" in error


def test_max_hold_and_daily_sell_use_confirmed_batch(env):
    path, clock, rule, coordinator, _monkeypatch = env
    _seed_lot(path, snapshot={"max_hold_days": 1})
    partition = path / "kline_daily" / f"date={DAY}"
    partition.mkdir(parents=True)
    confirm(env)
    batch = coordinator.latest("default", rule["id"])
    sells = [a for a in batch["actions"] if a["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["exit_reason"] == "max_hold"
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    orders = paper.load_orders(path)
    assert len(orders) == 1 and orders[0]["target_lot_id"] == "lot_buy"
    assert orders[0]["order_type"] == "close"


def test_changed_funds_skip_without_overspending(env):
    path, clock, rule, coordinator, _ = env
    paper_auto.update_auto_rule(path, rule["id"], {"max_positions": 2})
    confirm(env)
    paper.create_order(path, "600009.SH", "buy", qty=9000, ref_price=10)
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert len(paper.load_orders(path)) == 1
    assert "资金" in coordinator.latest("default", rule["id"])["actions"][0]["reason"]


def test_latest_batch_api_has_optional_status_fields(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.paper import router

    _path, _, _rule, coordinator, _ = env
    app = FastAPI()
    app.include_router(router)
    app.state.repo = coordinator.repo
    client = TestClient(app)
    assert client.get("/api/paper/auto_rules").json()["rules"][0]["latest_batch"] is None
    confirm(env)
    batch = client.get("/api/paper/auto_rules").json()["rules"][0]["latest_batch"]
    assert batch["status"] == "confirmed" and batch["signal_date"] == str(DAY)
    assert batch["submitted_at"] is None


def test_scheduler_registers_three_beijing_phases(monkeypatch):
    from unittest.mock import Mock

    from app.jobs import daily_pipeline
    from app.services import preferences

    scheduler = Mock()
    monkeypatch.setattr(daily_pipeline, "AsyncIOScheduler", lambda **kwargs: scheduler)
    monkeypatch.setattr(preferences, "get_review_schedule", lambda: {"enabled": False})
    daily_pipeline.start_scheduler(Mock(), Mock())
    phases = {call.kwargs["id"]: call.kwargs for call in scheduler.add_job.call_args_list
              if call.kwargs.get("id", "").startswith("paper_daily_")}
    assert set(phases) == {"paper_daily_prescan", "paper_daily_confirm", "paper_daily_submit"}
    for phase, minute in (("prescan", "50"), ("confirm", "55"), ("submit", "57")):
        trigger = phases[f"paper_daily_{phase}"]["trigger"]
        assert str(trigger.timezone) == "Asia/Shanghai"
        assert str(trigger.fields[6]) == minute


def test_real_engine_results_are_used_without_monitor_event_state(env, monkeypatch):
    from app.services.screener import ScreenerService
    from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine

    path, clock, _, coordinator, _ = env
    strategy = StrategyDef(
        meta={"id": "daily", "timeframes": ["1d"], "order_by": "raw_close", "descending": False},
        basic_filter={}, entry_signals=[], exit_signals=[], stop_loss=None, trailing_stop=None,
        trailing_take_profit_activate=None, trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=lambda frame, params: pl.col("raw_close") < 20, filter_history_fn=None,
        lookback_days=1, source="custom",
    )
    engine = StrategyEngine(strategy_dirs=[])
    monkeypatch.setattr(engine, "get", lambda sid: strategy)
    monkeypatch.setattr(ScreenerService, "build_strategy_context", lambda self, eng, day, ids, **kwargs:
                        StrategyDataContext(asset_type="stock", timeframe="1d", as_of=day, current=kwargs["current"]))
    coordinator.engine = engine
    frame = pl.DataFrame({"symbol": ["600000.SH", "600001.SH"], "date": [DAY, DAY],
                          "raw_close": [12.0, 10.0], "quote_ts": [int(clock[0].timestamp() * 1000)] * 2})
    monkeypatch.setattr(coordinator, "_snapshot", lambda *args: frame)
    coordinator.scan("confirm")
    clock[0] = clock[0].replace(minute=57)
    coordinator.submit()
    assert paper.load_orders(path)[0]["symbol"] == "600001.SH"


def test_unknown_old_ownership_is_quarantined_not_filled(env):
    path, _, _, coordinator, _ = env
    unknown, _ = paper.create_order(path, "600000.SH", "buy", qty=100, ref_price=10,
                                    strategy_id="daily", source="unknown-automation")
    coordinator.migrate()
    assert paper.get_order(path, unknown["id"])["daily_review_required"] is True
    assert paper.evaluate_intraday(path, {"600000.SH": 10}) == []


def test_submission_cannot_run_past_1458_during_validation(env, monkeypatch):
    path, clock, _, coordinator, _ = env
    confirm(env)
    clock[0] = clock[0].replace(minute=57)

    def slow_check(*args):
        clock[0] = clock[0].replace(minute=58)

    monkeypatch.setattr(coordinator, "_action_error", slow_check)
    coordinator.submit()
    assert paper.load_orders(path) == []


def test_next_open_deadline_respects_actual_trade_date():
    confirmation = {"signal_date": "2026-09-30", "target_trade_date": "2026-10-08"}
    holiday = datetime(2026, 10, 7, 16, 0, tzinfo=CN_TZ)
    before = datetime(2026, 10, 8, 9, 29, tzinfo=CN_TZ)
    after = before.replace(minute=30)
    assert paper_daily.submission_error(confirmation, "next_open", holiday) is None
    assert paper_daily.submission_error(confirmation, "next_open", before) is None
    assert "已错过" in paper_daily.submission_error(confirmation, "next_open", after)


def test_post_close_invalidation_is_scoped_to_daily_matrix(env, monkeypatch):
    from unittest.mock import Mock

    from app.jobs import daily_pipeline

    _, _, _, coordinator, _ = env
    engine = coordinator.engine
    engine.invalidate_realtime_matrix = Mock()
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(strategy_engine=engine))
    monkeypatch.setattr(paper_daily.DailyCoordinator, "scan", lambda *args, **kwargs: [])
    monkeypatch.setattr(paper_daily.DailyCoordinator, "submit", lambda *args, **kwargs: [])
    daily_pipeline._run_paper_daily_phase("post_close", coordinator.repo)
    engine.invalidate_realtime_matrix.assert_called_once_with("paper_daily:daily")


def test_post_close_rejects_same_day_stale_cache_generation(env):
    _, _, _, coordinator, _ = env
    coordinator.repo.get_enriched_range = lambda *args: None
    coordinator.repo.get_enriched_latest = lambda: pytest.fail("must use versioned reader")
    with pytest.raises(ValueError, match="当前数据版本"):
        coordinator._snapshot("post_close", DAY)


@pytest.mark.parametrize("already_refreshed,expected", [(True, 0), (False, 1)])
def test_manual_pipeline_refreshes_cache_once(tmp_path, monkeypatch, already_refreshed, expected):
    import asyncio
    from unittest.mock import Mock

    from app.api import pipeline as pipeline_api
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_api, "job_store", store)
    monkeypatch.setattr(pipeline_api, "try_acquire_run_slot", lambda job: True)
    monkeypatch.setattr(pipeline_api, "release_run_slot", lambda: None)
    monkeypatch.setattr(pipeline_api, "run_with_capacity", lambda job, run: run())
    monkeypatch.setattr(pipeline_api, "invalidate_storage_cache", lambda: None)
    repo = SimpleNamespace(refresh_cache=Mock())

    async def execute():
        result = await pipeline_api._submit_job(repo, None, lambda progress: lambda: {
            "cache_refreshed": already_refreshed,
        })
        for _ in range(100):
            job = store.get(result["job_id"])
            if job["status"] in {"succeeded", "failed"}:
                assert job["status"] == "succeeded", job
                return
            await asyncio.sleep(0.01)
        pytest.fail("pipeline did not finish")

    asyncio.run(execute())
    assert repo.refresh_cache.call_count == expected
