"""模拟盘历史回填: 假设账户从 start 日起按自动规则运行, 补足到 end 日。

口径说明
--------
信号/选股/退出决策全部来自回测引擎 (app/backtest), 保证与策略定义一致且无未来函数;
成交记账改为模拟盘口径: 不复权原始价 + 滑点 + 佣金(最低5元) + 卖出印花税 + 百股整手。

回测矩阵用前复权价, 模拟盘用原始价 (paper.py 契约), 两者在含分红的窗口内不等价,
因此这里不直接搬运回测的成交价与股数, 而是:
  - 沿用回测给出的「每笔买入预算金额 entry_value」(金额口径与复权无关);
  - 用当日不复权 open/close 重算成交价、股数与费用。
回测结束时的强制平仓 (exit_reason == "end") 不视为真实卖出, 保留为当前持仓。

用法
----
    python scripts/paper_backfill.py --dry-run            # 只打印, 不写盘
    python scripts/paper_backfill.py --apply              # 写入账户目录
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402
import app.enriched_generation as eg  # noqa: E402
from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService  # noqa: E402
from app.strategy import config as strategy_config  # noqa: E402
from app.strategy import paper  # noqa: E402
from app.strategy import paper_auto  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

BUY_COST_EXTRA = 0.0  # 佣金与滑点已单独建模, 预算换算不再叠加


def _patch_windows_process_probe() -> None:
    """Windows 上 os.kill(pid, 0) 对无效 pid 抛 SystemError (非 OSError),
    enriched_generation._process_is_alive 未捕获, 会让回填脚本整体失败。
    这里只补上该异常类型, 不改项目源码。
    """
    original = eg._process_is_alive

    def safe(pid):
        try:
            return original(pid)
        except SystemError:
            return False

    eg._process_is_alive = safe


def _build_service():
    store = DataStore()
    repo = KlineRepository(store)
    strategy_dirs = [
        ROOT / "backend" / "app" / "strategy" / "research",
        store.data_dir / "strategies" / "custom",
        store.data_dir / "strategies" / "ai",
        store.data_dir / "strategies" / "composite",
    ]
    strategy_engine = StrategyEngine(
        strategy_dirs=strategy_dirs,
        override_loader=lambda sid: strategy_config.load_override(store.data_dir, sid),
    )
    return store, StrategyBacktestService(BacktestEngine(repo), strategy_engine)


def load_raw_bars(data_dir: Path, days: list[str], symbols: set[str] | None = None):
    """加载不复权原始日K -> {(day, symbol): {open, close, high, low}}。

    kline_daily 与 enriched 的 raw_* 等价 (已核对: 当日 close == raw_close 全量一致)。
    """
    out: dict[tuple[str, str], dict[str, float]] = {}
    frames = []
    for day in days:
        p = data_dir / "kline_daily" / f"date={day}"
        if not p.exists():
            continue
        frames.append(
            pl.scan_parquet(str(p / "*.parquet"))
            .select("symbol", "open", "close", "high", "low")
            .with_columns(pl.lit(day).alias("day"))
            .collect()
        )
    if not frames:
        return out
    df = pl.concat(frames)
    if symbols:
        df = df.filter(pl.col("symbol").is_in(list(symbols)))
    for row in df.iter_rows(named=True):
        out[(row["day"], row["symbol"])] = {
            "open": float(row["open"]),
            "close": float(row["close"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
        }
    return out


def prev_closes(data_dir: Path, day: str, symbols: list[str]) -> dict[str, float]:
    """取 day 之前最近一个交易日的 raw_close (涨跌停判定基准)。"""
    all_days = sorted(
        x.name.removeprefix("date=") for x in (data_dir / "kline_daily").glob("date=*")
    )
    earlier = [d for d in all_days if d < day]
    if not earlier:
        return {}
    prev = earlier[-1]
    p = data_dir / "kline_daily" / f"date={prev}" / "*.parquet"
    df = pl.scan_parquet(str(p)).filter(pl.col("symbol").is_in(symbols)).select("symbol", "close").collect()
    return {r["symbol"]: float(r["close"]) for r in df.iter_rows(named=True)}


def run_backtest(service, strategy_id: str, start: date, end: date, rule: dict, acc: dict):
    """按模拟盘自动规则的参数跑回测, 返回成交事件列表。"""
    cfg = StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=start,
        end=end,
        entry_fill=rule.get("entry_fill") or "open_t+1",
        exit_fill=rule.get("exit_fill") or "close_t",
        commission_pct=float(acc.get("commission_pct", paper.DEFAULT_COMMISSION_PCT)),
        stamp_tax_pct=float(acc.get("stamp_tax_pct", paper.DEFAULT_STAMP_TAX_PCT)),
        slippage_bps=float(acc.get("slippage_bps", paper.DEFAULT_SLIPPAGE_BPS)),
        max_positions=int(rule.get("max_positions") or 5),
        max_exposure_pct=float(rule.get("max_total_position", 100.0)) / 100.0,
        initial_capital=float(acc.get("initial_cash", 30000.0)),
        position_sizing=rule.get("position_sizing") or "equal",
        mode="position",
        asset_type="stock",
    )
    res = service.run(cfg)
    if res.error:
        raise RuntimeError(f"回测失败 {strategy_id}: {res.error}")
    return res


def replay(res, acc: dict, rule: dict, bars, data_dir: Path, days: list[str], strategy_id: str,
           strategy_name: str = ""):
    """按模拟盘口径重放回测成交事件 -> (fills, 每日快照)。"""
    commission = float(acc.get("commission_pct", paper.DEFAULT_COMMISSION_PCT))
    stamp = float(acc.get("stamp_tax_pct", paper.DEFAULT_STAMP_TAX_PCT))
    slip = float(acc.get("slippage_bps", paper.DEFAULT_SLIPPAGE_BPS))

    fills: list[dict] = []
    warnings: list[str] = []
    cash = float(acc["initial_cash"])
    positions: dict[str, dict] = {}
    seq = 0

    events: list[dict] = []
    for t in res.trades:
        events.append({
            "kind": "buy", "day": str(t["entry_date"])[:10], "symbol": t["symbol"],
            "budget": float(t["entry_value"]), "score": t.get("entry_score"),
            "signal_date": str(t.get("entry_signal_date") or "")[:10],
            "reason": "signal", "trade": t,
        })
        if str(t["exit_reason"]) != "end":
            events.append({
                "kind": "sell", "day": str(t["exit_date"])[:10], "symbol": t["symbol"],
                "qty": int(t["shares"]), "reason": t["exit_reason"], "trade": t,
            })
    # 同一天先卖后买: 卖出释放现金与仓位, 与撮合顺序一致
    events.sort(key=lambda e: (e["day"], 0 if e["kind"] == "sell" else 1))
    by_day: dict[str, list[dict]] = {}
    for ev in events:
        by_day.setdefault(ev["day"], []).append(ev)

    nav_rows: list[dict] = []
    for day in days:
        # ── 当日成交 (先卖后买, 与撮合顺序一致) ──
        for ev in by_day.get(day, []):
            symbol = ev["symbol"]
            bar = bars.get((day, symbol))
            if bar is None:
                warnings.append(f"{day} {symbol} 缺原始行情, 跳过 {ev['kind']}")
                continue

            if ev["kind"] == "buy":
                raw_px = bar["open"]  # entry_fill=open_t+1 -> 当日开盘
                px = paper.apply_slippage(raw_px, "buy", slip)
                budget = ev["budget"]
                qty = paper.normalize_qty(int(budget / (px * (1 + commission + slip / 10000.0))))
                if qty <= 0:
                    # 预算买不起一手时, 现金充足则补一手 (真实交易常见做法)
                    if px > 0 and cash >= paper.LOT_SIZE * px + paper.buy_fee(paper.LOT_SIZE, px, commission):
                        qty = paper.LOT_SIZE
                        warnings.append(f"{day} {symbol} 预算不足一手, 按一手补建")
                    else:
                        warnings.append(f"{day} {symbol} 预算不足一手, 跳过买入")
                        continue
                fee = paper.buy_fee(qty, px, commission)
                cost = qty * px + fee
                if cost > cash + 1e-6:  # 现金不足则缩到最大可买手数
                    qty = paper.normalize_qty(int((cash - fee) / px)) if px > 0 else 0
                    if qty <= 0:
                        warnings.append(f"{day} {symbol} 现金不足, 跳过买入")
                        continue
                    fee = paper.buy_fee(qty, px, commission)
                    cost = qty * px + fee
                # 涨停不可买 (queue_limit_orders=false -> 拒单)
                prev = prev_closes(data_dir, day, [symbol]).get(symbol)
                if prev:
                    upper, _ = paper.limit_prices(prev, symbol, "stock")
                    if raw_px >= upper:
                        warnings.append(f"{day} {symbol} 开盘触及涨停 {upper}, 拒单")
                        continue
                cash -= cost
                seq += 1
                fills.append({
                    "seq": seq, "date": day, "symbol": symbol, "side": "buy", "qty": qty,
                    "price": round(px, 4), "fee": round(fee, 2), "asset_type": "stock",
                    "order_id": f"bf{seq:04d}", "strategy_id": strategy_id,
                    "strategy_snapshot": {"strategy_name": strategy_name},
                    "buy_score": ev.get("score"),
                    "auto_rule_id": rule.get("id"), "automation_source": "auto_follow",
                    "strategy_scope_id": strategy_id, "source": f"auto:{rule.get('id')}",
                    "kind": "fill", "signal_date": ev.get("signal_date"),
                    "backfill": True,
                })
                pos = positions.setdefault(symbol, {"qty": 0, "cost": 0.0, "lots": []})
                pos["cost"] += cost
                pos["qty"] += qty
                pos["lots"].append({"date": day, "qty": qty, "price": px})
            else:
                pos = positions.get(symbol)
                if not pos or pos["qty"] <= 0:
                    warnings.append(f"{day} {symbol} 无持仓可卖, 跳过")
                    continue
                qty = min(int(ev["qty"]), pos["qty"])
                # T+1: 当日买入的 lot 不可卖
                sellable = sum(l["qty"] for l in pos["lots"] if l["date"] < day)
                if sellable <= 0:
                    warnings.append(f"{day} {symbol} T+1 未解锁, 跳过卖出")
                    continue
                qty = min(qty, sellable)
                if qty >= paper.LOT_SIZE:
                    qty = paper.normalize_qty(qty)
                raw_px = bar["close"]  # exit_fill=close_t -> 当日收盘
                px = paper.apply_slippage(raw_px, "sell", slip)
                prev = prev_closes(data_dir, day, [symbol]).get(symbol)
                if prev:
                    _, lower = paper.limit_prices(prev, symbol, "stock")
                    if raw_px <= lower:
                        warnings.append(f"{day} {symbol} 收盘触及跌停 {lower}, 拒单")
                        continue
                fee = paper.sell_fee(qty, px, commission, stamp)
                cash += qty * px - fee
                remain = qty
                lots_out = []
                for lot in pos["lots"]:
                    if remain > 0 and lot["date"] < day:
                        take = min(lot["qty"], remain)
                        lot["qty"] -= take
                        remain -= take
                    if lot["qty"] > 0:
                        lots_out.append(lot)
                pos["lots"] = lots_out
                pos["cost"] -= qty * (pos["cost"] / pos["qty"]) if pos["qty"] else 0.0
                pos["qty"] -= qty
                seq += 1
                fills.append({
                    "seq": seq, "date": day, "symbol": symbol, "side": "sell", "qty": qty,
                    "price": round(px, 4), "fee": round(fee, 2), "asset_type": "stock",
                    "order_id": f"bf{seq:04d}", "strategy_id": strategy_id,
                    "auto_rule_id": rule.get("id"), "automation_source": "auto_follow",
                    "source": f"auto:{rule.get('id')}", "kind": "fill",
                    "exit_reason": ev["reason"], "backfill": True,
                })
                if pos["qty"] <= 0:
                    positions.pop(symbol, None)

        # ── 当日收盘快照 (持仓按 raw_close 定版) ──
        mv = 0.0
        for symbol, pos in positions.items():
            bar = bars.get((day, symbol))
            px = bar["close"] if bar else None
            if px is None or px <= 0:  # 缺行情按成本计 (与 daily_nav 一致)
                px = pos["cost"] / pos["qty"] if pos["qty"] else 0.0
            mv += pos["qty"] * px
        nav_rows.append({
            "date": day,
            "cash": round(cash, 2),
            "mv": round(mv, 2),
            "nav": round(cash + mv, 2),
            "positions": len(positions),
        })
    return fills, nav_rows, positions, cash, warnings


def main() -> int:
    _patch_windows_process_probe()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-09-01")
    ap.add_argument("--end", default="2026-10-21")
    ap.add_argument("--accounts", nargs="*", default=None, help="默认全部 arena_* 账户")
    ap.add_argument("--apply", action="store_true", help="写入账户目录; 默认只打印")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    store, service = _build_service()
    data_dir = store.data_dir

    all_days = sorted(
        d for d in (x.name.removeprefix("date=") for x in (data_dir / "kline_daily").glob("date=*"))
        if start.isoformat() <= d <= end.isoformat()
    )
    print(f"窗口 {start} .. {end}: {len(all_days)} 个交易日")

    account_ids = args.accounts or sorted(
        p.name for p in (data_dir / "paper" / "accounts").glob("arena_*")
    )
    print(f"账户: {account_ids}")

    for aid in account_ids:
        acc = paper.get_account(data_dir, aid)
        if acc is None:
            print(f"\n[跳过] {aid}: 账户不存在")
            continue
        rules = [r for r in paper_auto.load_auto_rules(data_dir, aid)
                 if r.get("match_kind") == "strategy" and r.get("enabled", True)]
        if not rules:
            print(f"\n[跳过] {aid}: 无启用的策略自动规则")
            continue
        rule = rules[0]
        sid = rule["match_id"]
        print(f"\n===== {aid} | {acc['name']} | 策略 {sid} =====")

        res = run_backtest(service, sid, start, end, rule, acc)
        print(f"  回测: {len(res.trades)} 笔, 期末 {res.stats.get('final_equity')}, "
              f"收益 {res.stats.get('total_return')}")

        try:
            strategy_name = service.strategy_engine.get(sid).meta.get("name", sid)
        except Exception:  # noqa: BLE001
            strategy_name = sid
        symbols = {t["symbol"] for t in res.trades}
        bars = load_raw_bars(data_dir, all_days, symbols)
        fills, nav_rows, positions, cash, warnings = replay(
            res, acc, rule, bars, data_dir, all_days, sid, strategy_name
        )
        first, last = nav_rows[0], nav_rows[-1]
        ret = (last["nav"] / first["nav"] - 1) if first["nav"] else 0.0
        print(f"  回填: {len(fills)} 条成交, 期末现金 {cash:.2f}, "
              f"持仓 {len(positions)} 只, 末日净值 {last['nav']}, 区间收益 {ret:+.2%}")
        for sym, pos in positions.items():
            print(f"     持仓 {sym}: {pos['qty']} 股, 成本 {pos['cost']:.2f}")
        for w in warnings:
            print(f"     [警告] {w}")
        print("  净值曲线首尾:")
        for row in nav_rows[:2] + nav_rows[-2:]:
            print(f"     {row['date']}  nav={row['nav']}  cash={row['cash']}  "
                  f"mv={row['mv']}  持仓={row['positions']}")

        if not args.apply:
            continue
        fills_path = paper._fills_path(data_dir, aid)
        with paper.PAPER_LOCK:
            fills_path.parent.mkdir(parents=True, exist_ok=True)
            with fills_path.open("w", encoding="utf-8") as fh:
                for f in fills:
                    fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            paper.rebuild_positions(data_dir, aid)
            acc2 = paper.get_account(data_dir, aid)
            acc2["cash"] = round(cash, 2)
            paper.save_account(data_dir, acc2, aid)
            for row in nav_rows:
                paper._write_nav_line(data_dir, row["date"], row, aid)
        print(f"  [已写入] {fills_path}")

        # ── 校验: 用 paper 域权威实现重放台账, 必须与本脚本结果一致 ──
        rp, rcash = paper.replay_positions(data_dir, account_id=aid)
        ok_pos = all(
            round(rp.get(s, {}).get("qty", 0)) == p["qty"] for s, p in positions.items()
        ) and len([s for s, p in rp.items() if p["qty"] > 0]) == len(positions)
        ok_cash = abs(rcash - round(cash, 2)) < 0.01
        print(f"  校验: 持仓一致={ok_pos} 现金一致={ok_cash} (重放 {rcash:.2f} vs 脚本 {cash:.2f})")
        if not (ok_pos and ok_cash):
            print("  [校验失败] 台账与重放结果不一致, 请检查")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
