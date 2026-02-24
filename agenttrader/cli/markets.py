# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import time
from datetime import UTC, datetime

import click
from rich.console import Console
from rich.table import Table

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.data.cache import DataCache
from agenttrader.db import get_engine
from agenttrader.errors import MarketNotCachedError


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
    cache = DataCache(get_engine())
    tag_list = [t.strip() for t in tags.split(",")] if tags else None
    markets = cache.get_markets(platform=platform, category=category, tags=tag_list, min_volume=min_volume, limit=limit)

    if json_output:
        payload = {
            "ok": True,
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

    for m in markets:
        latest = cache.get_latest_price(m.id)
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
