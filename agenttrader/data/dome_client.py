from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agenttrader.data.models import Market, MarketType, OrderBook, OrderLevel, Platform, PricePoint

# This is the ONLY file that may import dome_api_sdk.
from dome_api_sdk import DomeClient as _DomeSDK  # type: ignore


class DomeClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("dome_api_key not set. Run: agenttrader config set dome_api_key <key>")
        self._sdk = _DomeSDK({"api_key": api_key})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_markets(
        self,
        platform: str = "all",
        category: str | None = None,
        tags: list[str] | None = None,
        market_ids: list[str] | None = None,
        resolved: bool = False,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        results_by_id: dict[str, Market] = {}
        wanted_tags = {t.lower() for t in tags} if tags else None

        if platform in ("all", "polymarket"):
            statuses = ["closed"] if resolved else ["open"]
            for status in statuses:
                params: dict[str, Any] = {"status": status, "limit": limit}
                if tags:
                    params["tags"] = tags
                if min_volume is not None:
                    params["min_volume"] = min_volume
                if market_ids and len(market_ids) == 1:
                    params["token_id"] = market_ids[0]

                poly_markets = self._fetch_paginated(
                    self._sdk.polymarket.markets.get_markets,
                    params,
                    items_attr="markets",
                    max_items=limit,
                )
                for item in poly_markets:
                    market = self._to_market(item, Platform.POLYMARKET)
                    if resolved and not market.resolved:
                        continue
                    if category and not self._matches_category(market, category):
                        continue
                    if wanted_tags and not wanted_tags.issubset({t.lower() for t in market.tags}):
                        continue
                    results_by_id[market.id] = market

        if platform in ("all", "kalshi"):
            statuses = ["finalized"] if resolved else ["open"]
            for status in statuses:
                params = {"status": status, "limit": limit}
                if min_volume is not None:
                    params["min_volume"] = min_volume
                if market_ids and len(market_ids) == 1:
                    params["market_ticker"] = market_ids[0]

                kalshi_markets = self._fetch_paginated(
                    self._sdk.kalshi.markets.get_markets,
                    params,
                    items_attr="markets",
                    max_items=limit,
                )
                for item in kalshi_markets:
                    market = self._to_market(item, Platform.KALSHI)
                    if resolved and not market.resolved:
                        continue
                    if category and not self._matches_category(market, category):
                        continue
                    if wanted_tags and not wanted_tags.issubset({t.lower() for t in market.tags}):
                        continue
                    results_by_id[market.id] = market

        results = sorted(results_by_id.values(), key=lambda m: m.volume, reverse=True)
        if market_ids:
            wanted = set(market_ids)
            results = [m for m in results if m.id in wanted]

        return results[:limit]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_market_price(
        self,
        market_id: str,
        platform: Platform,
        at_time: int | None = None,
    ) -> PricePoint:
        now_ts = int(time.time())
        if platform == Platform.POLYMARKET:
            params: dict[str, Any] = {"token_id": market_id}
            if at_time is not None:
                params["at_time"] = at_time
            data = self._sdk.polymarket.markets.get_market_price(params)
            yes_price = float(getattr(data, "price", 0.0))
            ts = int(getattr(data, "at_time", now_ts))
            return PricePoint(timestamp=ts, yes_price=yes_price, no_price=max(0.0, 1.0 - yes_price), volume=0.0)

        params = {"market_ticker": market_id}
        if at_time is not None:
            params["at_time"] = at_time
        data = self._sdk.kalshi.markets.get_market_price(params)
        yes = getattr(data, "yes", None)
        no = getattr(data, "no", None)
        yes_price = float(getattr(yes, "price", 0.0))
        no_price = float(getattr(no, "price", max(0.0, 1.0 - yes_price)))
        ts = int(getattr(yes, "at_time", now_ts))
        return PricePoint(timestamp=ts, yes_price=yes_price, no_price=no_price, volume=0.0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_candlesticks(
        self,
        condition_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        interval: int = 60,
    ) -> list[PricePoint]:
        if platform == Platform.POLYMARKET:
            resp = self._sdk.polymarket.markets.get_candlesticks(
                {
                    "condition_id": condition_id,
                    "start_time": start_time,
                    "end_time": end_time,
                    "interval": interval,
                }
            )

            points: list[PricePoint] = []
            for series in getattr(resp, "candlesticks", []) or []:
                for entry in series:
                    if not hasattr(entry, "end_period_ts"):
                        continue
                    price_obj = getattr(entry, "price", {}) or {}
                    yes_price = self._safe_float(price_obj.get("close_dollars"), None)
                    if yes_price is None:
                        yes_price = self._safe_float(price_obj.get("close"), 0.0)
                        if yes_price > 1.0:
                            yes_price = yes_price / 100.0
                    yes_price = min(max(float(yes_price), 0.0), 1.0)
                    ts = int(getattr(entry, "end_period_ts", 0))
                    if ts > 10_000_000_000:
                        ts //= 1000
                    points.append(
                        PricePoint(
                            timestamp=ts,
                            yes_price=yes_price,
                            no_price=max(0.0, 1.0 - yes_price),
                            volume=float(getattr(entry, "volume", 0.0) or 0.0),
                        )
                    )
                if points:
                    break

            points.sort(key=lambda x: x.timestamp)
            return points

        trades = self._fetch_paginated(
            self._sdk.kalshi.markets.get_trades,
            {
                "ticker": condition_id,
                "start_time": start_time,
                "end_time": end_time,
                "limit": 1000,
            },
            items_attr="trades",
            max_items=5000,
        )

        bucket_seconds = max(60, int(interval) * 60)
        by_bucket: dict[int, dict[str, float]] = defaultdict(lambda: {"price": 0.0, "volume": 0.0, "ts": 0.0})
        for trade in trades:
            ts = int(getattr(trade, "created_time", 0) or 0)
            if ts <= 0:
                continue
            bucket = ts - (ts % bucket_seconds)
            yes_price = self._safe_float(getattr(trade, "yes_price_dollars", None), None)
            if yes_price is None:
                yes_price = self._safe_float(getattr(trade, "yes_price", 0.0), 0.0)
                if yes_price > 1.0:
                    yes_price = yes_price / 100.0
            by_bucket[bucket]["price"] = float(yes_price)
            by_bucket[bucket]["volume"] += float(getattr(trade, "count", 0.0) or 0.0)
            by_bucket[bucket]["ts"] = float(ts)

        points = [
            PricePoint(
                timestamp=bucket,
                yes_price=min(max(v["price"], 0.0), 1.0),
                no_price=max(0.0, 1.0 - min(max(v["price"], 0.0), 1.0)),
                volume=v["volume"],
            )
            for bucket, v in sorted(by_bucket.items())
        ]
        return points

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_orderbook_snapshots(
        self,
        market_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        limit: int = 100,
    ) -> list[OrderBook]:
        start_ms = int(start_time * 1000)
        end_ms = int(end_time * 1000)

        if platform == Platform.POLYMARKET:
            snapshots = self._fetch_paginated(
                self._sdk.polymarket.markets.get_orderbooks,
                {
                    "token_id": market_id,
                    "start_time": start_ms,
                    "end_time": end_ms,
                    "limit": limit,
                },
                items_attr="snapshots",
                max_items=max(1000, limit * 20),
            )

            out: list[OrderBook] = []
            for snap in snapshots:
                asks_raw = getattr(snap, "asks", []) or []
                bids_raw = getattr(snap, "bids", []) or []
                asks = [
                    OrderLevel(
                        price=self._safe_float(level.get("price"), 0.0),
                        size=self._safe_float(level.get("size"), 0.0),
                    )
                    for level in asks_raw
                    if isinstance(level, dict)
                ]
                bids = [
                    OrderLevel(
                        price=self._safe_float(level.get("price"), 0.0),
                        size=self._safe_float(level.get("size"), 0.0),
                    )
                    for level in bids_raw
                    if isinstance(level, dict)
                ]
                ts = int(self._safe_float(getattr(snap, "timestamp", int(time.time())), int(time.time())))
                if ts > 10_000_000_000:
                    ts //= 1000
                out.append(OrderBook(market_id=market_id, timestamp=ts, bids=bids, asks=asks))

            out.sort(key=lambda x: x.timestamp)
            return out

        # Kalshi orderbook endpoint in SDK currently throws parsing errors on some payloads.
        try:
            snapshots = self._fetch_paginated(
                self._sdk.kalshi.markets.get_orderbooks,
                {
                    "ticker": market_id,
                    "start_time": start_ms,
                    "end_time": end_ms,
                    "limit": limit,
                },
                items_attr="snapshots",
                max_items=max(1000, limit * 20),
            )
        except Exception:
            return []

        out: list[OrderBook] = []
        for snap in snapshots:
            ts = int(self._safe_float(getattr(snap, "timestamp", int(time.time())), int(time.time())))
            if ts > 10_000_000_000:
                ts //= 1000
            out.append(OrderBook(market_id=market_id, timestamp=ts, bids=[], asks=[]))
        out.sort(key=lambda x: x.timestamp)
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_matching_markets(
        self,
        polymarket_slug: str | None = None,
        kalshi_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if polymarket_slug:
            params["polymarket_market_slug"] = polymarket_slug
        if kalshi_ticker:
            params["kalshi_event_ticker"] = kalshi_ticker
        resp = self._sdk.matching_markets.get_matching_markets(params)

        out: list[dict[str, Any]] = []
        markets_map = getattr(resp, "markets", {}) or {}
        for key, values in markets_map.items():
            for value in values:
                out.append({"event_key": key, **self._to_dict(value)})
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_matching_markets_by_sport(self, sport: str, date: str) -> list[dict[str, Any]]:
        resp = self._sdk.matching_markets.get_matching_markets_by_sport({"sport": sport, "date": date})
        out: list[dict[str, Any]] = []
        markets_map = getattr(resp, "markets", {}) or {}
        for key, values in markets_map.items():
            for value in values:
                out.append({"event_key": key, **self._to_dict(value)})
        return out

    def _fetch_paginated(
        self,
        fn,
        params: dict[str, Any],
        items_attr: str,
        max_items: int | None = None,
    ) -> list[Any]:
        out: list[Any] = []
        pagination_key: str | None = None
        seen_keys: set[str] = set()
        pages = 0

        while True:
            pages += 1
            if pages > 1000:
                break
            call_params = dict(params)
            if pagination_key:
                call_params["pagination_key"] = pagination_key

            response = fn(call_params)
            items = getattr(response, items_attr, None)
            if items is None and isinstance(response, dict):
                items = response.get(items_attr, [])
            out.extend(list(items or []))
            if max_items is not None and len(out) >= max_items:
                return out[:max_items]

            pagination = getattr(response, "pagination", None)
            if pagination is None and isinstance(response, dict):
                pagination = response.get("pagination")

            has_more = False
            next_key = None
            if pagination is not None:
                has_more = bool(getattr(pagination, "has_more", False))
                next_key = getattr(pagination, "pagination_key", None)
                if isinstance(pagination, dict):
                    has_more = bool(pagination.get("has_more", has_more))
                    next_key = pagination.get("pagination_key", next_key)

            if has_more and next_key:
                if next_key in seen_keys:
                    break
                seen_keys.add(next_key)
                pagination_key = next_key
                continue
            break

        return out

    def _to_market(self, item: Any, platform: Platform) -> Market:
        status = str(getattr(item, "status", "open")).lower()
        resolved = self._is_resolved_status(platform, status)
        if platform == Platform.POLYMARKET:
            tags = [str(t) for t in (getattr(item, "tags", []) or [])]
            category = tags[0].lower() if tags else ""
            resolution = self._extract_resolution(item, platform)
            market_id = str(getattr(getattr(item, "side_a", None), "id", getattr(item, "market_slug", "")))
            return Market(
                id=market_id,
                condition_id=str(getattr(item, "condition_id", market_id)),
                platform=platform,
                title=str(getattr(item, "title", market_id)),
                category=category,
                tags=tags,
                market_type=MarketType.BINARY,
                volume=float(getattr(item, "volume_total", 0.0) or 0.0),
                close_time=int(getattr(item, "close_time", None) or getattr(item, "end_time", 0) or 0),
                resolved=resolved,
                resolution=resolution,
                scalar_low=None,
                scalar_high=None,
            )

        title = str(getattr(item, "title", getattr(item, "market_ticker", "")))
        return Market(
            id=str(getattr(item, "market_ticker", "")),
            condition_id=str(getattr(item, "event_ticker", getattr(item, "market_ticker", ""))),
            platform=platform,
            title=title,
            category=(title.split(" ")[0].lower() if title else ""),
            tags=[],
            market_type=MarketType.BINARY,
            volume=float(getattr(item, "volume", 0.0) or 0.0),
            close_time=int(getattr(item, "close_time", None) or getattr(item, "end_time", 0) or 0),
            resolved=resolved,
            resolution=self._extract_resolution(item, platform),
            scalar_low=None,
            scalar_high=None,
        )

    @staticmethod
    def _is_resolved_status(platform: Platform, status: str) -> bool:
        status = str(status or "").lower()
        if platform == Platform.POLYMARKET:
            return status in {"resolved", "closed", "settled", "finalized"}
        return status in {"finalized", "resolved", "settled", "closed"}

    @staticmethod
    def _extract_resolution(item: Any, platform: Platform) -> str | None:
        if platform == Platform.POLYMARKET:
            winning_side = getattr(item, "winning_side", None)
            if winning_side is not None:
                label = getattr(winning_side, "label", None)
                if label is not None:
                    return str(label).lower()
            for attr in ("result", "resolution", "winning_outcome", "outcome"):
                value = getattr(item, attr, None)
                if value is not None and str(value).strip() != "":
                    return str(value).lower()
            return None

        for attr in ("result", "resolution", "winning_outcome", "outcome"):
            value = getattr(item, attr, None)
            if value is not None and str(value).strip() != "":
                return str(value).lower()
        return None

    @staticmethod
    def _matches_category(market: Market, category: str) -> bool:
        wanted = str(category).strip().lower()
        if not wanted:
            return True
        if str(market.category or "").lower() == wanted:
            return True
        return wanted in {str(tag).lower() for tag in market.tags}

    @staticmethod
    def _to_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "__dict__"):
            out: dict[str, Any] = {}
            for k, v in value.__dict__.items():
                if hasattr(v, "__dict__"):
                    out[k] = v.__dict__
                elif isinstance(v, list):
                    out[k] = [x.__dict__ if hasattr(x, "__dict__") else x for x in v]
                else:
                    out[k] = v
            return out
        return {"value": str(value)}

    @staticmethod
    def _safe_float(value: Any, default: Any) -> Any:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
