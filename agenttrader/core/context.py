# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.dialects.sqlite import insert

from agenttrader.data.cache import DataCache
from agenttrader.data.models import ExecutionMode, FillResult, Market, OrderBook, Platform, Position as PositionModel, PricePoint
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_session
from agenttrader.db.schema import PaperPortfolio, Position, Trade
from agenttrader.errors import AgentTraderError, MarketNotCachedError
from agenttrader.core.fill_model import FillModel


_VALID_ORDER_TYPES = ("market", "limit")


def _validate_buy_params(contracts, order_type, limit_price):
    """Shared validation for buy() across all context implementations."""
    if float(contracts) <= 0:
        raise AgentTraderError("InvalidOrder", "contracts must be > 0")
    if order_type not in _VALID_ORDER_TYPES:
        raise AgentTraderError("InvalidOrder", f"order_type must be 'market' or 'limit', got '{order_type}'")
    if order_type == "limit" and limit_price is None:
        raise AgentTraderError("InvalidOrder", "limit_price is required when order_type='limit'")


def _market_matches_category(market: Market, category: str | None) -> bool:
    wanted = str(category or "").strip().lower()
    if not wanted:
        return True
    if str(market.category or "").strip().lower() == wanted:
        return True
    return wanted in {str(tag or "").strip().lower() for tag in (market.tags or [])}


def _cache_get_latest_price(cache: DataCache, market_id: str, platform: str | None = None) -> PricePoint | None:
    try:
        if platform is not None:
            return cache.get_latest_price(market_id, platform=platform)
        return cache.get_latest_price(market_id)
    except TypeError:
        return cache.get_latest_price(market_id)
    except AttributeError:
        return None


class ExecutionContext(ABC):
    @abstractmethod
    def subscribe(self, platform, category, tags, market_ids) -> None: ...

    @abstractmethod
    def search_markets(self, query, platform) -> list[Market]: ...

    @abstractmethod
    def get_price(self, market_id: str) -> float: ...

    @abstractmethod
    def get_orderbook(self, market_id: str) -> OrderBook: ...

    @abstractmethod
    def get_history(self, market_id: str, lookback_hours: int) -> list[PricePoint]: ...

    @abstractmethod
    def get_position(self, market_id: str) -> PositionModel | None: ...

    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_portfolio_value(self) -> float: ...

    @abstractmethod
    def buy(self, market_id, contracts, side, order_type, limit_price) -> str: ...

    @abstractmethod
    def sell(self, market_id, contracts) -> str: ...

    @abstractmethod
    def log(self, message: str) -> None: ...

    @abstractmethod
    def set_state(self, key: str, value) -> None: ...

    @abstractmethod
    def get_state(self, key: str, default=None): ...


