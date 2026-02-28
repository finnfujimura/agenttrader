# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import operator
import re
import time
from datetime import UTC, datetime

import click
from rich.console import Console
from rich.table import Table

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.data.cache import DataCache
from agenttrader.data.models import Market
from agenttrader.data.parquet_adapter import ParquetDataAdapter
from agenttrader.db import get_engine
from agenttrader.errors import AgentTraderError, MarketNotCachedError


OPERATORS = {
    "<": operator.lt,
    ">": operator.gt,
    "<=": operator.le,
    ">=": operator.ge,
    "==": operator.eq,
}

CONDITION_PATTERN = re.compile(
    r"^(price_vs_7d_avg|current_price|volume|days_until_close|price_change_24h)"
    r"\s*(<=|>=|==|<|>)\s*"
    r"(-?\d+\.?\d*)$"
)


@click.group("markets")
def markets_group() -> None:
    """Read cached market data."""


@markets_group.command("list")
@click.option("--platform", default="all")
@click.option("--category", default=None)
@click.option("--tags", default=None, help="Comma-separated tag filters")
@click.option("--min-volume", type=float, default=None)
@click.option("--limit", type=int, default=100)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def markets_list(platform: str, category: str | None, tags: str | None, min_volume: float | None, limit: int, json_output: bool) -> None:
    ensure_initialized()
    source_name, source = _get_market_source()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    if source_name == "parquet":
        markets = source.get_markets(
            platform=platform,
            category=category,
            min_volume=min_volume,
            limit=limit,
        )
        if tag_list:
            wanted = {t.lower() for t in tag_list}
            markets = [m for m in markets if wanted.issubset({t.lower() for t in m.tags})]
    else:
        markets = source.get_markets(platform=platform, category=category, tags=tag_list, min_volume=min_volume, limit=limit)

    if json_output:
        payload = {
            "ok": True,
            "data_source": source_name,
            "count": len(markets),
            "markets": [
                {
                    "id": m.id,
                    "condition_id": m.condition_id,
                    "platform": m.platform.value,
                    "title": m.title,
                    "category": m.category,
                    "tags": m.tags,
                    "market_type": m.market_type.value,
                    "scalar_low": m.scalar_low,
                    "scalar_high": m.scalar_high,
                    "volume": m.volume,
                    "close_time": m.close_time,
                    "resolved": m.resolved,
                    "resolution": m.resolution,
                }
                for m in markets
            ],
        }
        emit_json(payload)
        return

    table = Table(title="Cached Markets")
    table.add_column("ID")
    table.add_column("Platform")
    table.add_column("Title")
    table.add_column("Category")
    table.add_column("Price")
    table.add_column("Volume")

    if source_name == "parquet":
        click.echo("Data source: Jon Becker dataset (parquet) -- 2021-present")
    else:
        click.echo("Data source: local sync cache (SQLite) -- run 'agenttrader dataset download' for full history")

    now = int(time.time())
    for m in markets:
        if source_name == "parquet":
            points = source.get_price_history(m.id, m.platform, now - 7 * 86400, now)
            latest = points[-1] if points else None
        else:
            latest = source.get_latest_price(m.id)
        price_text = f"{latest.yes_price:.3f}" if latest else "-"
        table.add_row(m.id[:14], m.platform.value, m.title[:60], m.category or "", price_text, f"{m.volume:,.2f}")

    Console().print(table)


@markets_group.command("price")
@click.argument("market_id")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def markets_price(market_id: str, json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())
    market = cache.get_market(market_id)
    if not market:
        raise MarketNotCachedError(market_id)
    latest = cache.get_latest_price(market_id)
    if latest is None:
        raise MarketNotCachedError(market_id)

    payload = {
        "ok": True,
        "market_id": market_id,
        "platform": market.platform.value,
        "timestamp": latest.timestamp,
        "yes_price": latest.yes_price,
        "no_price": latest.no_price,
        "volume": latest.volume,
    }
    if json_output:
        emit_json(payload)
        return
    click.echo(f"{market.title}\nYES: {latest.yes_price:.4f} NO: {latest.no_price if latest.no_price is not None else '-'}")


