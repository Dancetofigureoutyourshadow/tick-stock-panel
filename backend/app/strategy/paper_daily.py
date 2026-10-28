"""日线跟单: 完整结果确认 -> 持久化计划 -> 指定时点提交。

不消费监控增量事件。价格撮合仍由 paper 域和盘后管道负责。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_now
from app.parquet import scan_daily_parquet
from app.services import trading_day
from app.services.fs_utils import atomic_write_text
from app.strategy import config as strategy_config
from app.strategy import paper, paper_auto, paper_exit, paper_intents

logger = logging.getLogger(__name__)
RISK_REASONS = {"stop_loss", "trailing_stop", "trailing_take_profit", "take_profit"}
# 盘后阶段只读全量同步后的权威日K/指标, 不再回退到盘中快照。
POST_CLOSE_PHASES = {"post_close", "post_close_sell"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def daily_strategy_ids(data_dir: Path, engine) -> set[str]:
    """无法加载的策略按日线隔离，不能降级回盘中交易。"""
    if engine is None:
        return set()
    ids = set()
    for account_id in paper.list_account_ids(data_dir):
        for rule in paper_auto.load_auto_rules(data_dir, account_id):
            if rule.get("match_kind") != "strategy":
                continue
            sid = rule["match_id"]
            try:
                strategy = engine.get(sid)
                if strategy.execution_backend == "minute_filter":
                    continue
                if "1d" not in strategy.meta.get("timeframes", ["1d"]):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.debug("日线跟单策略不可加载 %s: %s", sid, exc)
            ids.add(sid)
    return ids


def _next_trade_date(day: date, data_dir: Path | None, *, use_calendar: bool = True) -> date | None:
    from app.services.data_integrity import _trading_calendar

    calendar = (_trading_calendar() or set()) if use_calendar else set()
    later = [d for d in calendar if d > day]
    if data_dir is not None:
        later.extend(date.fromisoformat(p.name[5:]) for p in (data_dir / "kline_daily").glob("date=*")
                     if p.is_dir() and p.name[5:] > day.isoformat())
    return min(later) if later else None


def submission_error(confirmation: dict, order_type: str, now=None, *, data_dir: Path | None = None) -> str | None:
    now = (now or cn_now()).astimezone(CN_TZ)
    signal_day = date.fromisoformat(confirmation["signal_date"])
    if order_type == "close":
        if confirmation.get("settle_basis") == "post_close":
            # 收盘卖出改用盘后同步数据确认, 提交窗口顺延到收盘后同一天。
            if now.date() != signal_day or now.time() < time(15, 0):
                return "已错过盘后卖出提交窗口，当日不补单"
        elif now.date() != signal_day or not time(14, 57) <= now.time() < time(14, 58):
            return "已错过14:57提交窗口，当日不补单"
    elif now.date() > signal_day:
        target = confirmation.get("target_trade_date")
        target_day = date.fromisoformat(target) if target else _next_trade_date(signal_day, data_dir, use_calendar=False)
        if target_day is None:
            return "下一交易日无法确认，暂停提交"
        if now.date() > target_day or (now.date() == target_day and now.time() >= time(9, 30)):
            return "已错过下一交易日开盘，不补交订单"
    return None


class DailyCoordinator:
    def __init__(self, repo, engine, quotes=None):
        self.repo = repo
        self.engine = engine
        self.quotes = quotes
        self.data_dir = repo.store.data_dir

    def _directory(self, account_id: str) -> Path:
        return paper._root(self.data_dir, account_id) / "daily_batches"

    def _path(self, account_id: str, rule_id: str, day: str, mode: str) -> Path:
        key = _digest(rule_id)
        return self._directory(account_id) / f"{day}_{mode}_{key}.json"

    def _save(self, batch: dict) -> None:
        path = self._path(batch["account_id"], batch["rule_id"], batch["signal_date"], batch["mode"])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(batch, ensure_ascii=False, default=str, allow_nan=False))

    def batches(self, account_id: str, *, day: str = "*", mode: str = "*") -> list[dict]:
        rows = []
        for path in sorted(self._directory(account_id).glob(f"{day}_{mode}_*.json")):
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def latest(self, account_id: str, rule_id: str) -> dict | None:
        paths = list(self._directory(account_id).glob(f"*_{_digest(rule_id)}.json"))
        if not paths:
            return None
        latest_day = max(p.name[:10] for p in paths)
        rows = [json.loads(p.read_text(encoding="utf-8")) for p in paths if p.name.startswith(latest_day)]
        batch = max(rows, key=lambda b: (b["signal_date"], b.get("updated_at", "")))
        now = cn_now().astimezone(CN_TZ)
        if batch["mode"] == "close" and batch["status"] in {"confirmed", "prescanned"} and (
            now.date().isoformat() > batch["signal_date"] or (
                now.date().isoformat() == batch["signal_date"] and now.time() >= time(14, 58)
            )
        ):
            batch.update(status="skipped", reason="已错过14:57提交窗口，当日不补单")
        if batch["mode"] == "close_post" and batch["status"] == "confirmed" and (
            now.date().isoformat() > batch["signal_date"]
        ):
            batch.update(status="skipped", reason="已错过盘后卖出提交窗口，当日不补单")
        states = {a.get("status") for a in batch["actions"]}
        if states and states <= {"submitted", "filled"}:
            orders = {(o.get("confirmation") or {}).get("action_key"): o
                      for o in paper.load_orders(self.data_dir, account_id) if o.get("confirmation")}
            intents = {(i.get("confirmation") or {}).get("action_key"): i
                       for i in paper_intents.load(self.data_dir, account_id) if i.get("confirmation")}
            if all(orders.get(a["key"], {}).get("status") == "filled" for a in batch["actions"]):
                batch["status"] = "filled"
            elif any(intents.get(a["key"], {}).get("status") == "pending" for a in batch["actions"]):
                batch["status"] = "awaiting_confirmation"
            elif any(orders.get(a["key"], {}).get("status") in {"expired", "cancelled"}
                     or intents.get(a["key"], {}).get("status") in {"expired", "rejected"}
                     for a in batch["actions"]):
                batch.update(status="partial", reason="部分订单或待确认操作已过期或撤销")
        return batch

    def expire_intents(self) -> None:
        with paper.PAPER_LOCK:
            for account_id in paper.list_account_ids(self.data_dir):
                for intent in paper_intents.load(self.data_dir, account_id):
                    if intent.get("status") != "pending" or not intent.get("confirmation"):
                        continue
                    confirmation = intent["confirmation"]
                    if cn_now().date().isoformat() == confirmation["signal_date"] and cn_now().time() < time(14, 57):
                        continue
                    error = submission_error(confirmation, intent["order_type"], data_dir=self.data_dir)
                    if error and "无法确认" not in error:
                        paper_intents.resolve(self.data_dir, intent, "expired", error, account_id)

    def _rules(self):
        ids = daily_strategy_ids(self.data_dir, self.engine)
        return [(acc, rule) for acc in paper.list_account_ids(self.data_dir)
                for rule in paper_auto.load_auto_rules(self.data_dir, acc, enabled_only=True)
                if rule.get("match_kind") == "strategy" and rule["match_id"] in ids]

    def migrate(self) -> None:
        """一次性撤销可证明归属的旧日线自动操作；不动风险退出、手动操作。"""
        ids = daily_strategy_ids(self.data_dir, self.engine)
        known_ids = set()
        for sid in ids:
            try:
                self.engine.get(sid)
                known_ids.add(sid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("旧操作策略归属待核对 %s: %s", sid, exc)
        for account_id in paper.list_account_ids(self.data_dir):
            marker = self._directory(account_id) / "migration_v1.done"
            if marker.exists():
                continue
            rules = {r["id"]: r for r in paper_auto.load_auto_rules(self.data_dir, account_id)
                     if r.get("match_kind") == "strategy" and r["match_id"] in ids}
            if not rules:
                continue
            with paper.PAPER_LOCK:
                for item in paper.load_orders(self.data_dir, account_id) + paper_intents.load(self.data_dir, account_id):
                    if item.get("status") != "pending" or item.get("confirmation"):
                        continue
                    if item.get("exit_reason") in RISK_REASONS or item.get("source") == "manual":
                        continue
                    rid = item.get("auto_rule_id") or str(item.get("source", "")).removeprefix("auto:")
                    sid = rules.get(rid, {}).get("match_id", item.get("strategy_id"))
                    owned = (rid in rules and sid in known_ids) or (
                        item.get("strategy_id") in known_ids and item.get("exit_reason") in {"signal", "max_hold"}
                    )
                    if not owned:
                        if sid in ids:
                            item["daily_review_required"] = True
                            if item["id"].startswith("intent_"):
                                paper_intents.save(self.data_dir, item, account_id)
                            else:
                                paper.save_order(self.data_dir, item, account_id)
                        continue
                    reason = "切换日线确认流程，撤销旧盘中信号操作并重新扫描"
                    if item["id"].startswith("intent_"):
                        paper_intents.resolve(self.data_dir, item, "rejected", reason, account_id)
                    else:
                        item.update(status="cancelled", reason=reason)
                        paper.save_order(self.data_dir, item, account_id)
                marker.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(marker, cn_now().isoformat())

    def _snapshot(self, phase: str, day: date) -> pl.DataFrame:
        if phase not in POST_CLOSE_PHASES:
            if self.quotes is None:
                raise ValueError("实时行情服务不可用")
            self.quotes.refresh()
            frame, snapshot_day = self.quotes.get_enriched_today()
        else:
            # 版本化读取会拒绝世代不一致的同日缓存，不能只检查 latest 的日期。
            frame = self.repo.get_enriched_range(day, day)
            if frame is None or frame.is_empty():
                raise ValueError("盘后指标缓存尚未完成当前数据版本的重建")
            snapshot_day = day
            if "quote_ts" in frame.columns and frame["quote_ts"].is_not_null().any():
                raise ValueError("指标缓存仍是盘中快照，盘后重建尚未完成")
            raw = scan_daily_parquet(
                (self.data_dir / "kline_daily" / f"date={day}" / "*.parquet").as_posix(),
            ).collect()
            if "quote_ts" in raw.columns:
                stale = raw.filter(pl.col("quote_ts").is_not_null() & (pl.col("volume") > 0))
                if not stale.is_empty():
                    raise ValueError("当日日K仍含实时快照，须等待全量同步完成")
            if not set(frame["symbol"].to_list()) <= set(raw["symbol"].to_list()):
                raise ValueError("当日指标与权威日K覆盖不一致")
        if snapshot_day != day or frame.is_empty():
            raise ValueError("当日日K或指标数据未就绪")
        return frame

    def _validate_snapshot(self, frame: pl.DataFrame, phase: str, now: datetime) -> str | None:
        required = {"symbol", "date", "raw_close"}
        if not required <= set(frame.columns) or frame.is_empty():
            return "行情为空或缺少日期、原始价格字段"
        if frame["date"].null_count() or frame["date"].min() != now.date() or frame["date"].max() != now.date():
            return "行情日期不是信号交易日"
        if frame["raw_close"].null_count() or not frame.select((pl.col("raw_close").is_finite() & (pl.col("raw_close") > 0)).all()).item():
            return "原始价格缺失或无效"
        if phase not in POST_CLOSE_PHASES:
            if "quote_ts" not in frame.columns or frame["quote_ts"].null_count():
                return "缺少行情时间，不能确认信号"
            ages = frame.select((pl.lit(now.timestamp() * 1000) - pl.col("quote_ts")) / 1000).to_series()
            if ages.min() < 0 or ages.max() > 60:
                return "行情已过期或时间超前（要求不超过60秒）"
        return None

    def _run_strategy(self, sid: str, frame: pl.DataFrame, day: date):
        from app.services.screener import ScreenerService

        strategy = self.engine.get(sid)
        overrides = strategy_config.load_override(self.data_dir, sid)
        service = ScreenerService(self.repo)
        context = service.build_strategy_context(
            self.engine, day, [sid], current=frame,
            overrides_map={sid: overrides}, params_map={sid: overrides.get("params", {})},
            cache_key=f"paper_daily:{sid}",
        )
        if context.history is not None and context.history.is_empty():
            raise ValueError("策略所需历史数据未就绪")
        # 当前日必须来自本次快照，不能使用历史缓存中的14:50旧行。
        if context.history is not None and not context.history.is_empty():
            context.history = pl.concat([
                context.history.filter(pl.col("date") < day), frame,
            ], how="diagonal_relaxed").sort(["symbol", "date"])
        if strategy.execution_backend == "matrix_native":
            context.market = self.engine.prepare_realtime_matrix(
                context, [sid], overrides_map={sid: overrides},
                params_map={sid: overrides.get("params", {})},
            )
        result = self.engine.run(sid, context, overrides=overrides, params=overrides.get("params"))
        params = self.engine.resolve_params(strategy, overrides.get("params"), overrides)
        snapshot = {"strategy_id": sid, "strategy_name": strategy.meta.get("name", sid),
                    "strategy_version": str(strategy.meta.get("version", "")), "params": params,
                    "timeframe": "1d",
                    "basic_filter": {**(strategy.basic_filter or {}), **(overrides.get("basic_filter") or {})},
                    "scoring": overrides.get("scoring", strategy.meta.get("scoring", [])),
                    "scoring_directions": overrides.get("scoring_directions", {}),
                    "overlay_filter": overrides.get("overlay_filter", [])}
        for key in ("entry_signals", "exit_signals", "stop_loss", "take_profit", "trailing_stop",
                    "trailing_take_profit_activate", "trailing_take_profit_drawdown", "max_hold_days"):
            snapshot[key] = overrides.get(key, getattr(strategy, key, None))
        return result, snapshot

    def _config_key(self, rule: dict) -> str:
        strategy = self.engine.get(rule["match_id"])
        path = getattr(strategy, "file_path", None)
        return _digest([rule, strategy_config.load_override(self.data_dir, rule["match_id"]),
                        strategy.meta, path.stat().st_mtime_ns if path else None])

    @staticmethod
    def _fill(rule: dict, side: str) -> str:
        key = "entry_fill" if side == "buy" else "exit_fill"
        fallback = "open_t+1" if rule.get("order_type", "next_open") == "next_open" else "close_t"
        return rule.get(key, fallback)

    def _plan(self, account_id: str, rule: dict, result, frame: pl.DataFrame, snapshot: dict, mode: str) -> list[dict]:
        sid = rule["match_id"]
        day = result.as_of.isoformat()
        rows = {r["symbol"]: r for r in frame.to_dicts()}
        events = []
        if mode != "close_post":
            entry_symbols = {h["symbol"] for h in result.entry_signal_hits}
            for rank, row in enumerate(result.rows):
                symbol = row["symbol"]
                if symbol in entry_symbols and symbol in rows:
                    events.append({"source": "strategy", "strategy_id": sid, "type": "buy_signal",
                                   "symbol": symbol, "price": rows[symbol]["raw_close"],
                                   "score": row.get("score", getattr(result, "scores", {}).get(symbol, 0)),
                                   "_rank": rank, "strategy_snapshot": snapshot})
        if mode != "close":
            exits = {h["symbol"] for h in result.exit_signal_hits}
            exit_state = paper_exit._load_state(self.data_dir, account_id)
            trading_dates = sorted(p.name[5:] for p in (self.data_dir / "kline_daily").glob("date=*")
                                   if p.is_dir() and p.name[5:] <= day)
            for symbol, pos in paper.load_positions(self.data_dir, account_id).items():
                if symbol not in rows:
                    continue
                for lot in pos.get("lots", []):
                    if lot.get("strategy_id") != sid or not int(lot.get("qty", 0)) or str(lot.get("date", "")) >= day:
                        continue
                    max_hold = (lot.get("strategy_snapshot") or {}).get("max_hold_days")
                    held_days = sum(str(lot.get("date", "")) < d <= day for d in trading_dates)
                    reason = "signal" if symbol in exits else "max_hold" if max_hold and held_days >= int(max_hold) else None
                    fingerprint = "max_hold:" if reason == "max_hold" else "signal:" + ",".join(sorted(
                        signal for hit in result.exit_signal_hits if hit["symbol"] == symbol
                        for signal in hit.get("signals", [])
                    ))
                    if exit_state.get(str(lot.get("lot_id")), {}).get("ignored_fingerprint") == fingerprint:
                        continue
                    if reason:
                        events.append({"source": "strategy", "strategy_id": sid, "type": "sell_signal",
                                       "symbol": symbol, "price": rows[symbol]["raw_close"],
                                       "_lot_id": str(lot.get("lot_id")), "exit_reason": reason,
                                       "strategy_snapshot": lot.get("strategy_snapshot") or snapshot})
        target_fill = "open_t+1" if mode == "open" else "close_t"
        order_type = "next_open" if mode == "open" else "close"
        events = [e for e in events if self._fill(rule, paper_auto._strategy_event_side(e)) == target_fill]
        if not rule.get("_legacy_mode"):
            # 保留旧金额/权益比例规则的仓位契约，仅改变确认时点。
            actions = []
            for event in events:
                side = paper_auto._strategy_event_side(event)
                if paper_auto._in_cooldown(self.data_dir, rule, event["symbol"], int(rule.get("cooldown_days", 0)), account_id, side):
                    continue
                qty = paper_auto._sizing_qty(self.data_dir, rule, event["price"], account_id) if side == "buy" else next(
                    int(lot["qty"]) for lot in paper.load_positions(self.data_dir, account_id)[event["symbol"]]["lots"]
                    if str(lot.get("lot_id")) == event["_lot_id"]
                )
                if qty > 0:
                    actions.append({
                        "symbol": event["symbol"], "side": side, "qty": qty,
                        "order_type": order_type,
                        "ref_price": event["price"], "source": f"auto:{rule['id']}",
                        "auto_enabled": True, "strategy_id": sid, "strategy_scope_id": sid,
                        "strategy_snapshot": event["strategy_snapshot"], "buy_score": event.get("score"),
                        "target_lot_id": event.get("_lot_id"), "exit_reason": event.get("exit_reason"),
                        "max_positions": rule.get("max_positions", 0), "max_total_position": 1.0,
                        "auto_rule_id": rule["id"], "automation_source": "auto_follow",
                    })
            return actions
        normalized = paper_auto.normalize_rule({
            **rule, "position_sizing": rule.get("position_sizing", "equal"),
            "max_positions": rule.get("max_positions") or paper.DEFAULT_MAX_POSITIONS,
            "entry_fill": self._fill(rule, "buy"), "exit_fill": self._fill(rule, "sell"),
        })
        return paper_auto._legacy_events(self.data_dir, events, [normalized], account_id, dry_run=True)

    def scan(self, phase: str, *, skip_reason: str | None = None) -> list[dict]:
        now = cn_now().astimezone(CN_TZ)
        if trading_day.is_trading_day(now) is False:
            return []
        if phase == "confirm" and not time(14, 55) <= now.time() < time(14, 57):
            return []
        if phase == "prescan" and not time(14, 50) <= now.time() < time(14, 55):
            return []
        if phase in POST_CLOSE_PHASES and now.time() < time(15, 30):
            return []
        self.migrate()
        self.expire_intents()
        mode = {"post_close_sell": "close_post", "post_close": "open"}.get(phase, "close")
        if mode == "close":
            sides, fill = ("buy",), "close_t"
        elif mode == "close_post":
            sides, fill = ("sell",), "close_t"
        else:
            sides, fill = ("buy", "sell"), "open_t+1"
        rules = [(acc, r) for acc, r in self._rules() if any(self._fill(r, side) == fill for side in sides)]
        if not rules:
            return []
        target_day = _next_trade_date(now.date(), self.data_dir) if mode == "open" else None
        frame = pl.DataFrame()
        error = skip_reason
        try:
            if not error:
                frame = self._snapshot(phase, now.date())
                error = self._validate_snapshot(frame, phase, cn_now().astimezone(CN_TZ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("日线跟单快照不可用: %s", exc)
            error = str(exc)
        results = {}
        batches = []
        for account_id, rule in rules:
            if phase != "prescan":
                old = self._path(account_id, rule["id"], now.date().isoformat(), mode)
                if old.exists() and json.loads(old.read_text(encoding="utf-8"))["status"] in {"submitted", "filled", "partial"}:
                    continue
            batch = {
                "id": _digest([account_id, rule["id"], now.date().isoformat(), mode]),
                "account_id": account_id, "rule_id": rule["id"], "strategy_id": rule["match_id"],
                "signal_date": now.date().isoformat(), "mode": mode,
                "settle_basis": "post_close" if mode == "close_post" else None,
                "rule_snapshot": rule,
                "target_trade_date": target_day.isoformat() if target_day else None,
                "status": "skipped" if error else "prescanned" if phase == "prescan" else "confirmed",
                "reason": error, "quote_time": None, "confirmed_at": None, "submitted_at": None,
                "updated_at": cn_now().isoformat(), "actions": [], "candidates": [],
            }
            try:
                if not error:
                    sid = rule["match_id"]
                    if sid not in results:
                        try:
                            results[sid] = self._run_strategy(sid, frame, now.date())
                        except Exception as exc:  # noqa: BLE001
                            results[sid] = exc
                    if isinstance(results[sid], Exception):
                        raise ValueError(str(results[sid]))
                    result, snapshot = results[sid]
                    batch["strategy_snapshot"] = snapshot
                    if result.as_of != now.date():
                        raise ValueError("策略结果日期与信号日不符")
                    batch["config_key"] = self._config_key(rule)
                    batch["candidates"] = result.rows
                    batch["quote_time"] = (
                        datetime.fromtimestamp(frame["quote_ts"].min() / 1000, CN_TZ).isoformat()
                        if mode == "close" else f"{now.date()}T15:00:00+08:00"
                    )
                    if phase != "prescan":
                        if mode == "close" and cn_now().time() >= time(14, 57):
                            raise ValueError("最终扫描未在14:57前完成")
                        with paper.PAPER_LOCK:
                            batch["actions"] = self._plan(account_id, rule, result, frame, snapshot, mode)
                        if mode == "close" and cn_now().time() >= time(14, 57):
                            raise ValueError("最终扫描未在14:57前完成")
                        batch["confirmed_at"] = cn_now().isoformat()
                        for action in batch["actions"]:
                            action["status"] = "planned"
                            action["key"] = _digest([account_id, rule["id"], batch["signal_date"], action["side"], action["symbol"], action.get("target_lot_id")])
            except Exception as exc:  # noqa: BLE001
                logger.warning("日线跟单确认失败 %s: %s", rule["id"], exc)
                batch.update(status="skipped", reason=str(exc), actions=[])
            if phase == "prescan" and cn_now().time() >= time(14, 55):
                continue  # 预热任务迟到不能覆盖最终确认批次。
            self._save(batch)
            batches.append(batch)
        return batches

    def submit(self, *, mode: str = "close") -> list[dict]:
        now = cn_now().astimezone(CN_TZ)
        if trading_day.is_trading_day(now) is False:
            return []
        if mode == "close" and now.time() < time(14, 57):
            return []
        if mode == "close_post" and now.time() < time(15, 0):
            return []
        order_type = "next_open" if mode == "open" else "close"
        created = []
        with paper.PAPER_LOCK:
            for account_id in paper.list_account_ids(self.data_dir):
                rules = {r["id"]: r for r in paper_auto.load_auto_rules(self.data_dir, account_id, enabled_only=True)}
                for batch in self.batches(account_id, mode=mode,
                                          day=now.date().isoformat() if mode in {"close", "close_post"} else "*"):
                    if batch["mode"] != mode or batch["status"] not in {"confirmed", "partial"}:
                        continue
                    if batch["status"] == "partial" and not any(a["status"] == "planned" for a in batch["actions"]):
                        continue
                    error = submission_error(batch, order_type, now, data_dir=self.data_dir)
                    rule = rules.get(batch["rule_id"])
                    if not rule:
                        error = "规则已停用或删除"
                    else:
                        try:
                            if self._config_key(rule) != batch["config_key"]:
                                error = "策略或规则配置已改变，原确认结果失效"
                        except Exception as exc:  # noqa: BLE001
                            error = f"策略配置不可用: {exc}"
                    if error:
                        batch.update(status="skipped", reason=error, updated_at=now.isoformat())
                        self._save(batch)
                        continue
                    if not batch["actions"]:
                        batch.update(status="skipped", reason="本次没有可提交的交易计划", updated_at=now.isoformat())
                        self._save(batch)
                        continue
                    for action in batch["actions"]:
                        if action["status"] != "planned":
                            continue
                        action_now = cn_now().astimezone(CN_TZ)
                        timing_error = submission_error(batch, order_type, action_now, data_dir=self.data_dir)
                        if timing_error:
                            action.update(status="skipped", reason=timing_error)
                            continue
                        confirmation = {k: batch.get(k) for k in ("signal_date", "quote_time", "confirmed_at", "target_trade_date")}
                        if batch.get("settle_basis"):
                            confirmation["settle_basis"] = batch["settle_basis"]
                        confirmation.update(batch_id=batch["id"], action_key=action["key"], submitted_at=action_now.isoformat())
                        existing = next((o for o in paper.load_orders(self.data_dir, account_id) + paper_intents.load(self.data_dir, account_id)
                                         if (o.get("confirmation") or {}).get("action_key") == action["key"]), None)
                        if existing:
                            action.update(status="submitted", order_id=existing["id"])
                            continue
                        error = self._action_error(account_id, action)
                        if error:
                            action.update(status="skipped", reason=error)
                        else:
                            kwargs = {k: v for k, v in action.items() if k not in {"auto_enabled", "status", "key"}}
                            if action["auto_enabled"]:
                                order, error = paper.create_order(self.data_dir, account_id=account_id, confirmation=confirmation, **kwargs)
                            else:
                                kwargs["lot_id"] = kwargs.pop("target_lot_id", None)
                                order = paper_intents.create(self.data_dir, account_id=account_id, confirmation=confirmation, **kwargs)
                                order = {**order, "kind": "intent"}
                            if error:
                                action.update(status="skipped", reason=error)
                            else:
                                action.update(status="submitted", order_id=order["id"])
                                created.append((account_id, order))
                        batch["status"] = "partial"
                        self._save(batch)
                    batch.update(status="submitted" if all(a["status"] == "submitted" for a in batch["actions"]) else "partial",
                                 submitted_at=now.isoformat(), updated_at=now.isoformat())
                    self._save(batch)
        return created

    def _action_error(self, account_id: str, action: dict) -> str | None:
        account = paper.get_account(self.data_dir, account_id)
        if not account or account.get("status") != "active":
            return "账户不存在或已冻结"
        orders = paper.load_orders(self.data_dir, account_id)
        pending = [o for o in orders + paper_intents.load(self.data_dir, account_id) if o.get("status") == "pending"]
        if any(o.get("daily_review_required") for o in pending):
            return "有归属不明的旧操作待处理，暂停新增交易"
        if action["side"] == "sell":
            if paper_exit._pending_action(self.data_dir, action["target_lot_id"], account_id):
                return "该持仓批次已有退出操作"
            lots = paper.load_positions(self.data_dir, account_id).get(action["symbol"], {}).get("lots", [])
            lot = next((lot for lot in lots if str(lot.get("lot_id")) == action["target_lot_id"]), None)
            if not lot or int(lot.get("qty", 0)) < action["qty"]:
                return "目标持仓已变化"
        else:
            used = paper_auto._position_slots_used(self.data_dir, account_id)
            used.update(o["symbol"] for o in pending if o["side"] == "buy")
            if action["symbol"] in used:
                return "标的已有持仓或待买操作"
            if len(used) >= (action["max_positions"] or paper.MAX_POSITION_SYMBOLS):
                return "已达最大持仓数"
            reserved = sum(int(o["qty"]) * float(o.get("ref_price") or 0) for o in pending if o["side"] == "buy")
            price = paper.apply_slippage(action["ref_price"], "buy", account["slippage_bps"])
            needed = action["qty"] * price + paper.buy_fee(action["qty"], price, account["commission_pct"])
            if needed + reserved > account["cash"]:
                return "资金已被其他订单占用"
        return None


def publish_created(quotes, data_dir: Path, created: list[tuple[str, dict]]) -> None:
    from app.services import alert_store

    events = [event for account_id, order in created
              for event in paper_auto.auto_order_events([order], account_id)]
    if events:
        alert_store.append_many(data_dir, events)
        if quotes:
            quotes._broadcast_alerts(events)
            quotes._maybe_send_system_notifications(events)
            quotes._maybe_send_paper_webhook(events)