class BacktestContext(ExecutionContext):
    def __init__(
        self,
        initial_cash: float,
        price_data: dict[str, list[dict[str, Any] | PricePoint]],
        orderbook_data: dict[str, list[OrderBook]] | None,
        markets: dict[str, Market],
        parquet_adapter=None,
        platform_map: dict[str, Platform] | None = None,
        execution_mode: ExecutionMode = ExecutionMode.STRICT_PRICE_ONLY,
    ):
        self._cash = float(initial_cash)
        self._initial_cash = float(initial_cash)
        self._price_data = {
            market_id: [self._coerce_point(x) for x in points]
            for market_id, points in price_data.items()
        }
        for points in self._price_data.values():
            points.sort(key=lambda p: p.timestamp)
        self._orderbook_data = {
            market_id: sorted(list(orderbooks), key=lambda o: o.timestamp)
            for market_id, orderbooks in (orderbook_data or {}).items()
        }
        self._markets = dict(markets)
        self._parquet_adapter = parquet_adapter
        self._platform_map = dict(platform_map or {})
        self._state: dict[str, Any] = {}
        self._logs: list[dict[str, Any]] = []
        self._subscriptions: set[str] = set()
        self._current_ts = 0
        self._active_market_id: str | None = None  # prevents cross-market look-ahead
        self._fill_model = FillModel()
        self._execution_mode = execution_mode
        if execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            from agenttrader.core.price_fill_model import PriceOnlyFillModel
            self._price_fill_model = PriceOnlyFillModel()
        else:
            self._price_fill_model = None
        self._positions: dict[str, PositionModel] = {}
        self._trades: list[dict[str, Any]] = []
        self._equity_curve: list[dict[str, float | int]] = []
        self._slippage_samples: list[float] = []

    def subscribe(self, platform="all", category=None, tags=None, market_ids=None) -> None:
        if market_ids:
            self._subscriptions.update(market_ids)
            return
        for market_id, market in self._markets.items():
            if platform != "all" and market.platform.value != platform:
                continue
            if not _market_matches_category(market, category):
                continue
            if tags and not set(tags).issubset(set(market.tags)):
                continue
            self._subscriptions.add(market_id)

    def search_markets(self, query, platform="all") -> list[Market]:
        q = query.lower()
        out = []
        for market in self._markets.values():
            if platform != "all" and market.platform.value != platform:
                continue
            if q in market.title.lower():
                out.append(market)
        return out

    def get_price(self, market_id: str) -> float:
        points = self._price_data.get(market_id, [])
        # Prevent cross-market look-ahead bias: for the active market use <=,
        # for other markets use < so they only see already-announced prices.
        if self._active_market_id is not None and market_id != self._active_market_id:
            historical = [p for p in points if p.timestamp < self._current_ts]
        else:
            historical = [p for p in points if p.timestamp <= self._current_ts]
        if not historical:
            raise MarketNotCachedError(market_id)
        return historical[-1].yes_price

    def get_orderbook(self, market_id: str) -> OrderBook:
        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            raise AgentTraderError(
                "NoObservedOrderbook",
                f"No observed orderbook data for {market_id}. "
                "In strict_price_only mode, orderbook access is not available. "
                "Use execution_mode='synthetic_execution_model' to enable synthetic orderbooks.",
            )
        if self._execution_mode == ExecutionMode.OBSERVED_ORDERBOOK:
            books = self._orderbook_data.get(market_id, [])
            before = [o for o in books if o.timestamp <= self._current_ts]
            if before:
                return before[-1]
            raise AgentTraderError(
                "NoObservedOrderbook",
                f"No observed orderbook history for {market_id}.",
            )
        # SYNTHETIC_EXECUTION_MODEL
        books = self._orderbook_data.get(market_id, [])
        before = [o for o in books if o.timestamp <= self._current_ts]
        if before:
            return before[-1]
        price = self.get_price(market_id)
        spread = 0.01
        from agenttrader.data.models import OrderLevel
        return OrderBook(
            market_id=market_id,
            timestamp=self._current_ts,
            bids=[OrderLevel(price=max(0.0, price - spread), size=1_000_000.0)],
            asks=[OrderLevel(price=min(1.0, price + spread), size=1_000_000.0)],
        )

    def get_history(self, market_id: str, lookback_hours: int = 24) -> list[PricePoint]:
        cutoff = self._current_ts - int(lookback_hours * 3600)
        points = self._price_data.get(market_id, [])
        if self._active_market_id is not None and market_id != self._active_market_id:
            return [p for p in points if cutoff <= p.timestamp < self._current_ts]
        return [p for p in points if cutoff <= p.timestamp <= self._current_ts]

    def get_position(self, market_id: str) -> PositionModel | None:
        return self._positions.get(market_id)

    def get_cash(self) -> float:
        return self._cash

    def get_portfolio_value(self) -> float:
        value = self._cash
        for pos in self._positions.values():
            try:
                mark = self.get_price(pos.market_id)
            except AgentTraderError:
                mark = pos.avg_cost
            value += pos.contracts * mark
        return value

    def buy(self, market_id, contracts, side="yes", order_type="market", limit_price=None) -> str:
        _validate_buy_params(contracts, order_type, limit_price)
        contracts = float(contracts)

        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            observed_price = self.get_price(market_id)
            fill = self._price_fill_model.fill_buy(
                contracts, observed_price,
                limit_price=limit_price if order_type == "limit" else None,
            )
        else:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_buy(contracts, ob, order_type=order_type, limit_price=limit_price)

        if not fill.filled or fill.contracts <= 0:
            raise AgentTraderError("OrderNotFilled", f"Buy not filled for market {market_id}")

        cost = fill.contracts * fill.fill_price
        if self._cash < cost:
            raise AgentTraderError("InsufficientCashError", f"Insufficient cash for buy: need {cost:.2f}")
        self._cash -= cost

        current = self._positions.get(market_id)
        if current is None:
            current = PositionModel(
                id=str(uuid.uuid4()),
                market_id=market_id,
                platform=self._markets.get(market_id, Market(market_id, market_id, Platform.POLYMARKET, market_id, "", [], self._guess_market_type(market_id), 0.0, 0, False, None, None, None)).platform,
                side=side,
                contracts=fill.contracts,
                avg_cost=fill.fill_price,
                opened_at=self._current_ts,
            )
        else:
            total_contracts = current.contracts + fill.contracts
            current.avg_cost = ((current.avg_cost * current.contracts) + cost) / total_contracts
            current.contracts = total_contracts
        self._positions[market_id] = current

        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": self._markets.get(market_id).title if market_id in self._markets else market_id,
                "action": "buy",
                "side": side,
                "contracts": fill.contracts,
                "price": fill.fill_price,
                "slippage": fill.slippage,
                "filled_at": self._current_ts,
                "pnl": None,
                "resolved_correctly": None,
            }
        )
        self._slippage_samples.append(fill.slippage)
        return trade_id

    def sell(self, market_id, contracts=None) -> str:
        position = self._positions.get(market_id)
        if not position:
            raise AgentTraderError("NoPositionError", f"No open position for market {market_id}")
        contracts_to_sell = float(contracts if contracts is not None else position.contracts)
        contracts_to_sell = min(contracts_to_sell, position.contracts)
        if contracts_to_sell <= 0:
            raise AgentTraderError("InvalidOrder", "contracts must be > 0")

        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            observed_price = self.get_price(market_id)
            fill = self._price_fill_model.fill_sell(contracts_to_sell, observed_price)
        else:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_sell(contracts_to_sell, ob)

        if not fill.filled:
            raise AgentTraderError("OrderNotFilled", f"Sell not filled for market {market_id}")

        proceeds = fill.contracts * fill.fill_price
        cost_basis = fill.contracts * position.avg_cost
        pnl = proceeds - cost_basis
        self._cash += proceeds

        position.contracts -= fill.contracts
        if position.contracts <= 0:
            self._positions.pop(market_id, None)
        else:
            self._positions[market_id] = position

        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": self._markets.get(market_id).title if market_id in self._markets else market_id,
                "action": "sell",
                "side": position.side,
                "contracts": fill.contracts,
                "price": fill.fill_price,
                "slippage": fill.slippage,
                "filled_at": self._current_ts,
                "pnl": pnl,
            }
        )
        self._slippage_samples.append(fill.slippage)
        return trade_id

    def log(self, message: str) -> None:
        self._logs.append({"timestamp": self._current_ts, "message": message})

    def set_state(self, key: str, value) -> None:
        self._state[key] = value

    def get_state(self, key: str, default=None):
        return self._state.get(key, default)

    def advance_time(self, ts: int) -> None:
        if ts < self._current_ts:
            raise AgentTraderError("TimeOrderViolation", f"Time must advance: {ts} < {self._current_ts}")
        self._current_ts = ts

    def set_active_market(self, market_id: str | None) -> None:
        """Set the currently processing market to prevent cross-market look-ahead bias."""
        self._active_market_id = market_id

    def record_snapshot(self) -> None:
        self._equity_curve.append({"timestamp": self._current_ts, "value": self.get_portfolio_value()})

    def settle_positions(self, market_id: str, outcome: str) -> float:
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0
        payout_price = 1.0 if outcome == pos.side else 0.0
        pnl = pos.contracts * (payout_price - pos.avg_cost)
        self._cash += pos.contracts * payout_price
        resolved_correctly = pnl > 0
        for trade in self._trades:
            if trade.get("action") != "buy":
                continue
            if trade.get("market_id") != market_id:
                continue
            if trade.get("side") != pos.side:
                continue
            if trade.get("resolved_correctly") is None:
                trade["resolved_correctly"] = resolved_correctly

        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": self._markets.get(market_id).title if market_id in self._markets else market_id,
                "action": "resolution",
                "side": pos.side,
                "contracts": pos.contracts,
                "price": payout_price,
                "slippage": 0.0,
                "filled_at": self._current_ts,
                "pnl": pnl,
            }
        )
        self._positions.pop(market_id, None)
        return pnl

    def compile_results(self) -> dict:
        return {
            "initial_cash": self._initial_cash,
            "final_value": self.get_portfolio_value(),
            "equity_curve": self._equity_curve,
            "trades": self._trades,
            "logs": self._logs,
            "avg_slippage": (sum(self._slippage_samples) / len(self._slippage_samples)) if self._slippage_samples else 0.0,
            "execution_mode": self._execution_mode.value,
        }

    @staticmethod
    def _coerce_point(item: dict[str, Any] | PricePoint) -> PricePoint:
        if isinstance(item, PricePoint):
            return item
        return PricePoint(
            timestamp=int(item["timestamp"]),
            yes_price=float(item["yes_price"]),
            no_price=float(item["no_price"]) if item.get("no_price") is not None else None,
            volume=float(item.get("volume", 0.0)),
        )

    def _guess_market_type(self, market_id: str):
        from agenttrader.data.models import MarketType

        market = self._markets.get(market_id)
        return market.market_type if market else MarketType.BINARY