@markets_group.command("history")
@click.argument("market_id")
@click.option("--days", type=int, default=7)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def markets_history(market_id: str, days: int, json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())
    if not cache.get_market(market_id):
        raise MarketNotCachedError(market_id)
    end_ts = int(time.time())
    start_ts = end_ts - (days * 24 * 3600)
    points = cache.get_price_history(market_id, start_ts, end_ts)

    if json_output:
        emit_json(
            {
                "ok": True,
                "market_id": market_id,
                "days": days,
                "history": [
                    {
                        "timestamp": p.timestamp,
                        "yes_price": p.yes_price,
                        "no_price": p.no_price,
                        "volume": p.volume,
                    }
                    for p in points
                ],
            }
        )
        return

    table = Table(title=f"History: {market_id}")
    table.add_column("Timestamp")
    table.add_column("YES")
    table.add_column("NO")
    table.add_column("Volume")
    for p in points:
        ts = datetime.fromtimestamp(p.timestamp, tz=UTC).strftime("%Y-%m-%d %H:%M")
        table.add_row(ts, f"{p.yes_price:.4f}", f"{p.no_price:.4f}" if p.no_price is not None else "-", f"{p.volume:.2f}")
    Console().print(table)


@markets_group.command("match")
@click.option("--polymarket-slug", default=None)
@click.option("--kalshi-ticker", default=None)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def markets_match(polymarket_slug: str | None, kalshi_ticker: str | None, json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())
    markets = cache.get_markets(limit=5000)

    poly = [m for m in markets if m.platform.value == "polymarket"]
    kalshi = [m for m in markets if m.platform.value == "kalshi"]

    base = None
    counterpart = []
    if polymarket_slug:
        matches = [m for m in poly if polymarket_slug.lower() in (m.id + m.condition_id + m.title).lower()]
        base = matches[0] if matches else None
        if base:
            tokens = {t for t in base.title.lower().split() if len(t) > 3}
            counterpart = [m for m in kalshi if tokens & set(m.title.lower().split())]
    elif kalshi_ticker:
        matches = [m for m in kalshi if kalshi_ticker.lower() in (m.id + m.condition_id + m.title).lower()]
        base = matches[0] if matches else None
        if base:
            tokens = {t for t in base.title.lower().split() if len(t) > 3}
            counterpart = [m for m in poly if tokens & set(m.title.lower().split())]

    matched = []
    if base:
        for m in counterpart[:20]:
            matched.append(
                {
                    "base_market_id": base.id,
                    "base_platform": base.platform.value,
                    "match_market_id": m.id,
                    "match_platform": m.platform.value,
                    "base_title": base.title,
                    "match_title": m.title,
                }
            )

    payload = {"ok": True, "count": len(matched), "matches": matched}
    if json_output:
        emit_json(payload)
    else:
        if not matched:
            click.echo("No matches found in local cache")
            return
        table = Table(title="Cross-platform Matches")
        table.add_column("Base")
        table.add_column("Match")
        table.add_column("Platforms")
        for m in matched:
            table.add_row(m["base_title"][:50], m["match_title"][:50], f"{m['base_platform']} -> {m['match_platform']}")
        Console().print(table)


def _parse_condition(condition_str: str) -> tuple[str, object, float]:
    match = CONDITION_PATTERN.match(str(condition_str).strip())
    if not match:
        raise AgentTraderError(
            "InvalidCondition",
            (
                f"Invalid condition: '{condition_str}'. Supported metrics: "
                "price_vs_7d_avg, current_price, volume, days_until_close, price_change_24h"
            ),
        )
    metric, op_str, value = match.group(1), match.group(2), float(match.group(3))
    return metric, OPERATORS[op_str], value


def _compute_market_metrics(market_id: str, cache: DataCache) -> dict:
    now = int(time.time())
    market = cache.get_market(market_id)
    latest = cache.get_latest_price(market_id)
    history_7d = cache.get_price_history(market_id, now - 7 * 86400, now)
    history_24h = cache.get_price_history(market_id, now - 86400, now)

    current_price = float(latest.yes_price) if latest is not None else None
    avg_7d = (sum(float(p.yes_price) for p in history_7d) / len(history_7d)) if history_7d else None
    oldest_24h = float(history_24h[0].yes_price) if history_24h else None

    return {
        "current_price": current_price,
        "price_vs_7d_avg": (current_price - avg_7d) if (current_price is not None and avg_7d is not None) else None,
        "price_change_24h": (current_price - oldest_24h) if (current_price is not None and oldest_24h is not None) else None,
        "volume": float(market.volume or 0.0) if market else None,
        "days_until_close": ((int(market.close_time) - now) / 86400.0) if market and market.close_time else None,
    }


