# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import os
import time

import click

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.config import APP_DIR, load_config
from agenttrader.data.cache import DataCache
from agenttrader.data.pmxt_client import PmxtClient
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine


def _get_candlesticks_with_chunking(
    client: PmxtClient,
    market,
    start_ts: int,
    end_ts: int,
    interval_minutes: int,
):
    try:
        return client.get_candlesticks(
            condition_id=market.condition_id,
            platform=market.platform,
            start_time=start_ts,
            end_time=end_ts,
            interval=interval_minutes,
        )
    except Exception as exc:
        error_text = str(exc).lower()
        last_attempt = getattr(exc, "last_attempt", None)
        if last_attempt is not None and hasattr(last_attempt, "exception"):
            nested = last_attempt.exception()
            if nested is not None:
                error_text = f"{error_text} {nested}".lower()
        max_span_seconds = 31 * 24 * 3600
        if interval_minutes != 60 or "invalid interval for time range" not in error_text:
            raise
        if end_ts - start_ts <= max_span_seconds:
            raise

        all_points = []
        chunk_start = start_ts
        while chunk_start <= end_ts:
            chunk_end = min(chunk_start + max_span_seconds - 1, end_ts)
            chunk_points = client.get_candlesticks(
                condition_id=market.condition_id,
                platform=market.platform,
                start_time=chunk_start,
                end_time=chunk_end,
                interval=interval_minutes,
            )
            all_points.extend(chunk_points)
            chunk_start = chunk_end + 1

        by_ts = {point.timestamp: point for point in all_points}
        return [by_ts[ts] for ts in sorted(by_ts.keys())]


@click.command(
    "sync",
    help=(
        "Sync live market data from PMXT for paper trading.\n\n"
        "Note: backtesting prefers the Jon Becker parquet dataset, not sync data.\n"
        "Run 'agenttrader dataset download' to set up the full backtest dataset."
    ),
)
@click.option("--days", type=int, default=None)
@click.option("--platform", default="all")
@click.option("--category", default=None)
@click.option("--markets", "market_ids", multiple=True)
@click.option("--resolved", is_flag=True, default=False, help="Sync resolved/expired markets for backtesting")
@click.option("--granularity", type=click.Choice(["hourly", "minute", "daily"]), default=None)
@click.option("--limit", type=int, default=100)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def sync_cmd(
    days: int | None,
    platform: str,
    category: str | None,
    market_ids: tuple[str, ...],
    resolved: bool,
    granularity: str | None,
    limit: int,
    json_output: bool,
) -> None:
    """
    Sync live market data from PMXT for paper trading.

    Note: backtesting prefers the Jon Becker parquet dataset, not sync data.
    """
    ensure_initialized()
    cfg = load_config()

    if days is None:
        days = cfg["max_sync_days"]
    granularity = granularity or cfg["sync_granularity"]

    interval_map = {
        "hourly": 60,
        "minute": 1,
        "daily": 1440,
    }

    engine = get_engine()
    cache = DataCache(engine)
    ob_store = OrderBookStore()
    client = PmxtClient()

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
        resolved=resolved,
        limit=limit,
    )

    for market in fetched_markets:
        try:
            cache.upsert_market(market)
            market_end_ts = end_ts
            if resolved and market.close_time:
                market_end_ts = min(end_ts, int(market.close_time))

            if resolved and market_end_ts < start_ts:
                candles = []
                orderbooks = []
            else:
                candles = _get_candlesticks_with_chunking(
                    client=client,
                    market=market,
                    start_ts=start_ts,
                    end_ts=market_end_ts,
                    interval_minutes=interval_map[granularity],
                )
                orderbooks = client.get_orderbook_snapshots(
                    market_id=market.id,
                    platform=market.platform,
                    start_time=start_ts,
                    end_time=market_end_ts,
                    limit=100,
                )

            cache.upsert_price_points_batch(market.id, market.platform.value, candles, source="pmxt", granularity=granularity)
            price_points_fetched += len(candles)
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