class StreamingBacktestContext(ExecutionContext):
    """
    ExecutionContext for the streaming backtest engine.
    Uses O(1) price cursors and rolling history buffers.
    """

    def __init__(
        self,
        initial_cash: float,
        market_map: dict[str, Market],
        fill_model: FillModel,
        history_buffer_hours: int = 168,
        execution_mode: ExecutionMode = ExecutionMode.STRICT_PRICE_ONLY,
    ):
        self._cash = float(initial_cash)
        self._initial_cash = float(initial_cash)
        self._market_map = dict(market_map)
        self._fill_model = fill_model
        self._current_ts = 0
        self._history_buffer_hours = int(history_buffer_hours)
        self._active_market_id: str | None = None
        self._portfolio_changed = False
        self._execution_mode = execution_mode
        if execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            from agenttrader.core.price_fill_model import PriceOnlyFillModel
            self._price_fill_model = PriceOnlyFillModel()
        else:
            self._price_fill_model = None

        # O(1) price lookup
        self._price_cursors: dict[str, float] = {}
        self._price_cursor_ts: dict[str, int] = {}

        # Rolling history buffer per market (~5-min avg frequency)
        max_buffer = max(self._history_buffer_hours * 12, 1)
        self._history: dict[str, deque[PricePoint]] = defaultdict(lambda: deque(maxlen=max_buffer))

        self._positions: dict[str, PositionModel] = {}
        self._trades: list[dict[str, Any]] = []
        self._equity_curve: list[dict[str, float | int]] = []
        self._slippage_samples: list[float] = []
        self._logs: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {}
        self._subscriptions: set[str] = set()

    # --- Time and cursor management (called by engine) ---

    def advance_time(self, ts: int) -> None:
        if ts < self._current_ts:
            raise AgentTraderError("TimeOrderViolation", f"Time must advance: {ts} < {self._current_ts}")
        self._current_ts = ts

    def set_active_market(self, market_id: str | None) -> None:
        self._active_market_id = market_id

    def set_price_cursor(self, market_id: str, price: float) -> None:
        self._price_cursors[market_id] = float(price)
        self._price_cursor_ts[market_id] = int(self._current_ts)

    def push_history(self, market_id: str, point: PricePoint) -> None:
        self._history[market_id].append(point)

    def get_market(self, market_id: str) -> Market | None:
        return self._market_map.get(market_id)

    def portfolio_changed_since_last_check(self) -> bool:
        changed = self._portfolio_changed
        self._portfolio_changed = False
        return changed

    def record_snapshot(self, ts: int | None = None) -> None:
        if ts is not None:
            self._current_ts = int(ts)
        self._equity_curve.append({"timestamp": self._current_ts, "value": self.get_portfolio_value()})

    # --- ExecutionContext interface (called by strategy) ---

    def subscribe(self, platform="all", category=None, tags=None, market_ids=None) -> None:
        # No-op: streaming engine resolves subscriptions ahead of runtime.
        return None

    def search_markets(self, query, platform="all") -> list[Market]:
        q = str(query or "").lower()
        return [
            m
            for m in self._market_map.values()
            if q in m.title.lower() and (platform == "all" or m.platform.value == platform)
        ]

    def get_price(self, market_id: str) -> float:
        if market_id not in self._price_cursors:
            raise MarketNotCachedError(market_id)

        # Prevent cross-market look-ahead on same-timestamp events.
        if self._active_market_id is not None and market_id != self._active_market_id:
            cursor_ts = self._price_cursor_ts.get(market_id)
            if cursor_ts is None or cursor_ts >= self._current_ts:
                for point in reversed(self._history[market_id]):
                    if point.timestamp < self._current_ts:
                        return point.yes_price
                raise MarketNotCachedError(market_id)

        return self._price_cursors[market_id]

    def get_history(self, market_id: str, lookback_hours: int = 24) -> list[PricePoint]:
        cutoff = self._current_ts - int(lookback_hours * 3600)
        if self._active_market_id is not None and market_id != self._active_market_id:
            return [p for p in self._history[market_id] if cutoff <= p.timestamp < self._current_ts]
        return [p for p in self._history[market_id] if cutoff <= p.timestamp <= self._current_ts]

    def get_orderbook(self, market_id: str) -> OrderBook:
        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            raise AgentTraderError(
                "NoObservedOrderbook",
                f"No observed orderbook data for {market_id}. "
                "In strict_price_only mode, orderbook access is not available. "
                "Use execution_mode='synthetic_execution_model' to enable synthetic orderbooks.",
            )
        if self._execution_mode == ExecutionMode.OBSERVED_ORDERBOOK:
            raise AgentTraderError(
                "NoObservedOrderbook",
                f"No observed orderbook history for {market_id}. "
                "Historical orderbook data must be collected via PMXT sync before use.",
            )
        # SYNTHETIC_EXECUTION_MODEL: existing behavior
        price = self._price_cursors.get(market_id, 0.5)
        return self._synthesize_orderbook(price, market_id)

    def get_position(self, market_id: str) -> PositionModel | None:
        return self._positions.get(market_id)

    def get_cash(self) -> float:
        return self._cash

    def get_portfolio_value(self) -> float:
        value = self._cash
        for pos in self._positions.values():
            try:
                mark = self.get_price(pos.market_id)
            except AgentTraderError:
                mark = pos.avg_cost
            value += pos.contracts * mark
        return value

    def buy(self, market_id, contracts, side="yes", order_type="market", limit_price=None) -> str:
        _validate_buy_params(contracts, order_type, limit_price)
        contracts = float(contracts)

        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            observed_price = self._price_cursors.get(market_id)
            if observed_price is None:
                raise MarketNotCachedError(market_id)
            fill = self._price_fill_model.fill_buy(
                contracts, observed_price,
                limit_price=limit_price if order_type == "limit" else None,
            )
        else:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_buy(contracts, ob, order_type=order_type, limit_price=limit_price)

        if not fill.filled or fill.contracts <= 0:
            raise AgentTraderError("OrderNotFilled", f"Buy not filled for market {market_id}")

        cost = fill.contracts * fill.fill_price
        if self._cash < cost:
            raise AgentTraderError("InsufficientCashError", f"Insufficient cash for buy: need {cost:.2f}")
        self._cash -= cost

        market = self._market_map.get(market_id)
        if market is None:
            raise MarketNotCachedError(market_id)

        current = self._positions.get(market_id)
        if current is None:
            current = PositionModel(
                id=str(uuid.uuid4()),
                market_id=market_id,
                platform=market.platform,
                side=side,
                contracts=fill.contracts,
                avg_cost=fill.fill_price,
                opened_at=self._current_ts,
            )
        else:
            total_contracts = current.contracts + fill.contracts
            current.avg_cost = ((current.avg_cost * current.contracts) + cost) / total_contracts
            current.contracts = total_contracts
        self._positions[market_id] = current

        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": market.title,
                "action": "buy",
                "side": side,
                "contracts": fill.contracts,
                "price": fill.fill_price,
                "slippage": fill.slippage,
                "filled_at": self._current_ts,
                "pnl": None,
                "resolved_correctly": None,
            }
        )
        self._slippage_samples.append(fill.slippage)
        self._portfolio_changed = True
        return trade_id

    def sell(self, market_id, contracts=None) -> str:
        position = self._positions.get(market_id)
        if not position:
            raise AgentTraderError("NoPositionError", f"No open position for market {market_id}")
        contracts_to_sell = float(contracts if contracts is not None else position.contracts)
        contracts_to_sell = min(contracts_to_sell, position.contracts)
        if contracts_to_sell <= 0:
            raise AgentTraderError("InvalidOrder", "contracts must be > 0")

        if self._execution_mode == ExecutionMode.STRICT_PRICE_ONLY:
            observed_price = self._price_cursors.get(market_id)
            if observed_price is None:
                raise MarketNotCachedError(market_id)
            fill = self._price_fill_model.fill_sell(contracts_to_sell, observed_price)
        else:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_sell(contracts_to_sell, ob)

        if not fill.filled:
            raise AgentTraderError("OrderNotFilled", f"Sell not filled for market {market_id}")

        proceeds = fill.contracts * fill.fill_price
        cost_basis = fill.contracts * position.avg_cost
        pnl = proceeds - cost_basis
        self._cash += proceeds

        position.contracts -= fill.contracts
        if position.contracts <= 0:
            self._positions.pop(market_id, None)
        else:
            self._positions[market_id] = position

        market = self._market_map.get(market_id)
        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": market.title if market else market_id,
                "action": "sell",
                "side": position.side,
                "contracts": fill.contracts,
                "price": fill.fill_price,
                "slippage": fill.slippage,
                "filled_at": self._current_ts,
                "pnl": pnl,
            }
        )
        self._slippage_samples.append(fill.slippage)
        self._portfolio_changed = True
        return trade_id

    def settle_positions(self, market_id: str, outcome: str) -> float:
        pos = self._positions.get(market_id)
        if not pos:
            return 0.0
        payout_price = 1.0 if outcome == pos.side else 0.0
        pnl = pos.contracts * (payout_price - pos.avg_cost)
        self._cash += pos.contracts * payout_price
        resolved_correctly = pnl > 0
        for trade in self._trades:
            if trade.get("action") != "buy":
                continue
            if trade.get("market_id") != market_id:
                continue
            if trade.get("side") != pos.side:
                continue
            if trade.get("resolved_correctly") is None:
                trade["resolved_correctly"] = resolved_correctly

        market = self._market_map.get(market_id)
        trade_id = str(uuid.uuid4())
        self._trades.append(
            {
                "id": trade_id,
                "market_id": market_id,
                "market_title": market.title if market else market_id,
                "action": "resolution",
                "side": pos.side,
                "contracts": pos.contracts,
                "price": payout_price,
                "slippage": 0.0,
                "filled_at": self._current_ts,
                "pnl": pnl,
            }
        )
        self._positions.pop(market_id, None)
        self._portfolio_changed = True
        return pnl

    def log(self, message: str) -> None:
        self._logs.append({"timestamp": self._current_ts, "message": message})

    def set_state(self, key: str, value) -> None:
        self._state[key] = value

    def get_state(self, key: str, default=None):
        return self._state.get(key, default)

    def compile_results(self) -> dict:
        return {
            "initial_cash": self._initial_cash,
            "final_value": self.get_portfolio_value(),
            "equity_curve": self._equity_curve,
            "trades": self._trades,
            "logs": self._logs,
            "avg_slippage": (sum(self._slippage_samples) / len(self._slippage_samples)) if self._slippage_samples else 0.0,
            "execution_mode": self._execution_mode.value,
        }

    def _synthesize_orderbook(self, price: float, market_id: str) -> OrderBook:
        from agenttrader.data.models import OrderLevel

        spread = 0.01
        return OrderBook(
            market_id=market_id,
            timestamp=self._current_ts,
            bids=[OrderLevel(price=max(0.0, price - spread), size=1_000_000.0)],
            asks=[OrderLevel(price=min(1.0, price + spread), size=1_000_000.0)],
        )


