from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from agenttrader.data.models import Market, MarketType, OrderBook, OrderLevel, Platform, PricePoint


COMMON_CATEGORIES = {
    "politics",
    "sports",
    "crypto",
    "business",
    "world",
    "technology",
    "science",
    "entertainment",
    "economics",
}


class PmxtClient:
    def __init__(self) -> None:
        try:
            import pmxt  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "pmxt is not installed. Install with: pip install pmxt. "
                "Node.js is also required by pmxt's local sidecar."
            ) from exc

        self._poly = pmxt.Polymarket()
        self._kalshi = pmxt.Kalshi()

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
        if market_ids:
            return self._get_markets_by_ids(
                platform=platform,
                category=category,
                tags=tags,
                market_ids=market_ids,
                min_volume=min_volume,
            )

        results_by_id: dict[tuple[str, str], Market] = {}
        wanted_tags = {t.lower() for t in tags} if tags else None
        wanted_category = category.strip().lower() if category else None
        if resolved:
            status = "closed"
        else:
            status = "active"

        if platform in ("all", "polymarket"):
            for item in self._poly.fetch_markets(status=status, limit=limit):
                market = self._to_market(item, Platform.POLYMARKET, status_hint=status)
                if not self._matches_market_filters(market, wanted_category, wanted_tags, min_volume):
                    continue
                results_by_id[(market.platform.value, market.id)] = market

        if platform in ("all", "kalshi"):
            for item in self._kalshi.fetch_markets(status=status, limit=limit):
                market = self._to_market(item, Platform.KALSHI, status_hint=status)
                if not self._matches_market_filters(market, wanted_category, wanted_tags, min_volume):
                    continue
                results_by_id[(market.platform.value, market.id)] = market

        results = sorted(results_by_id.values(), key=lambda m: m.volume, reverse=True)
        return results[:limit]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def search_markets(
        self,
        query: str,
        platform: str = "all",
        limit: int = 100,
    ) -> list[Market]:
        results_by_id: dict[tuple[str, str], Market] = {}
        q = str(query or "").strip()
        if not q:
            return []

        if platform in ("all", "polymarket"):
            for item in self._poly.fetch_markets(query=q, status="all", limit=limit):
                market = self._to_market(item, Platform.POLYMARKET, status_hint="all")
                results_by_id[(market.platform.value, market.id)] = market

        if platform in ("all", "kalshi"):
            for item in self._kalshi.fetch_markets(query=q, status="all", limit=limit):
                market = self._to_market(item, Platform.KALSHI, status_hint="all")
                results_by_id[(market.platform.value, market.id)] = market

        results = sorted(results_by_id.values(), key=lambda m: m.volume, reverse=True)
        return results[:limit]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def _fetch_order_book_with_retry(self, client: Any, outcome_id: str) -> Any:
        return client.fetch_order_book(outcome_id)

    def get_live_snapshot(
        self,
        outcome_id: str,
        platform: Platform,
    ) -> dict[str, Any]:
        client = self._poly if platform == Platform.POLYMARKET else self._kalshi
        timestamp = int(time.time())
        try:
            book = self._fetch_order_book_with_retry(client, outcome_id)
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "timestamp": timestamp,
                "price": None,
                "orderbook": None,
            }

        bids = [
            OrderLevel(
                price=self._safe_float(getattr(level, "price", None), 0.0),
                size=self._safe_float(getattr(level, "size", None), 0.0),
            )
            for level in (getattr(book, "bids", None) or [])
        ]
        asks = [
            OrderLevel(
                price=self._safe_float(getattr(level, "price", None), 0.0),
                size=self._safe_float(getattr(level, "size", None), 0.0),
            )
            for level in (getattr(book, "asks", None) or [])
        ]
        orderbook = OrderBook(
            market_id=outcome_id,
            timestamp=timestamp,
            bids=bids,
            asks=asks,
        )

        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        yes_price = None
        if best_bid is not None and best_ask is not None:
            yes_price = (best_bid + best_ask) / 2.0
        elif best_bid is not None:
            yes_price = best_bid
        elif best_ask is not None:
            yes_price = best_ask

        price = None
        if yes_price is not None:
            clipped = max(0.0, min(1.0, float(yes_price)))
            price = PricePoint(
                timestamp=timestamp,
                yes_price=clipped,
                no_price=max(0.0, min(1.0, 1.0 - clipped)),
                volume=0.0,
            )

        status = "ok" if price is not None else "empty"
        return {
            "status": status,
            "error": None,
            "timestamp": timestamp,
            "price": price,
            "orderbook": orderbook,
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_market_price(
        self,
        market_id: str,
        platform: Platform,
        at_time: int | None = None,
    ) -> PricePoint:
        # pmxt market prices are in 0.0 - 1.0 probability units.
        _ = at_time
        client = self._poly if platform == Platform.POLYMARKET else self._kalshi
        book = client.fetch_order_book(market_id)
        yes_price = 0.0
        best_bid = book.bids[0].price if getattr(book, "bids", None) else None
        best_ask = book.asks[0].price if getattr(book, "asks", None) else None
        if best_bid is not None and best_ask is not None:
            yes_price = (float(best_bid) + float(best_ask)) / 2.0
        elif best_bid is not None:
            yes_price = float(best_bid)
        elif best_ask is not None:
            yes_price = float(best_ask)

        ts = int(time.time())
        return PricePoint(
            timestamp=ts,
            yes_price=max(0.0, min(1.0, yes_price)),
            no_price=max(0.0, min(1.0, 1.0 - yes_price)),
            volume=0.0,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_outcome_side(
        self,
        outcome_id: str,
        platform: Platform,
    ) -> str | None:
        """Return the canonical side label ('yes'/'no') for an outcome when PMXT exposes it."""
        client = self._poly if platform == Platform.POLYMARKET else self._kalshi
        if not hasattr(client, "fetch_market"):
            return None

        market = client.fetch_market(outcome_id=outcome_id)
        yes = getattr(market, "yes", None)
        if yes is not None and str(getattr(yes, "outcome_id", "")) == str(outcome_id):
            return "yes"
        no = getattr(market, "no", None)
        if no is not None and str(getattr(no, "outcome_id", "")) == str(outcome_id):
            return "no"

        outcomes = getattr(market, "outcomes", None) or []
        for outcome in outcomes:
            if str(getattr(outcome, "outcome_id", "")) != str(outcome_id):
                continue
            label = self._normalize_outcome_label(getattr(outcome, "label", None))
            if label in {"yes", "no"}:
                return label
            return None

        return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_candlesticks(
        self,
        condition_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        interval: int = 60,
    ) -> list[PricePoint]:
        return self.get_candlesticks_with_status(condition_id, platform, start_time, end_time, interval)["points"]

    # Maximum seconds per OHLCV chunk — 7 days keeps requests small enough for PMXT.
    _OHLCV_CHUNK_SECONDS = 7 * 24 * 3600

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_candlesticks_with_status(
        self,
        condition_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        interval: int = 60,
    ) -> dict[str, Any]:
        if end_time <= start_time:
            return {"points": [], "status": "empty", "error": None}

        span = end_time - start_time
        if span <= self._OHLCV_CHUNK_SECONDS:
            return self._fetch_ohlcv_chunk(condition_id, platform, start_time, end_time, interval)

        # Split into chunks, merge, deduplicate
        all_points: list[PricePoint] = []
        chunk_errors: list[str] = []
        chunk_start = start_time
        while chunk_start < end_time:
            chunk_end = min(chunk_start + self._OHLCV_CHUNK_SECONDS, end_time)
            result = self._fetch_ohlcv_chunk(condition_id, platform, chunk_start, chunk_end, interval)
            all_points.extend(result["points"])
            if result["status"] == "error" and result["error"]:
                chunk_errors.append(result["error"])
            chunk_start = chunk_end

        # Deduplicate by timestamp (keep last seen for each ts)
        seen: dict[int, PricePoint] = {}
        for p in all_points:
            seen[p.timestamp] = p
        points = sorted(seen.values(), key=lambda p: p.timestamp)

        if not points and chunk_errors:
            return {"points": [], "status": "error", "error": "; ".join(chunk_errors)}
        status = "ok" if points else "empty"
        return {"points": points, "status": status, "error": None}

    def _fetch_ohlcv_chunk(
        self,
        condition_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        interval: int = 60,
    ) -> dict[str, Any]:
        """Fetch a single OHLCV chunk (no further splitting)."""
        client = self._poly if platform == Platform.POLYMARKET else self._kalshi
        resolution = self._resolution_from_interval(interval)
        start_dt = datetime.fromtimestamp(int(start_time), tz=UTC)
        end_dt = datetime.fromtimestamp(int(end_time), tz=UTC)

        step_seconds = max(60, int(interval) * 60)
        approx_points = max(1, int((end_time - start_time) / step_seconds) + 2)
        limit = min(max(approx_points, 50), 10_000)

        try:
            candles = client.fetch_ohlcv(
                condition_id,
                resolution=resolution,
                start=start_dt,
                end=end_dt,
                limit=limit,
            )
        except Exception as exc:
            return {"points": [], "status": "error", "error": str(exc)}

        points: list[PricePoint] = []
        for candle in candles or []:
            ts = int(getattr(candle, "timestamp", 0) or 0)
            if ts > 10_000_000_000:
                ts //= 1000
            close = self._safe_float(getattr(candle, "close", None), None)
            if close is None:
                continue
            yes_price = max(0.0, min(1.0, close))
            points.append(
                PricePoint(
                    timestamp=ts,
                    yes_price=yes_price,
                    no_price=max(0.0, 1.0 - yes_price),
                    volume=self._safe_float(getattr(candle, "volume", 0.0), 0.0),
                )
            )

        points.sort(key=lambda p: p.timestamp)
        status = "ok" if points else "empty"
        return {"points": points, "status": status, "error": None}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_orderbook_snapshots(
        self,
        market_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        limit: int = 100,
    ) -> list[OrderBook]:
        return self.get_orderbook_snapshots_with_status(market_id, platform, start_time, end_time, limit)["snapshots"]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_orderbook_snapshots_with_status(
        self,
        market_id: str,
        platform: Platform,
        start_time: int,
        end_time: int,
        limit: int = 100,
    ) -> dict[str, Any]:
        # pmxt provides current live book snapshots rather than historical snapshots.
        _ = (start_time, end_time, limit)
        snapshot = self.get_live_snapshot(market_id, platform)
        if snapshot["status"] == "error":
            return {"snapshots": [], "status": "error", "error": snapshot["error"]}
        orderbook = snapshot["orderbook"]
        if orderbook is None:
            return {"snapshots": [], "status": "empty", "error": None}
        return {"snapshots": [orderbook], "status": snapshot["status"], "error": None}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_matching_markets(
        self,
        polymarket_slug: str | None = None,
        kalshi_ticker: str | None = None,
    ) -> list[dict[str, Any]]:
        if not polymarket_slug and not kalshi_ticker:
            return []

        out: list[dict[str, Any]] = []
        if polymarket_slug:
            base = self._poly.fetch_markets(query=polymarket_slug, status="all", limit=1)
            if not base:
                return []
            base_market = base[0]
            tokens = {t for t in str(getattr(base_market, "title", "")).lower().split() if len(t) > 3}
            candidates = self._kalshi.fetch_markets(query=getattr(base_market, "title", ""), status="all", limit=50)
            for candidate in candidates:
                title_tokens = set(str(getattr(candidate, "title", "")).lower().split())
                if tokens and not (tokens & title_tokens):
                    continue
                out.append(
                    {
                        "event_key": str(getattr(base_market, "market_id", "")),
                        "polymarket_market_id": str(getattr(base_market, "market_id", "")),
                        "kalshi_market_id": str(getattr(candidate, "market_id", "")),
                        "polymarket_title": str(getattr(base_market, "title", "")),
                        "kalshi_title": str(getattr(candidate, "title", "")),
                    }
                )
        else:
            base = self._kalshi.fetch_markets(query=kalshi_ticker, status="all", limit=1)
            if not base:
                return []
            base_market = base[0]
            tokens = {t for t in str(getattr(base_market, "title", "")).lower().split() if len(t) > 3}
            candidates = self._poly.fetch_markets(query=getattr(base_market, "title", ""), status="all", limit=50)
            for candidate in candidates:
                title_tokens = set(str(getattr(candidate, "title", "")).lower().split())
                if tokens and not (tokens & title_tokens):
                    continue
                out.append(
                    {
                        "event_key": str(getattr(base_market, "market_id", "")),
                        "kalshi_market_id": str(getattr(base_market, "market_id", "")),
                        "polymarket_market_id": str(getattr(candidate, "market_id", "")),
                        "kalshi_title": str(getattr(base_market, "title", "")),
                        "polymarket_title": str(getattr(candidate, "title", "")),
                    }
                )
        return out

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_matching_markets_by_sport(self, sport: str, date: str) -> list[dict[str, Any]]:
        _ = (sport, date)
        # pmxt doesn't expose Dome-style sports matching endpoint.
        return []

    def _get_markets_by_ids(
        self,
        platform: str,
        category: str | None,
        tags: list[str] | None,
        market_ids: list[str],
        min_volume: float | None,
    ) -> list[Market]:
        wanted_ids = [str(market_id).strip() for market_id in market_ids if str(market_id).strip()]
        if not wanted_ids:
            return []

        wanted_category = category.strip().lower() if category else None
        wanted_tags = {t.lower() for t in tags} if tags else None
        query_limit = max(len(wanted_ids), 20)
        results: list[Market] = []
        seen: set[tuple[str, str]] = set()

        if platform in ("all", "polymarket"):
            self._append_markets_by_ids(
                backend=self._poly,
                platform=Platform.POLYMARKET,
                wanted_ids=wanted_ids,
                wanted_category=wanted_category,
                wanted_tags=wanted_tags,
                min_volume=min_volume,
                query_limit=query_limit,
                results=results,
                seen=seen,
            )

        if platform in ("all", "kalshi"):
            self._append_markets_by_ids(
                backend=self._kalshi,
                platform=Platform.KALSHI,
                wanted_ids=wanted_ids,
                wanted_category=wanted_category,
                wanted_tags=wanted_tags,
                min_volume=min_volume,
                query_limit=query_limit,
                results=results,
                seen=seen,
            )

        return results

    def _append_markets_by_ids(
        self,
        backend: Any,
        platform: Platform,
        wanted_ids: list[str],
        wanted_category: str | None,
        wanted_tags: set[str] | None,
        min_volume: float | None,
        query_limit: int,
        results: list[Market],
        seen: set[tuple[str, str]],
    ) -> None:
        for wanted_id in wanted_ids:
            normalized_wanted_id = wanted_id.lower()
            for item in backend.fetch_markets(query=wanted_id, status="all", limit=query_limit):
                market = self._to_market(item, platform, status_hint="all")
                if not self._matches_market_filters(market, wanted_category, wanted_tags, min_volume):
                    continue
                aliases = self._market_identifier_aliases(item, market)
                if normalized_wanted_id not in aliases:
                    continue
                key = (market.platform.value, market.id)
                if key in seen:
                    continue
                seen.add(key)
                results.append(market)

    def _to_market(self, item: Any, platform: Platform, status_hint: str) -> Market:
        raw_category = str(getattr(item, "category", "") or "")
        tags = [str(t) for t in (getattr(item, "tags", None) or [])]
        normalized_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
        clean_raw_category = raw_category.strip()
        if clean_raw_category and clean_raw_category.lower() not in normalized_tags:
            tags.append(clean_raw_category)

        primary_outcome = self._primary_outcome(item)
        market_id = str(getattr(primary_outcome, "outcome_id", None) or getattr(item, "market_id", ""))
        category = self._canonical_category(raw_category, tags)
        close_time = self._to_unix_seconds(getattr(item, "resolution_date", None))
        resolved = status_hint == "closed"
        resolution = self._infer_resolution(item) if resolved else None

        return Market(
            id=market_id,
            condition_id=market_id,
            platform=platform,
            title=str(getattr(item, "title", getattr(item, "question", market_id))),
            category=category,
            tags=tags,
            market_type=MarketType.BINARY,
            volume=self._safe_float(getattr(item, "volume", None), 0.0),
            close_time=close_time,
            resolved=resolved,
            resolution=resolution,
            scalar_low=None,
            scalar_high=None,
        )

    @staticmethod
    def _primary_outcome(item: Any) -> Any:
        yes = getattr(item, "yes", None)
        if yes is not None and getattr(yes, "outcome_id", None):
            return yes
        outcomes = getattr(item, "outcomes", None) or []
        labeled_yes = PmxtClient._find_outcome_by_label(outcomes, "yes")
        if labeled_yes is not None:
            return labeled_yes
        if outcomes:
            return outcomes[0]
        return item

    @staticmethod
    def _normalize_outcome_label(value: Any) -> str:
        return str(value or "").strip().lower()

    @staticmethod
    def _find_outcome_by_label(outcomes: list[Any], wanted_label: str) -> Any | None:
        wanted = PmxtClient._normalize_outcome_label(wanted_label)
        for outcome in outcomes or []:
            label = PmxtClient._normalize_outcome_label(getattr(outcome, "label", None))
            if label == wanted and getattr(outcome, "outcome_id", None):
                return outcome
        return None

    @staticmethod
    def _infer_resolution(item: Any) -> str | None:
        yes = getattr(item, "yes", None)
        no = getattr(item, "no", None)
        if yes is not None and no is not None:
            yes_price = PmxtClient._safe_float(getattr(yes, "price", None), None)
            no_price = PmxtClient._safe_float(getattr(no, "price", None), None)
            if yes_price is not None and no_price is not None:
                winner = yes if yes_price >= no_price else no
                label = getattr(winner, "label", None)
                return PmxtClient._normalize_outcome_label(label) or None

        outcomes = getattr(item, "outcomes", None) or []
        best_label = None
        best_price = None
        for outcome in outcomes:
            price = PmxtClient._safe_float(getattr(outcome, "price", None), None)
            if price is None:
                continue
            if best_price is None or price > best_price:
                best_price = price
                best_label = getattr(outcome, "label", None)
        normalized = PmxtClient._normalize_outcome_label(best_label)
        return normalized or None

    @staticmethod
    def _canonical_category(raw_category: str, tags: list[str]) -> str:
        tag_set = {str(t).strip().lower() for t in tags if str(t).strip()}
        for candidate in COMMON_CATEGORIES:
            if candidate in tag_set:
                return candidate
        clean = raw_category.strip().lower()
        if clean:
            return clean
        if tags:
            return str(tags[0]).strip().lower()
        return ""

    @staticmethod
    def _matches_category(market: Market, category: str) -> bool:
        wanted = str(category).strip().lower()
        if not wanted:
            return True
        if str(market.category or "").lower() == wanted:
            return True
        return wanted in {str(tag).lower() for tag in market.tags}

    @staticmethod
    def _matches_market_filters(
        market: Market,
        wanted_category: str | None,
        wanted_tags: set[str] | None,
        min_volume: float | None,
    ) -> bool:
        if wanted_category and not PmxtClient._matches_category(market, wanted_category):
            return False
        if wanted_tags and not wanted_tags.issubset({t.lower() for t in market.tags}):
            return False
        if min_volume is not None and market.volume < float(min_volume):
            return False
        return True

    @staticmethod
    def _market_identifier_aliases(item: Any, market: Market) -> set[str]:
        aliases = {
            str(value).strip().lower()
            for value in (
                market.id,
                market.condition_id,
                PmxtClient._field(item, "ticker"),
                PmxtClient._field(item, "market_id", "marketId", "id"),
                PmxtClient._field(item, "condition_id", "conditionId"),
            )
            if value is not None and str(value).strip()
        }

        primary_outcome = PmxtClient._primary_outcome(item)
        for value in (
            PmxtClient._field(primary_outcome, "outcome_id", "outcomeId", "id"),
        ):
            if value is not None and str(value).strip():
                aliases.add(str(value).strip().lower())

        return aliases

    @staticmethod
    def _field(item: Any, *names: str) -> Any:
        if item is None:
            return None
        if isinstance(item, dict):
            for name in names:
                if name in item:
                    return item[name]
            return None
        for name in names:
            value = getattr(item, name, None)
            if value is not None:
                return value
        return None

    @staticmethod
    def _to_unix_seconds(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            ts = int(value)
            if ts > 10_000_000_000:
                ts //= 1000
            return ts
        if isinstance(value, datetime):
            return int(value.timestamp())
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return 0
            try:
                return int(float(raw))
            except ValueError:
                pass
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return int(dt.timestamp())
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _resolution_from_interval(interval_minutes: int) -> str:
        mapping = {
            1: "1m",
            5: "5m",
            15: "15m",
            60: "1h",
            360: "6h",
            1440: "1d",
        }
        if interval_minutes in mapping:
            return mapping[interval_minutes]
        if interval_minutes >= 1440:
            return "1d"
        if interval_minutes >= 360:
            return "6h"
        if interval_minutes >= 60:
            return "1h"
        if interval_minutes >= 15:
            return "15m"
        if interval_minutes >= 5:
            return "5m"
        return "1m"

    @staticmethod
    def _safe_float(value: Any, default: float | None) -> float | None:
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
