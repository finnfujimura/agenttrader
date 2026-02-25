# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import os
import time

import click

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.config import APP_DIR, load_config
from agenttrader.data.cache import DataCache
from agenttrader.data.dome_client import DomeClient
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine
from agenttrader.errors import AgentTraderError


@click.command("sync")
@click.option("--days", type=int, default=None)
@click.option("--platform", default="all")
@click.option("--category", default=None)
@click.option("--markets", "market_ids", multiple=True)
@click.option("--granularity", type=click.Choice(["hourly", "minute", "daily"]), default=None)
@click.option("--limit", type=int, default=100)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def sync_cmd(
    days: int | None,
    platform: str,
    category: str | None,
    market_ids: tuple[str, ...],
    granularity: str | None,
    limit: int,
    json_output: bool,
) -> None:
    ensure_initialized()
    cfg = load_config()
    api_key = str(cfg.get("dome_api_key", ""))
    if not api_key:
        raise AgentTraderError(
            "ConfigError",
            "dome_api_key not set. Run: agenttrader config set dome_api_key <key>",
        )

    if days is None:
        days = int(cfg.get("max_sync_days", 90))
    granularity = granularity or str(cfg.get("sync_granularity", "hourly"))

    interval_map = {
        "hourly": 60,
        "minute": 1,
        "daily": 1440,
    }

    engine = get_engine()
    cache = DataCache(engine)
    ob_store = OrderBookStore()
    client = DomeClient(api_key)

    start_ts = int(time.time()) - days * 24 * 3600
    end_ts = int(time.time())

    errors: list[dict] = []
    markets_synced = 0
    price_points_fetched = 0
    orderbook_files_written = 0

    fetched_markets = client.get_markets(
        platform=platform,
        category=category,
        market_ids=list(market_ids) if market_ids else None,
        limit=limit,
    )

    for market in fetched_markets:
        try:
            cache.upsert_market(market)
            candles = client.get_candlesticks(
                condition_id=market.condition_id,
                platform=market.platform,
                start_time=start_ts,
                end_time=end_ts,
                interval=interval_map[granularity],
            )
            cache.upsert_price_points_batch(market.id, market.platform.value, candles)
            price_points_fetched += len(candles)

            orderbooks = client.get_orderbook_snapshots(
                market_id=market.id,
                platform=market.platform,
                start_time=start_ts,
                end_time=end_ts,
                limit=100,
            )
            orderbook_files_written += ob_store.write(market.platform.value, market.id, orderbooks)

            cache.mark_market_synced(market.id, int(time.time()))
            markets_synced += 1

            if not json_output:
                click.echo(f"synced {market.platform.value}:{market.id} ({len(candles)} candles, {len(orderbooks)} orderbooks)", err=True)
        except Exception as exc:  # pragma: no cover
            errors.append({"market_id": market.id, "error": str(exc)})

    disk_bytes = 0
    if APP_DIR.exists():
        for root, _, files in os.walk(APP_DIR):
            for fn in files:
                path = os.path.join(root, fn)
                if os.path.isfile(path):
                    disk_bytes += os.path.getsize(path)

    payload = {
        "ok": len(errors) == 0,
        "markets_synced": markets_synced,
        "price_points_fetched": price_points_fetched,
        "orderbook_files_written": orderbook_files_written,
        "disk_used_mb": round(disk_bytes / (1024 * 1024), 3),
        "errors": errors,
    }

    if json_output:
        emit_json(payload)
    else:
        click.echo(
            f"Synced {markets_synced} markets, {price_points_fetched} price points, {orderbook_files_written} orderbook files"
        )
