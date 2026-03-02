import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
from click.testing import CliRunner

from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.index_adapter import BacktestIndexAdapter
from agenttrader.data.models import ExecutionMode, Market, MarketType, Platform, PricePoint


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _make_market(mid="m1"):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    return Market(
        id=mid,
        condition_id=mid,
        platform=Platform.POLYMARKET,
        title=f"Market {mid}",
        category="politics",
        tags=[],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )


def test_streaming_backtest_emits_preflight_and_progress(monkeypatch):
    backtest_engine_mod = importlib.import_module("agenttrader.core.backtest_engine")

    market = _make_market("m1")
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):  # noqa: ARG002
            return [market]

    class FakeIndex:
        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("m1", "polymarket", 2)]

        def get_latest_prices_before_batch(self, market_ids, platform, ts):  # noqa: ARG002
            return {"m1": 0.40}

        def stream_market_history_batch(self, market_ids, platform, start_ts, end_ts):  # noqa: ARG002
            yield ("m1", PricePoint(timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0))
            yield ("m1", PricePoint(timestamp=now_ts + 120, yes_price=0.55, no_price=0.45, volume=12.0))

        def stream_market_history_resampled_batch(self, market_ids, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history_batch(market_ids, platform, start_ts, end_ts)

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            if price < 0.50 and self.get_position(market.id) is None:
                self.buy(market.id, contracts=1)
            elif price > 0.50 and self.get_position(market.id) is not None:
                self.sell(market.id)

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    monkeypatch.setattr(backtest_engine_mod, "PROGRESS_INTERVAL_SECONDS", 0.0)

    events = []
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        FakeIndex(),
        progress_callback=events.append,
    )

    assert result["ok"] is True
    assert any(event["kind"] == "preflight" for event in events)
    assert any(event["kind"] == "progress" for event in events)


def test_batch_streaming_matches_per_market_order(tmp_path):
    db_path = tmp_path / "batch-stream.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE normalized_trades (market_id VARCHAR, platform VARCHAR, ts BIGINT, yes_price DOUBLE, volume DOUBLE)")
    conn.execute("CREATE TABLE market_metadata (market_id VARCHAR, platform VARCHAR, n_trades BIGINT, min_ts BIGINT, max_ts BIGINT)")
    rows = [
        ("m1", "polymarket", 100, 0.20, 1.0),
        ("m2", "polymarket", 100, 0.30, 1.0),
        ("m1", "polymarket", 200, 0.40, 2.0),
        ("m2", "polymarket", 250, 0.50, 3.0),
        ("m3", "polymarket", 300, 0.60, 4.0),
        ("m1", "polymarket", 3700, 0.70, 5.0),
        ("m2", "polymarket", 3800, 0.80, 6.0),
    ]
    meta = [
        ("m1", "polymarket", 3, 100, 3700),
        ("m2", "polymarket", 3, 100, 3800),
        ("m3", "polymarket", 1, 300, 300),
    ]
    conn.executemany("INSERT INTO normalized_trades VALUES (?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO market_metadata VALUES (?, ?, ?, ?, ?)", meta)
    conn.close()

    adapter = BacktestIndexAdapter(index_path=db_path)
    market_ids = ["m1", "m2", "m3"]

    import heapq

    iterators = {
        mid: adapter.stream_market_history(mid, "polymarket", 0, 5000)
        for mid in market_ids
    }
    heap = []
    for mid in market_ids:
        first = next(iterators[mid], None)
        if first is not None:
            heapq.heappush(heap, (int(first.timestamp), mid, first))
    per_market = []
    while heap:
        ts, mid, point = heapq.heappop(heap)
        per_market.append((mid, point.timestamp, point.yes_price))
        nxt = next(iterators[mid], None)
        if nxt is not None:
            heapq.heappush(heap, (int(nxt.timestamp), mid, nxt))

    batched = [
        (mid, point.timestamp, point.yes_price)
        for mid, point in adapter.stream_market_history_batch(market_ids, "polymarket", 0, 5000)
    ]
    assert batched == per_market

    resampled_per_market = []
    iterators = {
        mid: adapter.stream_market_history_resampled(mid, "polymarket", 0, 5000, 3600)
        for mid in market_ids
    }
    heap = []
    for mid in market_ids:
        first = next(iterators[mid], None)
        if first is not None:
            heapq.heappush(heap, (int(first.timestamp), mid, first))
    while heap:
        ts, mid, point = heapq.heappop(heap)
        resampled_per_market.append((mid, point.timestamp, round(point.yes_price, 6)))
        nxt = next(iterators[mid], None)
        if nxt is not None:
            heapq.heappush(heap, (int(nxt.timestamp), mid, nxt))

    resampled_batched = [
        (mid, point.timestamp, round(point.yes_price, 6))
        for mid, point in adapter.stream_market_history_resampled_batch(market_ids, "polymarket", 0, 5000, 3600)
    ]
    adapter.close()

    assert resampled_batched == resampled_per_market