class LiveContext(ExecutionContext):
    def __init__(
        self,
        portfolio_id: str,
        initial_cash: float,
        cache: DataCache,
        ob_store: OrderBookStore,
        pmxt_client=None,
        history_buffer_hours: int = 24,
        poll_interval_seconds: int = 5,
    ):
        self._portfolio_id = portfolio_id
        self._cash = float(initial_cash)
        self._cache = cache
        self._ob_store = ob_store
        self._pmxt_client = pmxt_client
        self._fill_model = FillModel()
        self._state: dict[str, Any] = {}
        self._logs: list[str] = []
        self._subscriptions: dict[str, Market] = {}
        self._positions: dict[str, PositionModel] = {}
        self._current_prices: dict[str, float] = {}
        self._live_orderbooks: dict[str, OrderBook] = {}
        self._last_persisted_ts: dict[str, int] = {}
        self._live_status: dict[str, dict[str, Any]] = {}
        self._history_buffer_hours = max(int(history_buffer_hours), 1)
        # Size buffer by expected data points, not seconds.
        # At 5s polling (default), 24h = 17,280 points.
        poll_interval = max(int(poll_interval_seconds), 1)
        max_buffer = max(self._history_buffer_hours * 3600 // poll_interval, 1)
        self._history: dict[str, deque[PricePoint]] = defaultdict(lambda: deque(maxlen=max_buffer))

    def subscribe(self, platform="all", category=None, tags=None, market_ids=None) -> None:
        live_markets: list[Market] = []
        if self._pmxt_client:
            try:
                live_markets = self._pmxt_client.get_markets(
                    platform=platform,
                    category=category,
                    tags=tags,
                    market_ids=list(market_ids) if market_ids else None,
                    resolved=False,
                    limit=1000,
                )
            except Exception:
                live_markets = []

        for market in live_markets:
            self._cache.upsert_market(market)

        cache_markets: list[Market] = []
        if market_ids:
            for market_id in market_ids:
                market = self._cache.get_market(str(market_id))
                if market is None:
                    continue
                if platform != "all" and market.platform.value != platform:
                    continue
                if not _market_matches_category(market, category):
                    continue
                if tags and not set(tags).issubset(set(market.tags)):
                    continue
                cache_markets.append(market)
        else:
            cache_markets = self._cache.get_markets(platform=platform, category=category, tags=tags, limit=1000)

        combined = {market.id: market for market in cache_markets}
        for market in live_markets:
            combined[market.id] = market
        self._subscriptions = combined

    def search_markets(self, query, platform="all") -> list[Market]:
        if self._pmxt_client:
            try:
                markets = self._pmxt_client.search_markets(str(query or ""), platform=platform, limit=100)
            except Exception:
                markets = []
            else:
                for market in markets:
                    self._cache.upsert_market(market)
                if markets:
                    return markets
        return self._cache.search_markets(query, platform)

    def get_price(self, market_id: str) -> float:
        if market_id in self._current_prices:
            return self._current_prices[market_id]
        market = self._subscriptions.get(market_id) or self._cache.get_market(market_id)
        platform = market.platform.value if market is not None else None
        latest = _cache_get_latest_price(self._cache, market_id, platform=platform)
        if not latest:
            raise MarketNotCachedError(market_id)
        return latest.yes_price

    def set_live_price(self, market_id: str, price: float) -> None:
        self._current_prices[market_id] = float(price)

    def get_orderbook(self, market_id: str) -> OrderBook:
        if market_id in self._live_orderbooks:
            return self._live_orderbooks[market_id]
        market = self._cache.get_market(market_id)
        if not market:
            raise MarketNotCachedError(market_id)
        ob = self._ob_store.get_nearest(market.platform.value, market_id, int(datetime.now(tz=UTC).timestamp()))
        if not ob:
            raise AgentTraderError(
                "NoObservedOrderbook",
                f"No observed orderbook snapshot for {market_id}.",
            )
        return ob

    def get_history(self, market_id: str, lookback_hours: int = 24) -> list[PricePoint]:
        now = int(datetime.now(tz=UTC).timestamp())
        start = now - int(lookback_hours * 3600)
        combined: dict[int, PricePoint] = {
            point.timestamp: point
            for point in self._cache.get_price_history(market_id, start, now)
        }
        for point in self._history.get(market_id, []):
            if start <= point.timestamp <= now:
                combined[point.timestamp] = point
        return [combined[ts] for ts in sorted(combined)]

    def get_position(self, market_id: str) -> PositionModel | None:
        return self._positions.get(market_id)

    def get_cash(self) -> float:
        return self._cash

    def get_portfolio_value(self) -> float:
        value = self._cash
        for pos in self._positions.values():
            try:
                mark = self.get_price(pos.market_id)
            except AgentTraderError:
                mark = pos.avg_cost
            value += pos.contracts * mark
        return value

    def refresh_market_live(
        self,
        market: Market,
        persist_interval_seconds: int = 60,
        force_persist: bool = False,
    ) -> dict[str, Any]:
        if not self._pmxt_client:
            return self._mark_live_failure(market.id, "Live PMXT client is not configured.")

        self._cache.upsert_market(market)
        snapshot = self._pmxt_client.get_live_snapshot(market.id, market.platform)
        snapshot_status = str(snapshot.get("status") or "error")
        fallback_error = str(snapshot.get("error") or "Live snapshot failed.")
        orderbook = snapshot.get("orderbook")
        if orderbook is not None:
            self._live_orderbooks[market.id] = orderbook

        point = snapshot.get("price")
        used_cached_price = False
        if point is None:
            point = _cache_get_latest_price(self._cache, market.id, platform=market.platform.value)
            if point is not None:
                used_cached_price = True
                snapshot_status = "degraded"
                if snapshot.get("error"):
                    fallback_error = f"{fallback_error} Using cached price history fallback."
                else:
                    fallback_error = "Live snapshot returned no price. Using cached price history fallback."
            else:
                if snapshot_status == "error":
                    return self._mark_live_failure(market.id, fallback_error)
                return self._mark_live_failure(
                    market.id,
                    "Live snapshot returned no price and no cached price history is available.",
                )

        updated = False
        if point is not None:
            previous_price = self._current_prices.get(market.id)
            self._current_prices[market.id] = float(point.yes_price)
            history = self._history[market.id]
            if history and history[-1].timestamp == point.timestamp:
                history[-1] = point
            elif not history or history[-1].timestamp < point.timestamp:
                history.append(point)
            updated = previous_price is None or abs(previous_price - point.yes_price) > 1e-12

        persisted = False
        if (
            point is not None
            and not used_cached_price
            and (force_persist or self._should_persist_point(market.id, point.timestamp, persist_interval_seconds))
        ):
            self._cache.upsert_price_point(market.id, market.platform.value, point)
            if orderbook is not None:
                self._ob_store.write(market.platform.value, market.id, [orderbook])
            self._cache.mark_market_synced(market.id, int(point.timestamp))
            self._last_persisted_ts[market.id] = int(point.timestamp)
            persisted = True

        previous = self._live_status.get(market.id, {})
        prev_failures = int(previous.get("consecutive_failures", 0))
        if used_cached_price:
            new_failures = prev_failures + 1
        else:
            # Decrement toward 0 on success so recovery requires sustained stability
            new_failures = max(prev_failures - 1, 0)
        self._live_status[market.id] = {
            "market_id": market.id,
            "status": snapshot_status,
            "last_live_update_ts": point.timestamp if point is not None else None,
            "last_orderbook_ts": orderbook.timestamp if orderbook is not None else None,
            "last_error": fallback_error if used_cached_price else None,
            "consecutive_failures": new_failures,
            "degraded": snapshot_status != "ok" or new_failures > 0,
            "has_live_price": point is not None,
            "has_live_orderbook": orderbook is not None,
            "current_price": point.yes_price if point is not None else self._current_prices.get(market.id),
        }
        return {
            "market_id": market.id,
            "status": snapshot_status,
            "price": point,
            "orderbook": orderbook,
            "updated": updated,
            "persisted": persisted,
            "error": fallback_error if used_cached_price else None,
        }

    def get_live_status(self) -> dict[str, dict[str, Any]]:
        return {market_id: dict(state) for market_id, state in self._live_status.items()}

    def buy(self, market_id, contracts, side="yes", order_type="market", limit_price=None) -> str:
        _validate_buy_params(contracts, order_type, limit_price)
        contracts = float(contracts)
        try:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_buy(contracts, ob, order_type=order_type, limit_price=limit_price)
        except AgentTraderError:
            # Fall back to mid-price when no live orderbook is available yet
            price = self._current_prices.get(market_id)
            if price is None:
                raise MarketNotCachedError(market_id)
            fill = FillResult(filled=True, fill_price=price, contracts=float(contracts), slippage=0.0, partial=False)
        if not fill.filled or fill.contracts <= 0:
            raise AgentTraderError("OrderNotFilled", f"Buy not filled for market {market_id}")

        cost = fill.contracts * fill.fill_price
        if cost > self._cash:
            raise AgentTraderError("InsufficientCashError", f"Insufficient cash for buy: need {cost:.2f}")
        self._cash -= cost

        market = self._cache.get_market(market_id)
        if not market:
            raise MarketNotCachedError(market_id)

        pos = self._positions.get(market_id)
        if not pos:
            pos = PositionModel(
                id=str(uuid.uuid4()),
                market_id=market_id,
                platform=market.platform,
                side=side,
                contracts=fill.contracts,
                avg_cost=fill.fill_price,
                opened_at=int(datetime.now(tz=UTC).timestamp()),
            )
        else:
            total = pos.contracts + fill.contracts
            pos.avg_cost = ((pos.avg_cost * pos.contracts) + cost) / total
            pos.contracts = total
        self._positions[market_id] = pos

        trade_id = str(uuid.uuid4())
        now_ts = int(datetime.now(tz=UTC).timestamp())
        with self._cache._engine.begin() as conn:
            conn.execute(
                insert(Trade).values(
                    id=trade_id,
                    portfolio_id=self._portfolio_id,
                    market_id=market_id,
                    platform=market.platform.value,
                    action="buy",
                    side=side,
                    contracts=fill.contracts,
                    price=fill.fill_price,
                    slippage=fill.slippage,
                    filled_at=now_ts,
                    pnl=None,
                )
            )
            conn.execute(
                insert(Position)
                .values(
                    id=pos.id,
                    portfolio_id=self._portfolio_id,
                    market_id=market_id,
                    platform=market.platform.value,
                    side=pos.side,
                    contracts=pos.contracts,
                    avg_cost=pos.avg_cost,
                    opened_at=pos.opened_at,
                    closed_at=None,
                    realized_pnl=None,
                )
                .on_conflict_do_update(
                    index_elements=[Position.id],
                    set_={
                        "contracts": pos.contracts,
                        "avg_cost": pos.avg_cost,
                        "closed_at": None,
                        "realized_pnl": None,
                    },
                )
            )
            conn.execute(
                PaperPortfolio.__table__.update()
                .where(PaperPortfolio.id == self._portfolio_id)
                .values(cash_balance=self._cash)
            )

        return trade_id

    def sell(self, market_id, contracts=None) -> str:
        pos = self._positions.get(market_id)
        if not pos:
            raise AgentTraderError("NoPositionError", f"No open position for market {market_id}")

        qty = min(float(contracts if contracts is not None else pos.contracts), pos.contracts)
        try:
            ob = self.get_orderbook(market_id)
            fill = self._fill_model.simulate_sell(qty, ob)
        except AgentTraderError:
            # Fall back to mid-price when no live orderbook is available yet
            price = self._current_prices.get(market_id)
            if price is None:
                raise MarketNotCachedError(market_id)
            fill = FillResult(filled=True, fill_price=price, contracts=float(qty), slippage=0.0, partial=False)
        if not fill.filled or fill.contracts <= 0:
            raise AgentTraderError("OrderNotFilled", f"Sell not filled for market {market_id}")

        proceeds = fill.contracts * fill.fill_price
        cost_basis = fill.contracts * pos.avg_cost
        pnl = proceeds - cost_basis
        self._cash += proceeds

        pos.contracts -= fill.contracts
        if pos.contracts <= 0:
            self._positions.pop(market_id, None)

        trade_id = str(uuid.uuid4())
        market = self._cache.get_market(market_id)
        if not market:
            raise MarketNotCachedError(market_id)
        now_ts = int(datetime.now(tz=UTC).timestamp())

        with self._cache._engine.begin() as conn:
            conn.execute(
                insert(Trade).values(
                    id=trade_id,
                    portfolio_id=self._portfolio_id,
                    market_id=market_id,
                    platform=market.platform.value,
                    action="sell",
                    side=pos.side,
                    contracts=fill.contracts,
                    price=fill.fill_price,
                    slippage=fill.slippage,
                    filled_at=now_ts,
                    pnl=pnl,
                )
            )
            if market_id in self._positions:
                conn.execute(
                    Position.__table__.update()
                    .where(Position.id == self._positions[market_id].id)
                    .values(contracts=self._positions[market_id].contracts, avg_cost=self._positions[market_id].avg_cost)
                )
            else:
                conn.execute(
                    Position.__table__.update()
                    .where(and_(Position.portfolio_id == self._portfolio_id, Position.market_id == market_id, Position.closed_at.is_(None)))
                    .values(closed_at=now_ts, realized_pnl=pnl)
                )

            conn.execute(
                PaperPortfolio.__table__.update()
                .where(PaperPortfolio.id == self._portfolio_id)
                .values(cash_balance=self._cash)
            )

        return trade_id

    def log(self, message: str) -> None:
        now_ts = int(datetime.now(tz=UTC).timestamp())
        self._logs.append(message)
        self._cache.append_log(self._portfolio_id, now_ts, message)

    def set_state(self, key: str, value) -> None:
        self._state[key] = value

    def get_state(self, key: str, default=None):
        return self._state.get(key, default)

    def load_positions_from_db(self) -> None:
        q = select(Position).where(and_(Position.portfolio_id == self._portfolio_id, Position.closed_at.is_(None)))
        with get_session(self._cache._engine) as session:
            rows = list(session.scalars(q).all())
        self._positions = {
            row.market_id: PositionModel(
                id=row.id,
                market_id=row.market_id,
                platform=Platform(row.platform),
                side=row.side,
                contracts=float(row.contracts),
                avg_cost=float(row.avg_cost),
                opened_at=int(row.opened_at),
            )
            for row in rows
        }

    def _should_persist_point(self, market_id: str, timestamp: int, persist_interval_seconds: int) -> bool:
        last_persisted = self._last_persisted_ts.get(market_id)
        if last_persisted is None:
            return True
        return int(timestamp) - int(last_persisted) >= max(int(persist_interval_seconds), 1)

    def _mark_live_failure(self, market_id: str, error: str) -> dict[str, Any]:
        previous = self._live_status.get(market_id, {})
        failures = int(previous.get("consecutive_failures", 0)) + 1
        self._live_status[market_id] = {
            "market_id": market_id,
            "status": "error",
            "last_live_update_ts": previous.get("last_live_update_ts"),
            "last_orderbook_ts": previous.get("last_orderbook_ts"),
            "last_error": error,
            "consecutive_failures": failures,
            "degraded": True,
            "has_live_price": market_id in self._current_prices,
            "has_live_orderbook": market_id in self._live_orderbooks,
            "current_price": self._current_prices.get(market_id),
        }
        return {
            "market_id": market_id,
            "status": "error",
            "price": None,
            "orderbook": None,
            "updated": False,
            "persisted": False,
            "error": error,
        }

    @property
    def subscriptions(self) -> dict[str, Market]:
        return self._subscriptions
