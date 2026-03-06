import duckdb

from agenttrader.data.models import Platform
from agenttrader.data.parquet_adapter import ParquetDataAdapter


def _write_parquet_dataset(base):
    (base / "polymarket" / "markets").mkdir(parents=True, exist_ok=True)
    (base / "polymarket" / "trades").mkdir(parents=True, exist_ok=True)
    (base / "polymarket" / "blocks").mkdir(parents=True, exist_ok=True)
    (base / "kalshi" / "markets").mkdir(parents=True, exist_ok=True)
    (base / "kalshi" / "trades").mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect()

    conn.execute(
        f"""
        COPY (
            SELECT
                'pm-market-1'::VARCHAR AS id,
                'cond-1'::VARCHAR AS condition_id,
                'Will BTC close above $50k?'::VARCHAR AS question,
                'bitcoin-above-50k'::VARCHAR AS slug,
                '["Yes","No"]'::VARCHAR AS outcomes,
                '["0.45","0.55"]'::VARCHAR AS outcome_prices,
                '["yes-token-1","no-token-1"]'::VARCHAR AS clob_token_ids,
                12345.0::DOUBLE AS volume,
                1000.0::DOUBLE AS liquidity,
                TRUE AS active,
                FALSE AS closed,
                TIMESTAMP '2024-06-01 00:00:00' AS end_date,
                TIMESTAMP '2024-01-01 00:00:00' AS created_at,
                '0xmaker'::VARCHAR AS market_maker_address,
                NOW()::TIMESTAMP AS _fetched_at
            UNION ALL
            SELECT
                'pm-market-2'::VARCHAR,
                'cond-2'::VARCHAR,
                'Will election candidate win?'::VARCHAR,
                'will-candidate-win'::VARCHAR,
                '["Yes","No"]'::VARCHAR,
                '["1.0","0.0"]'::VARCHAR,
                '["yes-token-2","no-token-2"]'::VARCHAR,
                20000.0::DOUBLE,
                2000.0::DOUBLE,
                FALSE,
                TRUE,
                TIMESTAMP '2024-01-15 00:00:00',
                TIMESTAMP '2023-12-01 00:00:00',
                '0xmaker'::VARCHAR,
                NOW()::TIMESTAMP
        ) TO '{base / "polymarket" / "markets" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )

    conn.execute(
        f"""
        COPY (
            SELECT
                1000::BIGINT AS block_number,
                '0xtx1'::VARCHAR AS transaction_hash,
                0::BIGINT AS log_index,
                '0xorder'::VARCHAR AS order_hash,
                '0xmaker'::VARCHAR AS maker,
                '0xtaker'::VARCHAR AS taker,
                'no-token-1'::VARCHAR AS maker_asset_id,
                'yes-token-1'::VARCHAR AS taker_asset_id,
                60::BIGINT AS maker_amount,
                40::BIGINT AS taker_amount,
                0::BIGINT AS fee,
                NULL::BIGINT AS timestamp,
                NOW()::TIMESTAMP AS _fetched_at,
                '0xcontract'::VARCHAR AS _contract
        ) TO '{base / "polymarket" / "trades" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )

    conn.execute(
        f"""
        COPY (
            SELECT
                1000::BIGINT AS block_number,
                '2024-01-01T00:00:00Z'::VARCHAR AS timestamp
        ) TO '{base / "polymarket" / "blocks" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )

    conn.execute(
        f"""
        COPY (
            SELECT
                'KXBTC-24JAN01-T50000'::VARCHAR AS ticker,
                'KXBTC'::VARCHAR AS event_ticker,
                'binary'::VARCHAR AS market_type,
                'Kalshi BTC market'::VARCHAR AS title,
                ''::VARCHAR AS yes_sub_title,
                ''::VARCHAR AS no_sub_title,
                'finalized'::VARCHAR AS status,
                45::BIGINT AS yes_bid,
                46::BIGINT AS yes_ask,
                54::BIGINT AS no_bid,
                55::BIGINT AS no_ask,
                44::BIGINT AS last_price,
                345600::BIGINT AS volume,
                0::BIGINT AS volume_24h,
                0::BIGINT AS open_interest,
                'yes'::VARCHAR AS result,
                TIMESTAMP '2024-01-01 00:00:00' AS created_time,
                TIMESTAMP '2024-01-01 00:00:00' AS open_time,
                TIMESTAMP '2024-01-20 00:00:00' AS close_time,
                NOW()::TIMESTAMP AS _fetched_at
        ) TO '{base / "kalshi" / "markets" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )

    conn.execute(
        f"""
        COPY (
            SELECT
                'trade-1'::VARCHAR AS trade_id,
                'KXBTC-24JAN01-T50000'::VARCHAR AS ticker,
                12::BIGINT AS count,
                35::BIGINT AS yes_price,
                65::BIGINT AS no_price,
                'yes'::VARCHAR AS taker_side,
                TIMESTAMP '2024-01-03 00:00:00' AS created_time,
                NOW()::TIMESTAMP AS _fetched_at
        ) TO '{base / "kalshi" / "trades" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )


def test_parquet_adapter_market_and_history_translation(tmp_path):
    _write_parquet_dataset(tmp_path)
    adapter = ParquetDataAdapter(data_dir=tmp_path)
    assert adapter.is_available() is True

    poly_markets = adapter.get_markets(platform="polymarket", limit=10)
    assert len(poly_markets) == 2
    assert all(m.platform == Platform.POLYMARKET for m in poly_markets)

    resolved_poly = adapter.get_markets(platform="polymarket", resolved_only=True, limit=10)
    assert len(resolved_poly) == 1
    assert resolved_poly[0].resolved is True
    assert resolved_poly[0].resolution == "yes"

    poly_history = adapter.get_price_history("yes-token-1", Platform.POLYMARKET, 1700000000, 1710000000)
    assert len(poly_history) == 1
    assert poly_history[0].yes_price == 0.4
    assert poly_history[0].no_price == 0.6

    kalshi_markets = adapter.get_markets(platform="kalshi", limit=10)
    assert len(kalshi_markets) == 1
    assert kalshi_markets[0].platform == Platform.KALSHI
    assert kalshi_markets[0].volume == 3456.0

    kalshi_history = adapter.get_price_history("KXBTC-24JAN01-T50000", Platform.KALSHI, 1700000000, 1710000000)
    assert len(kalshi_history) == 1
    assert kalshi_history[0].yes_price == 0.35
    assert kalshi_history[0].no_price == 0.65


def test_parquet_adapter_ignores_appledouble_files(tmp_path):
    _write_parquet_dataset(tmp_path)
    # Add invalid AppleDouble sidecar file that should be ignored.
    bad = tmp_path / "polymarket" / "markets" / "._fake.parquet"
    bad.write_bytes(b"not-a-parquet-file")

    adapter = ParquetDataAdapter(data_dir=tmp_path)
    markets = adapter.get_markets(platform="polymarket", limit=5)
    assert len(markets) > 0


def test_parquet_adapter_discovers_recursive_partitioned_files(tmp_path):
    _write_parquet_dataset(tmp_path)

    paths = [
        tmp_path / "polymarket" / "markets" / "part-000.parquet",
        tmp_path / "polymarket" / "trades" / "part-000.parquet",
        tmp_path / "polymarket" / "blocks" / "part-000.parquet",
        tmp_path / "kalshi" / "markets" / "part-000.parquet",
        tmp_path / "kalshi" / "trades" / "part-000.parquet",
    ]
    for src in paths:
        nested = src.parent / "year=2024" / "month=01"
        nested.mkdir(parents=True, exist_ok=True)
        src.rename(nested / src.name)

    adapter = ParquetDataAdapter(data_dir=tmp_path)
    markets = adapter.get_markets(platform="all", limit=10)
    ids = {market.id for market in markets}

    assert "yes-token-1" in ids
    assert "KXBTC-24JAN01-T50000" in ids


def test_parquet_get_markets_by_ids_bulk_fast_path(tmp_path, monkeypatch):
    _write_parquet_dataset(tmp_path)
    adapter = ParquetDataAdapter(data_dir=tmp_path)

    calls = {"fast": 0, "fallback": 0}
    original_fast = adapter._populate_temp_market_ids_fast
    original_fallback = adapter._populate_temp_market_ids_fallback

    def wrapped_fast(table_name, market_ids):
        calls["fast"] += 1
        return original_fast(table_name, market_ids)

    def wrapped_fallback(table_name, market_ids):
        calls["fallback"] += 1
        return original_fallback(table_name, market_ids)

    monkeypatch.setattr(adapter, "_populate_temp_market_ids_fast", wrapped_fast)
    monkeypatch.setattr(adapter, "_populate_temp_market_ids_fallback", wrapped_fallback)

    results = adapter.get_markets_by_ids_bulk(["yes-token-1", "KXBTC-24JAN01-T50000"], platform="all")
    result_ids = {market.id for market in results}

    assert "yes-token-1" in result_ids
    assert "KXBTC-24JAN01-T50000" in result_ids
    assert calls["fast"] == 1
    assert calls["fallback"] == 0


def test_parquet_get_markets_by_ids_bulk_fallback_path(tmp_path, monkeypatch):
    _write_parquet_dataset(tmp_path)
    adapter = ParquetDataAdapter(data_dir=tmp_path)

    calls = {"fast": 0, "fallback": 0}
    original_fallback = adapter._populate_temp_market_ids_fallback

    def unavailable_fast(table_name, market_ids):  # noqa: ARG001
        calls["fast"] += 1
        return False

    def wrapped_fallback(table_name, market_ids):
        calls["fallback"] += 1
        return original_fallback(table_name, market_ids)

    monkeypatch.setattr(adapter, "_populate_temp_market_ids_fast", unavailable_fast)
    monkeypatch.setattr(adapter, "_populate_temp_market_ids_fallback", wrapped_fallback)

    results = adapter.get_markets_by_ids_bulk(["cond-1", "KXBTC-24JAN01-T50000"], platform="all")
    result_ids = {market.id for market in results}

    assert "yes-token-1" in result_ids
    assert "KXBTC-24JAN01-T50000" in result_ids
    assert calls["fast"] == 1
    assert calls["fallback"] == 1


def test_parquet_get_markets_by_ids_bulk_mixed_platform_matching(tmp_path):
    _write_parquet_dataset(tmp_path)
    adapter = ParquetDataAdapter(data_dir=tmp_path)

    results = adapter.get_markets_by_ids_bulk(
        ["yes-token-1", "cond-1", "KXBTC-24JAN01-T50000", "KXBTC-24JAN01-T50000", "missing"],
        platform="all",
    )

    by_id = {market.id: market for market in results}
    assert set(by_id) == {"yes-token-1", "KXBTC-24JAN01-T50000"}
    assert by_id["yes-token-1"].platform == Platform.POLYMARKET
    assert by_id["yes-token-1"].condition_id == "cond-1"
    assert by_id["KXBTC-24JAN01-T50000"].platform == Platform.KALSHI