def test_cli_backtest_json_progress_uses_stderr_and_stdout_final_only(monkeypatch, tmp_path):
    backtest_mod = importlib.import_module("agenttrader.cli.backtest")

    strategy = tmp_path / "strat.py"
    strategy.write_text(
        "from agenttrader import BaseStrategy\n"
        "class TestStrategy(BaseStrategy):\n"
        "    def on_start(self):\n"
        "        self.subscribe(platform='polymarket')\n"
        "    def on_market_data(self, market, price, orderbook):\n"
        "        pass\n",
        encoding="utf-8",
    )

    store = {}

    class FakeSession:
        def add(self, obj):
            store[obj.id] = obj

        def get(self, _cls, key):
            return store.get(key)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeEngine:
        def run(self, strategy_class, config, progress_callback=None):
            assert progress_callback is not None
            progress_callback(
                {
                    "kind": "preflight",
                    "data_source": "normalized-index",
                    "fidelity": "exact_trade",
                    "range_start_ts": 100,
                    "range_end_ts": 200,
                    "markets_tested": 3,
                    "max_markets_applied": None,
                    "estimated_work_units": 50,
                    "work_unit_label": "events",
                    "warnings": [],
                }
            )
            progress_callback(
                {
                    "kind": "progress",
                    "data_source": "normalized-index",
                    "fidelity": "exact_trade",
                    "range_start_ts": 100,
                    "range_end_ts": 200,
                    "current_ts": 150,
                    "markets_tested": 3,
                    "max_markets_applied": None,
                    "processed_units": 25,
                    "work_unit_label": "events",
                    "percent_complete": 50.0,
                    "elapsed_seconds": 5.0,
                    "throughput_per_second": 5.0,
                    "eta_seconds": 5.0,
                }
            )
            return {
                "ok": True,
                "data_source": "normalized-index",
                "final_value": 1001.0,
                "metrics": {"sharpe_ratio": 1.2},
                "_artifact_payload": {"equity_curve": [], "trades": []},
            }

    monkeypatch.setattr(backtest_mod, "ensure_initialized", lambda: None)
    monkeypatch.setattr(backtest_mod, "validate_strategy_file", lambda _path: {"valid": True, "errors": [], "warnings": []})
    monkeypatch.setattr(backtest_mod, "load_config", lambda: {"default_initial_cash": 1000.0, "schedule_interval_minutes": 15})
    monkeypatch.setattr(backtest_mod, "get_engine", lambda: object())
    monkeypatch.setattr(backtest_mod, "get_session", lambda _engine: FakeSession())
    monkeypatch.setattr(backtest_mod, "get_backtest_engine", lambda: FakeEngine())
    monkeypatch.setattr(backtest_mod, "write_backtest_artifact", lambda run_id, curve, trades: f"{run_id}.msgpack")

    runner = CliRunner()
    result = runner.invoke(
        backtest_mod.backtest_cmd,
        [str(strategy), "--from", "2024-01-01", "--to", "2024-01-02", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "Backtest starting" not in result.stdout
    assert "Backtest progress" not in result.stdout
    assert "Backtest starting" in result.stderr
    assert "Backtest progress" in result.stderr


def test_mcp_list_backtests_includes_running_progress(monkeypatch):
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakeRow:
        id = "run-1"
        status = "running"
        strategy_path = "strat.py"
        results_json = json.dumps(
            {
                "ok": True,
                "run_id": "run-1",
                "status": "running",
                "progress": {
                    "percent_complete": 12.5,
                    "processed_units": 100,
                    "work_unit_label": "events",
                },
            }
        )

    class FakeCache:
        def list_backtest_runs(self, limit=100):
            return [FakeRow()]

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())

    result = _run(mcp_server.call_tool("list_backtests", {}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True
    assert payload["runs"][0]["progress_pct"] == 12.5
    assert payload["runs"][0]["processed_units"] == 100
