# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentTraderError(Exception):
    error: str
    message: str
    fix: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class NotInitializedError(AgentTraderError):
    def __init__(self):
        super().__init__(
            error="NotInitialized",
            message="agenttrader not initialized. Run: agenttrader init",
            fix="Run: agenttrader init",
        )


class MarketNotCachedError(AgentTraderError):
    def __init__(self, market_id: str):
        super().__init__(
            error="MarketNotCached",
            message=f"Market {market_id} not in cache. Run: agenttrader sync",
            fix=f"Call sync_data(market_ids=['{market_id}']) or run: agenttrader sync --market-id {market_id}",
        )


class StrategyValidationError(AgentTraderError):
    def __init__(self, errors: list[dict[str, Any]], warnings: list[dict[str, Any]]):
        super().__init__(
            error="StrategyValidationError",
            message="Strategy validation failed",
            extra={"errors": errors, "warnings": warnings},
        )