def _compute_market_metrics_parquet(market: Market, adapter: ParquetDataAdapter) -> dict:
    now = int(time.time())
    history_7d = adapter.get_price_history(market.id, market.platform, now - 7 * 86400, now)
    history_24h = adapter.get_price_history(market.id, market.platform, now - 86400, now)
    current_price = float(history_7d[-1].yes_price) if history_7d else (float(history_24h[-1].yes_price) if history_24h else None)
    avg_7d = (sum(float(p.yes_price) for p in history_7d) / len(history_7d)) if history_7d else None
    oldest_24h = float(history_24h[0].yes_price) if history_24h else None

    return {
        "current_price": current_price,
        "price_vs_7d_avg": (current_price - avg_7d) if (current_price is not None and avg_7d is not None) else None,
        "price_change_24h": (current_price - oldest_24h) if (current_price is not None and oldest_24h is not None) else None,
        "volume": float(market.volume or 0.0),
        "days_until_close": ((int(market.close_time) - now) / 86400.0) if market.close_time else None,
    }


def _get_market_source() -> tuple[str, object]:
    adapter = ParquetDataAdapter()
    if adapter.is_available():
        return "parquet", adapter
    return "sqlite", DataCache(get_engine())


@markets_group.command("screen")
@click.option("--condition", required=True)
@click.option("--platform", default="all")
@click.option("--category", default=None)
@click.option("--min-volume", type=float, default=None)
@click.option("--min-history-days", type=int, default=7)
@click.option("--limit", type=int, default=20)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def markets_screen(
    condition: str,
    platform: str,
    category: str | None,
    min_volume: float | None,
    min_history_days: int,
    limit: int,
    json_output: bool,
) -> None:
    ensure_initialized()
    metric_name, op_fn, threshold = _parse_condition(condition)
    source_name, source = _get_market_source()
    now = int(time.time())

    if source_name == "parquet":
        markets = source.get_markets(platform=platform, category=category, min_volume=min_volume, limit=5000)
    else:
        markets = source.get_markets(platform=platform, category=category, min_volume=min_volume, limit=5000)
    matches = []

    for market in markets:
        if source_name == "parquet":
            history = source.get_price_history(market.id, market.platform, now - max(min_history_days, 1) * 86400, now)
            metrics = _compute_market_metrics_parquet(market, source)
        else:
            history = source.get_price_history(market.id, now - max(min_history_days, 1) * 86400, now)
            metrics = _compute_market_metrics(market.id, source)
        if not history:
            continue

        metric_value = metrics.get(metric_name)
        if metric_value is None:
            continue
        if not op_fn(metric_value, threshold):
            continue

        matches.append(
            {
                "id": market.id,
                "title": market.title,
                "platform": market.platform.value,
                "category": market.category,
                "current_price": metrics["current_price"],
                "price_vs_7d_avg": metrics["price_vs_7d_avg"],
                "price_change_24h": metrics["price_change_24h"],
                "volume": metrics["volume"],
                "days_until_close": metrics["days_until_close"],
            }
        )
        if len(matches) >= limit:
            break

    payload = {"ok": True, "data_source": source_name, "condition": condition, "count": len(matches), "markets": matches}
    if json_output:
        emit_json(payload)
        return

    table = Table(title=f"Market Screener ({condition})")
    table.add_column("Market")
    table.add_column("Platform")
    table.add_column("Category")
    table.add_column("Current")
    table.add_column("vs 7d Avg")
    table.add_column("24h Chg")
    table.add_column("Volume")
    table.add_column("Days to Close")

    for m in matches:
        table.add_row(
            m["title"][:50],
            m["platform"],
            m["category"] or "",
            f"{m['current_price']:.3f}" if m["current_price"] is not None else "-",
            f"{m['price_vs_7d_avg']:.3f}" if m["price_vs_7d_avg"] is not None else "-",
            f"{m['price_change_24h']:.3f}" if m["price_change_24h"] is not None else "-",
            f"{float(m['volume'] or 0.0):,.0f}",
            f"{m['days_until_close']:.2f}" if m["days_until_close"] is not None else "-",
        )
    Console().print(table)
