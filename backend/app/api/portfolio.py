"""单一人民币本地策略账户 API。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.market_time import cn_today
from app.services import preferences, watchlist
from app.services.portfolio import PortfolioAccount, PortfolioError
from app.strategy import config as strategy_config

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class SettingsPatch(BaseModel):
    max_positions: int | None = None
    max_total_position: Decimal | None = None
    commission_rate: Decimal | None = None
    stamp_tax_rate: Decimal | None = None


class CashRequest(BaseModel):
    type: str
    amount: Decimal
    note: str = ""


class ReverseCashRequest(BaseModel):
    note: str = ""


class PreviewItem(BaseModel):
    symbol: str
    strategy_id: str
    score: Decimal | None = None
    price: Decimal | None = None


class BuyPreviewRequest(BaseModel):
    items: list[PreviewItem] = Field(min_length=1)


class BuyRequest(BaseModel):
    symbol: str
    strategy_id: str
    buy_score: Decimal | None = None
    price: Decimal | None = None
    quantity: int
    trade_date: str | None = None
    name: str = ""
    watchlist_group_id: str | None = None
    confirm_buy_warnings: bool = False


class SellRequest(BaseModel):
    price: Decimal | None = None
    quantity: int | None = None
    trade_date: str | None = None
    remove_from_watchlist: bool = False


def _account(request: Request) -> PortfolioAccount:
    account = getattr(request.app.state, "portfolio_account", None)
    if account is None:
        account = PortfolioAccount(request.app.state.repo.store.data_dir)
        request.app.state.portfolio_account = account
    return account


def _http_error(exc: PortfolioError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _finite_positive(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() and result > 0 else None


def _finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _normalise_quote(
    row: dict[str, Any],
    price: Decimal,
    price_source: str,
) -> dict[str, Any]:
    """把行情行转换成持仓展示所需的统一口径。"""
    prev_close = _finite_positive(row.get("prev_close"))
    change_amount = _finite_decimal(row.get("change_amount"))
    change_pct = _finite_decimal(row.get("change_pct"))
    if prev_close is not None:
        if change_amount is None:
            change_amount = price - prev_close
        if change_pct is None:
            change_pct = change_amount / prev_close
    else:
        # 没有昨收时禁止仅凭当前价伪造“今日”涨跌。
        change_amount = None
        change_pct = None
    return {
        "prev_close": _decimal_text(prev_close),
        "change_amount": _decimal_text(change_amount),
        "change_pct": float(change_pct) if change_pct is not None else None,
        "price_source": price_source,
    }


def _resolve_quotes(
    request: Request,
    symbols: list[str],
) -> tuple[dict[str, Decimal], dict[str, str], dict[str, dict[str, Any]]]:
    """解析持仓估值价格, 并保留同一行情快照的今日涨跌字段。"""
    wanted = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
    prices: dict[str, Decimal] = {}
    sources: dict[str, str] = {}
    quotes: dict[str, dict[str, Any]] = {}
    quote_service = getattr(request.app.state, "quote_service", None)
    if quote_service is not None and wanted:
        try:
            realtime = quote_service.get_quotes_compat()
            if isinstance(realtime, pl.DataFrame) and not realtime.is_empty():
                for row in realtime.iter_rows(named=True):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol not in wanted:
                        continue
                    price = _finite_positive(row.get("last_price") or row.get("close"))
                    if price is not None:
                        prices[symbol] = price
                        sources[symbol] = "realtime"
                        quotes[symbol] = _normalise_quote(row, price, "realtime")
        except Exception:
            pass

    missing = wanted - prices.keys()
    if missing:
        try:
            latest, _ = request.app.state.repo.get_enriched_latest()
            if isinstance(latest, pl.DataFrame) and not latest.is_empty():
                for row in latest.iter_rows(named=True):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol not in missing:
                        continue
                    # 账户成交与市值使用不复权价; 旧 schema 缺 raw_close 才回退 close。
                    price = _finite_positive(row.get("raw_close") or row.get("close"))
                    if price is not None:
                        prices[symbol] = price
                        sources[symbol] = "latest_close"
                        quotes[symbol] = _normalise_quote(row, price, "latest_close")
        except Exception:
            pass
    return prices, sources, quotes


def _resolve_prices(
    request: Request,
    symbols: list[str],
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """实时最新价优先, 缺失时降级到最新日线不复权收盘价。"""
    prices, sources, _ = _resolve_quotes(request, symbols)
    return prices, sources


def _strategy_snapshot(
    request: Request,
    strategy_id: str,
    buy_score: Decimal | None,
) -> dict[str, Any]:
    try:
        strategy = request.app.state.strategy_engine.get(strategy_id)
    except Exception as exc:
        raise PortfolioError("来源策略不存在或当前不可用") from exc
    if "stock" not in strategy.meta.get("asset_types", ["stock"]):
        raise PortfolioError("首版持仓账户仅支持 A 股股票策略")
    if "1d" not in strategy.meta.get("timeframes", ["1d"]):
        raise PortfolioError("首版持仓账户仅支持日线策略")
    data_dir = request.app.state.repo.store.data_dir
    overrides = strategy_config.load_override(data_dir, strategy_id)
    params = {
        item["id"]: item.get("default")
        for item in strategy.meta.get("params", [])
        if isinstance(item, dict) and item.get("id")
    }
    params.update(dict(overrides.get("params") or {}))

    def effective(key: str, default: Any) -> Any:
        return overrides.get(key, default)

    return {
        "strategy_id": strategy_id,
        "strategy_name": effective("name", strategy.meta.get("name") or strategy_id),
        "strategy_version": str(strategy.meta.get("version") or ""),
        "buy_score": None if buy_score is None else str(buy_score),
        "params": params,
        "entry_signals": list(effective("entry_signals", strategy.entry_signals) or []),
        "exit_signals": list(effective("exit_signals", strategy.exit_signals) or []),
        "stop_loss": effective("stop_loss", strategy.stop_loss),
        "take_profit": effective("take_profit", getattr(strategy, "take_profit", None)),
        "trailing_stop": effective("trailing_stop", getattr(strategy, "trailing_stop", None)),
        "trailing_take_profit_activate": effective(
            "trailing_take_profit_activate",
            getattr(strategy, "trailing_take_profit_activate", None),
        ),
        "trailing_take_profit_drawdown": effective(
            "trailing_take_profit_drawdown",
            getattr(strategy, "trailing_take_profit_drawdown", None),
        ),
        "max_hold_days": effective("max_hold_days", strategy.max_hold_days),
        "webhook_channels": list(preferences.get_webhook_default_channels()),
    }


def _has_exit_rules(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("exit_signals")) or any(
        snapshot.get(key) is not None
        for key in (
            "stop_loss",
            "take_profit",
            "trailing_stop",
            "trailing_take_profit_activate",
            "trailing_take_profit_drawdown",
            "max_hold_days",
        )
    )


@router.get("/settings")
def get_settings(request: Request):
    return _account(request).settings()


@router.put("/settings")
def put_settings(payload: SettingsPatch, request: Request):
    values = payload.model_dump(exclude_none=True)
    try:
        return _account(request).update_settings(values)
    except PortfolioError as exc:
        raise _http_error(exc) from exc


@router.get("/summary")
def get_summary(request: Request):
    account = _account(request)
    prices, _ = _resolve_prices(request, account.open_symbols())
    return account.summary(prices)


@router.get("/positions")
def get_positions(
    request: Request,
    as_of: str | None = Query(default=None),
    include_closed: bool = Query(default=False),
):
    account = _account(request)
    prices, _, quotes = _resolve_quotes(request, account.open_symbols())
    try:
        positions = account.positions(
            prices,
            as_of=as_of,
            include_closed=include_closed,
        )
        for position in positions:
            quote = quotes.get(position["symbol"])
            position.update(quote or {
                "prev_close": None,
                "change_pct": None,
                "change_amount": None,
                "price_source": "missing",
            })
        return {
            "positions": positions,
        }
    except PortfolioError as exc:
        raise _http_error(exc) from exc


@router.get("/transactions")
def get_transactions(request: Request):
    return _account(request).transactions()


@router.post("/cash")
def post_cash(payload: CashRequest, request: Request):
    try:
        flow = _account(request).record_cash(payload.type, payload.amount, note=payload.note)
    except PortfolioError as exc:
        raise _http_error(exc) from exc
    return {"cash_flow": flow}


@router.post("/cash/{flow_id}/reverse")
def reverse_cash(flow_id: str, payload: ReverseCashRequest, request: Request):
    try:
        flow = _account(request).reverse_cash(flow_id, note=payload.note)
    except PortfolioError as exc:
        raise _http_error(exc) from exc
    return {"cash_flow": flow}


@router.post("/buys/preview")
def preview_buys(payload: BuyPreviewRequest, request: Request):
    symbols = [item.symbol.strip().upper() for item in payload.items]
    account = _account(request)
    resolved, sources = _resolve_prices(
        request,
        list(dict.fromkeys([*symbols, *account.open_symbols()])),
    )
    candidates = []
    exit_rules_by_symbol: dict[str, bool] = {}
    for item, symbol in zip(payload.items, symbols, strict=True):
        try:
            snapshot = _strategy_snapshot(request, item.strategy_id, item.score)
        except PortfolioError as exc:
            raise _http_error(exc) from exc
        exit_rules_by_symbol[symbol] = _has_exit_rules(snapshot)
        price = item.price if item.price is not None else resolved.get(symbol)
        candidates.append(
            {
                "symbol": symbol,
                "strategy_id": item.strategy_id,
                "score": item.score,
                "price": price,
            }
        )
    try:
        result = account.preview_buys(candidates, current_prices=resolved)
    except PortfolioError as exc:
        raise _http_error(exc) from exc
    source_by_symbol = {
        symbol: ("manual" if item.price is not None else sources.get(symbol, "missing"))
        for item, symbol in zip(payload.items, symbols, strict=True)
    }
    for item in result["items"]:
        item["price_source"] = source_by_symbol.get(item["symbol"], "missing")
        item["has_exit_rules"] = exit_rules_by_symbol.get(item["symbol"], False)
    return result


@router.post("/buys")
def confirm_buy(payload: BuyRequest, request: Request):
    symbol = payload.symbol.strip().upper()
    if request.app.state.repo.resolve_asset_type(symbol) != "stock":
        raise HTTPException(400, "首版持仓账户仅支持日线 A 股")
    account = _account(request)
    prices, _ = _resolve_prices(
        request,
        list(dict.fromkeys([symbol, *account.open_symbols()])),
    )
    price = payload.price if payload.price is not None else prices.get(symbol)
    if price is None:
        raise HTTPException(400, "实时价和最新收盘价均不可用")
    try:
        snapshot = _strategy_snapshot(request, payload.strategy_id, payload.buy_score)
        name = payload.name.strip()
        if not name:
            name = request.app.state.repo.get_name_map([symbol]).get(symbol, "")
        return account.buy(
            symbol=symbol,
            name=name,
            price=price,
            quantity=payload.quantity,
            strategy_snapshot=snapshot,
            trade_date=payload.trade_date or cn_today().isoformat(),
            current_prices=prices,
            allow_buy_warnings=payload.confirm_buy_warnings,
            add_to_watchlist=lambda value: watchlist.add(
                value,
                "",
                payload.watchlist_group_id,
            ),
        )
    except PortfolioError as exc:
        raise _http_error(exc) from exc


@router.post("/positions/{position_id}/sell")
def confirm_sell(position_id: str, payload: SellRequest, request: Request):
    account = _account(request)
    trade_date = payload.trade_date or cn_today().isoformat()
    try:
        position = account.get_position(position_id)
        quantity = payload.quantity
        if quantity is None:
            position_rows = account.positions(
                {}, as_of=trade_date, include_closed=True
            )
            quantity = next(
                row["available_qty"] for row in position_rows if row["id"] == position_id
            )
        prices, _ = _resolve_prices(request, [position["symbol"]])
        price = payload.price if payload.price is not None else prices.get(position["symbol"])
        if price is None:
            raise PortfolioError("实时价和最新收盘价均不可用")
        return account.sell(
            position_id,
            price=price,
            quantity=quantity,
            trade_date=trade_date,
            remove_from_watchlist=payload.remove_from_watchlist,
            remove_watchlist=watchlist.remove,
        )
    except StopIteration as exc:
        raise _http_error(PortfolioError("持仓批次不存在")) from exc
    except PortfolioError as exc:
        raise _http_error(exc) from exc


@router.post("/positions/{position_id}/continue-holding")
def continue_holding(position_id: str, request: Request):
    try:
        return {"position": _account(request).continue_holding(position_id)}
    except PortfolioError as exc:
        raise _http_error(exc) from exc
