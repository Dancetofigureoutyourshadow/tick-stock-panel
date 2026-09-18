"""本地人民币策略账户账本。

所有现金与成交金额都以人民币分落库, 业务边界使用 ``Decimal``。SQLite
事务是账户现金、资金流水、成交和持仓批次的一致性边界; 旧 ``lots`` 文件域
完全不参与本账本。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.market_time import cn_today

_CENT = Decimal("0.01")
_INIT_LOCK = threading.Lock()


class PortfolioError(ValueError):
    """可直接映射为 4xx 的账户业务错误。"""


def _decimal(value: Decimal | str | int) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PortfolioError("金额格式无效") from exc
    if not result.is_finite():
        raise PortfolioError("金额格式无效")
    return result


def _to_cents(value: Decimal | str | int) -> int:
    return int((_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP) * 100).to_integral())


def _money(cents: int) -> str:
    return f"{(Decimal(cents) / 100).quantize(_CENT):.2f}"


def _proportional_cents(total_cents: int, quantity: int, total_quantity: int) -> int:
    return int(
        (Decimal(total_cents) * Decimal(quantity) / Decimal(total_quantity)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PortfolioAccount:
    """一个数据目录对应一个单币种本地模拟账户。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "user_data" / "portfolio.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        # 多个 API worker/测试线程可能同时首次触碰账本; journal_mode 切换需串行。
        with _INIT_LOCK, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS account_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    cash_cents INTEGER NOT NULL,
                    realized_pnl_cents INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO account_state(singleton, cash_cents) VALUES (1, 0);

                CREATE TABLE IF NOT EXISTS portfolio_settings (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    max_positions INTEGER NOT NULL,
                    max_total_position TEXT NOT NULL,
                    commission_rate TEXT NOT NULL,
                    stamp_tax_rate TEXT NOT NULL
                );
                INSERT OR IGNORE INTO portfolio_settings(
                    singleton, max_positions, max_total_position,
                    commission_rate, stamp_tax_rate
                ) VALUES (1, 10, '0.80', '0.0003', '0.0005');

                CREATE TABLE IF NOT EXISTS cash_flows (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL CHECK (type IN ('deposit', 'withdrawal', 'reversal')),
                    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                    cash_delta_cents INTEGER NOT NULL,
                    reverses_id TEXT UNIQUE REFERENCES cash_flows(id),
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    buy_price TEXT NOT NULL,
                    bought_qty INTEGER NOT NULL CHECK (bought_qty > 0),
                    remaining_qty INTEGER NOT NULL CHECK (remaining_qty >= 0),
                    cost_basis_cents INTEGER NOT NULL,
                    remaining_cost_cents INTEGER NOT NULL,
                    source_strategy_id TEXT NOT NULL,
                    source_strategy_name TEXT NOT NULL,
                    source_strategy_version TEXT NOT NULL,
                    buy_score TEXT,
                    snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('holding', 'pending_sell', 'closed')),
                    pending_reason TEXT,
                    pending_signal_key TEXT,
                    pending_at TEXT,
                    ignored_signal_key TEXT,
                    high_water_price TEXT NOT NULL,
                    buy_trade_date TEXT NOT NULL,
                    buy_time TEXT NOT NULL,
                    last_evaluated_trade_date TEXT,
                    holding_days INTEGER NOT NULL DEFAULT 0,
                    realized_pnl_cents INTEGER NOT NULL DEFAULT 0,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL REFERENCES positions(id),
                    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
                    symbol TEXT NOT NULL,
                    price TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    gross_cents INTEGER NOT NULL,
                    commission_cents INTEGER NOT NULL,
                    stamp_tax_cents INTEGER NOT NULL,
                    cash_delta_cents INTEGER NOT NULL,
                    realized_pnl_cents INTEGER,
                    trade_date TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                UPDATE positions
                   SET remaining_cost_cents = cost_basis_cents - COALESCE((
                       SELECT SUM(cash_delta_cents)
                         FROM trades
                        WHERE trades.position_id = positions.id
                          AND trades.side = 'sell'
                   ), 0)
                 WHERE remaining_qty > 0;
                """
            )

    def settings(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_settings WHERE singleton = 1"
            ).fetchone()
        return {
            "max_positions": int(row["max_positions"]),
            "max_total_position": row["max_total_position"],
            "commission_rate": row["commission_rate"],
            "stamp_tax_rate": row["stamp_tax_rate"],
        }

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.settings()
        merged = {**current, **values}
        try:
            max_positions = int(merged["max_positions"])
        except (TypeError, ValueError) as exc:
            raise PortfolioError("最大持仓数必须是正整数") from exc
        if isinstance(merged["max_positions"], bool) or max_positions <= 0:
            raise PortfolioError("最大持仓数必须是正整数")
        max_total = _decimal(merged["max_total_position"])
        commission = _decimal(merged["commission_rate"])
        stamp_tax = _decimal(merged["stamp_tax_rate"])
        if not (Decimal("0") < max_total <= Decimal("1")):
            raise PortfolioError("最大总仓位必须大于 0 且不超过 1")
        if not (Decimal("0") <= commission < Decimal("1")):
            raise PortfolioError("佣金率必须在 0 到 1 之间")
        if not (Decimal("0") <= stamp_tax < Decimal("1")):
            raise PortfolioError("印花税率必须在 0 到 1 之间")
        with self._transaction() as connection:
            connection.execute(
                """UPDATE portfolio_settings
                   SET max_positions = ?, max_total_position = ?,
                       commission_rate = ?, stamp_tax_rate = ?
                   WHERE singleton = 1""",
                (
                    max_positions,
                    _decimal_text(max_total),
                    _decimal_text(commission),
                    _decimal_text(stamp_tax),
                ),
            )
        return self.settings()

    def preview_buys(
        self,
        candidates: list[dict[str, Any]],
        *,
        current_prices: dict[str, Decimal | str | int],
    ) -> dict[str, Any]:
        """按组合回测的评分权重、仓位上限和整手口径生成建议。"""
        settings = self.settings()
        with self._connect() as connection:
            state = connection.execute(
                "SELECT cash_cents FROM account_state WHERE singleton = 1"
            ).fetchone()
            positions = connection.execute(
                """SELECT symbol, remaining_qty, remaining_cost_cents
                   FROM positions WHERE remaining_qty > 0"""
            ).fetchall()
        cash_cents = int(state["cash_cents"])
        market_value_cents = 0
        for position in positions:
            price = current_prices.get(position["symbol"])
            if price is None:
                market_value_cents += int(position["remaining_cost_cents"])
            else:
                market_value_cents += _to_cents(
                    _decimal(price) * int(position["remaining_qty"])
                )
        total_assets_cents = cash_cents + market_value_cents
        max_total = _decimal(settings["max_total_position"])
        max_exposure_cents = int(
            (Decimal(total_assets_cents) * max_total).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        exposure_capacity_cents = max(0, max_exposure_cents - market_value_cents)
        remaining_slots = max(0, int(settings["max_positions"]) - len(positions))
        single_cap_cents = (
            max_exposure_cents // int(settings["max_positions"])
            if settings["max_positions"]
            else 0
        )
        prepared: list[tuple[int, dict[str, Any], Decimal, Decimal | None]] = []
        for index, candidate in enumerate(candidates):
            score = max(_decimal(candidate.get("score") or 0), Decimal("0"))
            raw_price = candidate.get("price")
            price = self._positive_decimal(raw_price)
            prepared.append((index, candidate, score, price))
        # 与组合回测一致, 先按非负评分降序占用剩余名额; 同分保持请求顺序。
        eligible = [item for item in prepared if item[3] is not None]
        eligible.sort(key=lambda item: (-item[2], item[0]))
        selected = eligible[:remaining_slots]
        selected_indices = {item[0] for item in selected}
        total_budget_cents = min(
            cash_cents,
            exposure_capacity_cents,
            single_cap_cents * len(selected),
        )
        scores = [item[2] for item in selected]
        score_total = sum(scores, Decimal("0"))
        if selected and score_total > 0:
            weights = [score / score_total for score in scores]
        elif selected:
            equal = Decimal("1") / len(selected)
            weights = [equal for _ in selected]
        else:
            weights = []
        commission_rate = _decimal(settings["commission_rate"])
        weight_by_index = {
            item[0]: weight for item, weight in zip(selected, weights, strict=True)
        }
        items: list[dict[str, Any]] = []
        for index, candidate, score, maybe_price in prepared:
            symbol = str(candidate.get("symbol") or "").strip()
            blocked_reason = None
            price = maybe_price or Decimal("0")
            weight = weight_by_index.get(index, Decimal("0"))
            allocation_cents = min(
                int(
                    (Decimal(total_budget_cents) * weight).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                ),
                single_cap_cents,
            )
            quantity = 0
            fee_cents = 0
            required_cents = 0
            if maybe_price is None:
                blocked_reason = "行情缺失"
            elif index not in selected_indices:
                blocked_reason = "持仓名额已满"
            else:
                share_cost = price * (Decimal("1") + commission_rate)
                quantity = int(
                    (Decimal(allocation_cents) / 100 / share_cost / 100).to_integral_value(
                        rounding="ROUND_FLOOR"
                    )
                ) * 100
                if quantity <= 0:
                    blocked_reason = "预算不足一手"
                else:
                    gross_cents = _to_cents(price * quantity)
                    fee_cents = int(
                        (Decimal(gross_cents) * commission_rate).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                    )
                    required_cents = gross_cents + fee_cents
            items.append(
                {
                    "symbol": symbol,
                    "score": _decimal_text(score),
                    "weight": _decimal_text(weight),
                    "price": _decimal_text(price),
                    "suggested_budget": _money(allocation_cents),
                    "suggested_qty": quantity,
                    "estimated_fee": _money(fee_cents),
                    "estimated_cash_required": _money(required_cents),
                    "blocked_reason": blocked_reason,
                }
            )
        return {
            "available_cash": _money(cash_cents),
            "market_value": _money(market_value_cents),
            "total_assets": _money(total_assets_cents),
            "total_budget": _money(total_budget_cents),
            "single_position_cap": _money(single_cap_cents),
            "remaining_slots": remaining_slots,
            "items": items,
        }

    def record_cash(
        self,
        flow_type: str,
        amount: Decimal | str | int,
        *,
        note: str = "",
    ) -> dict[str, Any]:
        if flow_type not in {"deposit", "withdrawal"}:
            raise PortfolioError("资金流水类型必须是 deposit 或 withdrawal")
        cents = _to_cents(amount)
        if cents <= 0:
            raise PortfolioError("金额必须大于 0")
        delta = cents if flow_type == "deposit" else -cents
        flow_id = uuid.uuid4().hex
        created_at = _now_iso()
        with self._transaction() as connection:
            cash = int(
                connection.execute(
                    "SELECT cash_cents FROM account_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            if cash + delta < 0:
                raise PortfolioError("可用现金不足")
            connection.execute(
                "UPDATE account_state SET cash_cents = cash_cents + ? WHERE singleton = 1",
                (delta,),
            )
            connection.execute(
                """INSERT INTO cash_flows(
                       id, type, amount_cents, cash_delta_cents, note, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (flow_id, flow_type, cents, delta, note.strip(), created_at),
            )
        return self._cash_flow(flow_id)

    def reverse_cash(self, flow_id: str, *, note: str = "") -> dict[str, Any]:
        reversal_id = uuid.uuid4().hex
        created_at = _now_iso()
        with self._transaction() as connection:
            original = connection.execute(
                "SELECT * FROM cash_flows WHERE id = ?", (flow_id,)
            ).fetchone()
            if original is None:
                raise PortfolioError("资金流水不存在")
            if original["type"] == "reversal":
                raise PortfolioError("冲正记录不能再次冲正")
            if connection.execute(
                "SELECT 1 FROM cash_flows WHERE reverses_id = ?", (flow_id,)
            ).fetchone():
                raise PortfolioError("该资金流水已冲正")
            delta = -int(original["cash_delta_cents"])
            cash = int(
                connection.execute(
                    "SELECT cash_cents FROM account_state WHERE singleton = 1"
                ).fetchone()[0]
            )
            if cash + delta < 0:
                raise PortfolioError("可用现金不足, 无法冲正该入金")
            connection.execute(
                "UPDATE account_state SET cash_cents = cash_cents + ? WHERE singleton = 1",
                (delta,),
            )
            connection.execute(
                """INSERT INTO cash_flows(
                       id, type, amount_cents, cash_delta_cents, reverses_id, note, created_at
                   ) VALUES (?, 'reversal', ?, ?, ?, ?, ?)""",
                (
                    reversal_id,
                    int(original["amount_cents"]),
                    delta,
                    flow_id,
                    note.strip(),
                    created_at,
                ),
            )
        return self._cash_flow(reversal_id)

    def buy(
        self,
        *,
        symbol: str,
        name: str,
        price: Decimal | str | int,
        quantity: int,
        strategy_snapshot: dict[str, Any],
        trade_date: str,
        current_prices: dict[str, Decimal | str | int] | None = None,
        add_to_watchlist: Any | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        if not symbol:
            raise PortfolioError("股票代码不能为空")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise PortfolioError("买入数量必须是正整数")
        if quantity % 100 != 0:
            raise PortfolioError("A 股买入数量必须是 100 股的整数倍")
        trade_day = self._validate_trade_date(trade_date)
        execution_price = _decimal(price)
        if execution_price <= 0:
            raise PortfolioError("成交价必须大于 0")
        strategy_id = str(strategy_snapshot.get("strategy_id") or "").strip()
        if not strategy_id:
            raise PortfolioError("每个买入批次必须选择一个来源策略")
        snapshot_json = json.dumps(
            strategy_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        position_id = uuid.uuid4().hex
        trade_id = uuid.uuid4().hex
        created_at = _now_iso()
        with self._transaction() as connection:
            settings = connection.execute(
                "SELECT * FROM portfolio_settings WHERE singleton = 1"
            ).fetchone()
            state = connection.execute(
                "SELECT cash_cents FROM account_state WHERE singleton = 1"
            ).fetchone()
            open_positions = connection.execute(
                """SELECT symbol, remaining_qty, remaining_cost_cents FROM positions
                   WHERE remaining_qty > 0"""
            ).fetchall()
            if len(open_positions) >= int(settings["max_positions"]):
                raise PortfolioError("持仓名额已满")
            cash_cents = int(state["cash_cents"])
            marks = current_prices or {}
            invested_cents = 0
            for row in open_positions:
                mark = self._positive_decimal(marks.get(row["symbol"]))
                invested_cents += (
                    int(row["remaining_cost_cents"])
                    if mark is None
                    else _to_cents(mark * int(row["remaining_qty"]))
                )
            total_assets_cents = cash_cents + invested_cents
            max_exposure_cents = int(
                (
                    Decimal(total_assets_cents)
                    * _decimal(settings["max_total_position"])
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            single_cap_cents = max_exposure_cents // int(settings["max_positions"])
            gross_cents = _to_cents(execution_price * quantity)
            commission_cents = int(
                (Decimal(gross_cents) * _decimal(settings["commission_rate"])).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            required_cents = gross_cents + commission_cents
            if gross_cents > single_cap_cents:
                raise PortfolioError("买入金额超过单票仓位上限")
            if gross_cents > max(0, max_exposure_cents - invested_cents):
                raise PortfolioError("买入后将超过最大总仓位")
            if required_cents > cash_cents:
                raise PortfolioError("可用现金不足")
            connection.execute(
                "UPDATE account_state SET cash_cents = cash_cents - ? WHERE singleton = 1",
                (required_cents,),
            )
            connection.execute(
                """INSERT INTO positions(
                       id, symbol, name, buy_price, bought_qty, remaining_qty,
                       cost_basis_cents, remaining_cost_cents,
                       source_strategy_id, source_strategy_name, source_strategy_version,
                       buy_score, snapshot_json, status, high_water_price,
                       buy_trade_date, buy_time
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'holding', ?, ?, ?)""",
                (
                    position_id,
                    symbol,
                    name.strip(),
                    _decimal_text(execution_price),
                    quantity,
                    quantity,
                    required_cents,
                    required_cents,
                    strategy_id,
                    str(strategy_snapshot.get("strategy_name") or strategy_id),
                    str(strategy_snapshot.get("strategy_version") or ""),
                    (
                        None
                        if strategy_snapshot.get("buy_score") is None
                        else str(strategy_snapshot["buy_score"])
                    ),
                    snapshot_json,
                    _decimal_text(execution_price),
                    trade_day,
                    created_at,
                ),
            )
            connection.execute(
                """INSERT INTO trades(
                       id, position_id, side, symbol, price, quantity, gross_cents,
                       commission_cents, stamp_tax_cents, cash_delta_cents,
                       trade_date, created_at
                   ) VALUES (?, ?, 'buy', ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (
                    trade_id,
                    position_id,
                    symbol,
                    _decimal_text(execution_price),
                    quantity,
                    gross_cents,
                    commission_cents,
                    -required_cents,
                    trade_day,
                    created_at,
                ),
            )
            if add_to_watchlist is not None:
                add_to_watchlist(symbol)
        return {
            "position": self._position(position_id),
            "trade": self._trade(trade_id),
        }

    def sell(
        self,
        position_id: str,
        *,
        price: Decimal | str | int,
        quantity: int,
        trade_date: str,
        remove_from_watchlist: bool = False,
        remove_watchlist: Any | None = None,
    ) -> dict[str, Any]:
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise PortfolioError("卖出数量必须是正整数股")
        execution_price = _decimal(price)
        if execution_price <= 0:
            raise PortfolioError("成交价必须大于 0")
        trade_day = self._validate_trade_date(trade_date)
        trade_id = uuid.uuid4().hex
        created_at = _now_iso()
        with self._transaction() as connection:
            position = connection.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            if position is None:
                raise PortfolioError("持仓批次不存在")
            remaining_qty = int(position["remaining_qty"])
            if remaining_qty <= 0:
                raise PortfolioError("持仓批次已全部卖出")
            if not self._is_sellable_day(position["buy_trade_date"], trade_day):
                raise PortfolioError("A 股实行 T+1, 当日买入数量尚不可卖")
            if quantity > remaining_qty:
                raise PortfolioError("卖出数量超过该批次可卖数量")
            settings = connection.execute(
                "SELECT * FROM portfolio_settings WHERE singleton = 1"
            ).fetchone()
            gross_cents = _to_cents(execution_price * quantity)
            commission_cents = int(
                (Decimal(gross_cents) * _decimal(settings["commission_rate"])).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            stamp_tax_cents = int(
                (Decimal(gross_cents) * _decimal(settings["stamp_tax_rate"])).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            proceeds_cents = gross_cents - commission_cents - stamp_tax_cents
            old_cost_cents = int(position["remaining_cost_cents"])
            original_cost_cents = int(position["cost_basis_cents"])
            bought_qty = int(position["bought_qty"])
            next_qty = remaining_qty - quantity
            sold_qty = bought_qty - remaining_qty
            basis_sold_before_cents = _proportional_cents(
                original_cost_cents, sold_qty, bought_qty
            )
            basis_sold_after_cents = (
                original_cost_cents
                if next_qty == 0
                else _proportional_cents(
                    original_cost_cents, sold_qty + quantity, bought_qty
                )
            )
            allocated_cost_cents = basis_sold_after_cents - basis_sold_before_cents
            realized_cents = proceeds_cents - allocated_cost_cents
            # Remaining cost is the capital still tied up in the position. A
            # partial sale releases its net proceeds, so a profitable sale
            # lowers the cost line while a losing sale raises it.
            next_cost_cents = (
                0 if next_qty == 0 else old_cost_cents - proceeds_cents
            )
            next_status = "closed" if next_qty == 0 else position["status"]
            connection.execute(
                """UPDATE positions
                   SET remaining_qty = ?, remaining_cost_cents = ?, status = ?,
                       pending_reason = CASE WHEN ? = 0 THEN NULL ELSE pending_reason END,
                       pending_signal_key = CASE WHEN ? = 0 THEN NULL ELSE pending_signal_key END,
                       pending_at = CASE WHEN ? = 0 THEN NULL ELSE pending_at END,
                       realized_pnl_cents = realized_pnl_cents + ?,
                       closed_at = CASE WHEN ? = 0 THEN ? ELSE closed_at END
                   WHERE id = ?""",
                (
                    next_qty,
                    next_cost_cents,
                    next_status,
                    next_qty,
                    next_qty,
                    next_qty,
                    realized_cents,
                    next_qty,
                    created_at,
                    position_id,
                ),
            )
            connection.execute(
                """UPDATE account_state
                   SET cash_cents = cash_cents + ?,
                       realized_pnl_cents = realized_pnl_cents + ?
                   WHERE singleton = 1""",
                (proceeds_cents, realized_cents),
            )
            connection.execute(
                """INSERT INTO trades(
                       id, position_id, side, symbol, price, quantity, gross_cents,
                       commission_cents, stamp_tax_cents, cash_delta_cents,
                       realized_pnl_cents, trade_date, created_at
                   ) VALUES (?, ?, 'sell', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade_id,
                    position_id,
                    position["symbol"],
                    _decimal_text(execution_price),
                    quantity,
                    gross_cents,
                    commission_cents,
                    stamp_tax_cents,
                    proceeds_cents,
                    realized_cents,
                    trade_day,
                    created_at,
                ),
            )
            if remove_from_watchlist and next_qty == 0 and remove_watchlist is not None:
                still_open = connection.execute(
                    """SELECT 1 FROM positions
                       WHERE symbol = ? AND remaining_qty > 0 LIMIT 1""",
                    (position["symbol"],),
                ).fetchone()
                if still_open is None:
                    remove_watchlist(position["symbol"])
        return {
            "position": self._position(position_id),
            "trade": self._trade(trade_id),
        }

    def continue_holding(self, position_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            position = connection.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
            if position is None or int(position["remaining_qty"]) <= 0:
                raise PortfolioError("未找到可继续持有的批次")
            if position["status"] != "pending_sell":
                raise PortfolioError("该批次当前不在待卖出状态")
            connection.execute(
                """UPDATE positions
                   SET status = 'holding', ignored_signal_key = pending_signal_key,
                       pending_reason = NULL, pending_signal_key = NULL, pending_at = NULL
                   WHERE id = ?""",
                (position_id,),
            )
        return self._position(position_id)

    def evaluate_exit_rules(
        self,
        rows: list[dict[str, Any]],
        *,
        trade_date: str,
        now_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """只检查当前批次的规则快照, 不重新运行完整选股。"""
        evaluation_day = self._validate_trade_date(trade_date)
        event_ts = now_ms if now_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        row_by_symbol = {
            str(row.get("symbol") or "").upper(): row
            for row in rows
            if row.get("symbol")
        }
        events: list[dict[str, Any]] = []
        reason_labels = {
            "stop_loss": "止损",
            "trailing_stop": "移动止损",
            "trailing_take_profit": "移动止盈",
            "take_profit": "止盈",
            "signal": "策略卖点",
            "max_hold": "最大持有期",
        }
        with self._transaction() as connection:
            positions = connection.execute(
                """SELECT * FROM positions
                   WHERE remaining_qty > 0 ORDER BY rowid"""
            ).fetchall()
            for position in positions:
                if position["status"] == "pending_sell":
                    continue
                row = row_by_symbol.get(position["symbol"])
                if row is None:
                    continue
                close = self._positive_decimal(
                    row.get("raw_close") or row.get("last_price") or row.get("close")
                )
                if close is None:
                    continue
                high = self._positive_decimal(row.get("raw_high") or row.get("high")) or close
                low = self._positive_decimal(row.get("raw_low") or row.get("low")) or close
                buy_price = _decimal(position["buy_price"])
                old_high = _decimal(position["high_water_price"])
                next_high = max(old_high, high)
                holding_days = int(position["holding_days"])
                last_day = position["last_evaluated_trade_date"]
                if evaluation_day > position["buy_trade_date"] and last_day != evaluation_day:
                    holding_days += 1

                # 与回测一致: 买入当日不执行退出; 风控 → 策略卖点 → 最大持有期。
                reason: str | None = None
                hit_signals: list[str] = []
                snapshot = json.loads(position["snapshot_json"])
                if evaluation_day > position["buy_trade_date"]:
                    risk_lines: list[tuple[Decimal, str]] = []
                    stop_loss = self._optional_abs_decimal(snapshot.get("stop_loss"))
                    if stop_loss is not None:
                        risk_lines.append((buy_price * (Decimal("1") - stop_loss), "stop_loss"))
                    trailing_stop = self._optional_abs_decimal(snapshot.get("trailing_stop"))
                    if trailing_stop is not None:
                        risk_lines.append((old_high * (Decimal("1") - trailing_stop), "trailing_stop"))
                    activate = self._optional_abs_decimal(
                        snapshot.get("trailing_take_profit_activate")
                    )
                    drawdown = self._optional_abs_decimal(
                        snapshot.get("trailing_take_profit_drawdown")
                    )
                    if (
                        activate is not None
                        and drawdown is not None
                        and old_high > buy_price
                        and old_high / buy_price - Decimal("1") >= activate
                    ):
                        risk_lines.append(
                            (old_high * (Decimal("1") - drawdown), "trailing_take_profit")
                        )
                    triggered_lines = [item for item in risk_lines if low <= item[0]]
                    if triggered_lines:
                        _, reason = max(triggered_lines, key=lambda item: item[0])
                    if reason is None:
                        take_profit = self._optional_abs_decimal(snapshot.get("take_profit"))
                        if take_profit is not None and high >= buy_price * (
                            Decimal("1") + take_profit
                        ):
                            reason = "take_profit"
                    if reason is None:
                        hit_signals = [
                            signal
                            for signal in snapshot.get("exit_signals") or []
                            if bool(row.get(signal))
                        ]
                        if hit_signals:
                            reason = "signal"
                    if reason is None:
                        max_hold = snapshot.get("max_hold_days")
                        try:
                            max_hold_days = int(max_hold) if max_hold is not None else None
                        except (TypeError, ValueError):
                            max_hold_days = None
                        if max_hold_days is not None and holding_days >= max_hold_days:
                            reason = "max_hold"

                fingerprint = (
                    None
                    if reason is None
                    else f"{reason}:{','.join(sorted(hit_signals))}"
                )
                ignored = position["ignored_signal_key"]
                connection.execute(
                    """UPDATE positions
                       SET high_water_price = ?, holding_days = ?,
                           last_evaluated_trade_date = ?,
                           ignored_signal_key = CASE WHEN ? IS NULL THEN NULL ELSE ignored_signal_key END
                       WHERE id = ?""",
                    (
                        _decimal_text(next_high),
                        holding_days,
                        evaluation_day,
                        fingerprint,
                        position["id"],
                    ),
                )
                if fingerprint is None or fingerprint == ignored:
                    continue
                pending_at = datetime.fromtimestamp(event_ts / 1000, tz=UTC).isoformat()
                connection.execute(
                    """UPDATE positions
                       SET status = 'pending_sell', pending_reason = ?,
                           pending_signal_key = ?, pending_at = ?
                       WHERE id = ?""",
                    (reason, fingerprint, pending_at, position["id"]),
                )
                label = reason_labels.get(reason or "", reason or "卖出")
                events.append(
                    {
                        "ts": event_ts,
                        "rule_id": f"portfolio_{position['id']}",
                        "rule_name": f"持仓批次退出 · {position['symbol']}",
                        "position_id": position["id"],
                        "strategy_id": position["source_strategy_id"],
                        "source": "portfolio",
                        "type": "position_exit",
                        "exit_reason": reason,
                        "symbol": position["symbol"],
                        "name": position["name"] or position["symbol"],
                        "message": f"持仓批次触发{label}, 请确认卖出",
                        "price": float(close),
                        "change_pct": row.get("change_pct"),
                        "signals": hit_signals,
                        "severity": "warn",
                        "conditions": [],
                        "logic": "or",
                        "webhook_channels": list(snapshot.get("webhook_channels") or []),
                    }
                )
        return events

    @staticmethod
    def _positive_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            result = _decimal(value)
        except PortfolioError:
            return None
        return result if result > 0 else None

    @staticmethod
    def _optional_abs_decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return abs(_decimal(value))
        except PortfolioError:
            return None

    @staticmethod
    def _validate_trade_date(value: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as exc:
            raise PortfolioError("交易日期必须是 YYYY-MM-DD") from exc

    def _is_sellable_day(self, buy_trade_date: str, valuation_day: str) -> bool:
        if valuation_day <= buy_trade_date:
            return False
        # 本地存在交易日分区时以它为准, 休市日不误开放可卖数量。空数据目录
        # (如首次启动或单元测试) 则退化为日期先后关系。
        roots = (self.data_dir / "kline_daily", self.data_dir / "kline_daily_enriched")
        known_days: set[str] = set()
        for root in roots:
            try:
                known_days.update(
                    entry.name.removeprefix("date=")
                    for entry in root.glob("date=????-??-??")
                    if entry.is_dir()
                )
            except OSError:
                continue
        return valuation_day in known_days if known_days else True

    def _cash_flow(self, flow_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cash_flows WHERE id = ?", (flow_id,)
            ).fetchone()
        if row is None:
            raise PortfolioError("资金流水不存在")
        return {
            "id": row["id"],
            "type": row["type"],
            "amount": _money(row["amount_cents"]),
            "cash_delta": _money(row["cash_delta_cents"]),
            "reverses_id": row["reverses_id"],
            "note": row["note"],
            "created_at": row["created_at"],
        }

    def _position(self, position_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            ).fetchone()
        if row is None:
            raise PortfolioError("持仓批次不存在")
        snapshot = json.loads(row["snapshot_json"])
        exit_rule_keys = (
            "exit_signals",
            "stop_loss",
            "take_profit",
            "trailing_stop",
            "trailing_take_profit_activate",
            "trailing_take_profit_drawdown",
            "max_hold_days",
        )
        has_exit_rules = bool(snapshot.get("exit_signals")) or any(
            snapshot.get(key) is not None for key in exit_rule_keys[1:]
        )
        return {
            "id": row["id"],
            "symbol": row["symbol"],
            "name": row["name"],
            "buy_price": row["buy_price"],
            "bought_qty": int(row["bought_qty"]),
            "remaining_qty": int(row["remaining_qty"]),
            "cost_basis": _money(row["cost_basis_cents"]),
            "remaining_cost": _money(row["remaining_cost_cents"]),
            "source_strategy_id": row["source_strategy_id"],
            "source_strategy_name": row["source_strategy_name"],
            "source_strategy_version": row["source_strategy_version"],
            "buy_score": row["buy_score"],
            "strategy_snapshot": snapshot,
            "status": row["status"],
            "pending_reason": row["pending_reason"],
            "pending_at": row["pending_at"],
            "has_exit_rules": has_exit_rules,
            "high_water_price": row["high_water_price"],
            "buy_trade_date": row["buy_trade_date"],
            "buy_time": row["buy_time"],
            "holding_days": int(row["holding_days"]),
            "realized_pnl": _money(row["realized_pnl_cents"]),
            "closed_at": row["closed_at"],
        }

    def get_position(self, position_id: str) -> dict[str, Any]:
        return self._position(position_id)

    def open_symbols(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT symbol FROM positions
                   WHERE remaining_qty > 0 ORDER BY symbol"""
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def positions(
        self,
        prices: dict[str, Decimal | str | int],
        *,
        as_of: str | None = None,
        include_closed: bool = False,
    ) -> list[dict[str, Any]]:
        valuation_day = self._validate_trade_date(as_of or cn_today().isoformat())
        with self._connect() as connection:
            if include_closed:
                rows = connection.execute(
                    "SELECT id FROM positions ORDER BY rowid DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT id FROM positions
                       WHERE remaining_qty > 0 ORDER BY rowid DESC"""
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            position = self._position(row["id"])
            quantity = int(position["remaining_qty"])
            bought_qty = int(position["bought_qty"])
            original_cost_cents = _to_cents(position["cost_basis"])
            remaining_basis_cents = original_cost_cents - _proportional_cents(
                original_cost_cents, bought_qty - quantity, bought_qty
            )
            current = prices.get(position["symbol"])
            current_price = None if current is None else _decimal(current)
            market_cents = (
                remaining_basis_cents
                if current_price is None
                else _to_cents(current_price * quantity)
            )
            position.update(
                {
                    "available_qty": quantity
                    if self._is_sellable_day(position["buy_trade_date"], valuation_day)
                    else 0,
                    "current_price": (
                        None if current_price is None else _decimal_text(current_price)
                    ),
                    "market_value": _money(market_cents),
                    "unrealized_pnl": _money(market_cents - remaining_basis_cents),
                }
            )
            result.append(position)
        return result

    def _trade(self, trade_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
        if row is None:
            raise PortfolioError("成交记录不存在")
        return {
            "id": row["id"],
            "position_id": row["position_id"],
            "side": row["side"],
            "symbol": row["symbol"],
            "price": row["price"],
            "quantity": int(row["quantity"]),
            "gross_amount": _money(row["gross_cents"]),
            "commission": _money(row["commission_cents"]),
            "stamp_tax": _money(row["stamp_tax_cents"]),
            "cash_delta": _money(row["cash_delta_cents"]),
            "realized_pnl": (
                None
                if row["realized_pnl_cents"] is None
                else _money(row["realized_pnl_cents"])
            ),
            "trade_date": row["trade_date"],
            "created_at": row["created_at"],
        }

    def transactions(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            cash_rows = connection.execute(
                "SELECT * FROM cash_flows ORDER BY rowid DESC"
            ).fetchall()
            trade_rows = connection.execute(
                "SELECT * FROM trades ORDER BY rowid DESC"
            ).fetchall()
        return {
            "trades": [self._trade(row["id"]) for row in trade_rows],
            "cash_flows": [self._cash_flow(row["id"]) for row in cash_rows],
        }

    def summary(self, prices: dict[str, Decimal | str | int]) -> dict[str, str]:
        with self._connect() as connection:
            state = connection.execute(
                "SELECT cash_cents, realized_pnl_cents FROM account_state WHERE singleton = 1"
            ).fetchone()
            net_deposits = int(
                connection.execute(
                    "SELECT COALESCE(SUM(cash_delta_cents), 0) FROM cash_flows"
                ).fetchone()[0]
            )
            positions = connection.execute(
                """SELECT symbol, bought_qty, remaining_qty,
                          cost_basis_cents, remaining_cost_cents
                   FROM positions WHERE remaining_qty > 0"""
            ).fetchall()
        cash = int(state["cash_cents"])
        realized = int(state["realized_pnl_cents"])
        market_value = 0
        remaining_cost = 0
        for position in positions:
            bought_qty = int(position["bought_qty"])
            remaining_qty = int(position["remaining_qty"])
            original_cost = int(position["cost_basis_cents"])
            cost = original_cost - _proportional_cents(
                original_cost, bought_qty - remaining_qty, bought_qty
            )
            remaining_cost += cost
            price = prices.get(position["symbol"])
            market_value += (
                cost
                if price is None
                else _to_cents(_decimal(price) * remaining_qty)
            )
        unrealized = market_value - remaining_cost
        return {
            "available_cash": _money(cash),
            "market_value": _money(market_value),
            "total_assets": _money(cash + market_value),
            "net_deposits": _money(net_deposits),
            "realized_pnl": _money(realized),
            "unrealized_pnl": _money(unrealized),
            "total_pnl": _money(realized + unrealized),
        }
