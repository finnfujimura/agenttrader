import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from agenttrader.data.cache import DataCache
from agenttrader.data.models import Market, MarketType, Platform, PricePoint
from agenttrader.db.schema import Base


mcp_server = importlib.import_module("agenttrader.mcp.server")


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    return json.loads(result[0].text)


def _sqlite_url(path: Path) -> str:
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return f"sqlite:///{raw.replace('\\', '/')}"


@pytest.fixture(autouse=True)
def _set_perf_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(tmp_path / "performance.jsonl"))


def test_pmxt_primary_outcome_prefers_yes_labeled_outcome():
    from agenttrader.data.pmxt_client import PmxtClient

    no = SimpleNamespace(label="No", outcome_id="outcome-no")
    yes = SimpleNamespace(label="Yes", outcome_id="outcome-yes")
    item = SimpleNamespace(yes=None, outcomes=[no, yes])

    primary = PmxtClient._primary_outcome(item)

    assert primary.outcome_id == "outcome-yes"


def test_pmxt_market_keeps_raw_category_as_tag_alias():
    from agenttrader.data.pmxt_client import PmxtClient

    client = object.__new__(PmxtClient)
    item = SimpleNamespace(
        yes=SimpleNamespace(outcome_id="KXELONMARS-99", label="Yes"),
        outcomes=[],
        tags=["World", "International"],
        category="kxelonmars",
        title="Will Elon visit Mars?",
        volume=1000.0,
        resolution_date=None,
    )

    market = client._to_market(item, Platform.KALSHI, status_hint="open")

    assert market.category == "world"
    assert "kxelonmars" in {tag.lower() for tag in market.tags}


def test_cache_get_markets_matches_category_against_tags(tmp_path):
    engine = create_engine(_sqlite_url(tmp_path / "category-cache.sqlite"))
    Base.metadata.create_all(engine)
    cache = DataCache(engine)
    cache.upsert_market(
        Market(
            id="KXELONMARS-99",
            condition_id="KXELONMARS-99",
            platform=Platform.KALSHI,
            title="Will Elon visit Mars?",
            category="world",
            tags=["World", "International", "kxelonmars"],
            market_type=MarketType.BINARY,
            volume=1000.0,
            close_time=0,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        )
    )

    markets = cache.get_markets(platform="kalshi", category="kxelonmars", limit=10)

    assert [market.id for market in markets] == ["KXELONMARS-99"]


def test_cache_upsert_price_points_batch_updates_existing_row(tmp_path):
    engine = create_engine(_sqlite_url(tmp_path / "cache.sqlite"))
    Base.metadata.create_all(engine)
    cache = DataCache(engine)

    first = PricePoint(timestamp=100, yes_price=0.2, no_price=0.8, volume=10.0)
    second = PricePoint(timestamp=100, yes_price=0.8, no_price=0.2, volume=50.0)

    cache.upsert_price_points_batch("m1", "polymarket", [first], source="pmxt", granularity="1h")
    cache.upsert_price_points_batch("m1", "polymarket", [second], source="pmxt", granularity="1m")

    latest = cache.get_latest_price("m1", platform="polymarket")
    prov = cache.get_provenance("m1", "polymarket")

    assert latest is not None
    assert latest.yes_price == 0.8
    assert latest.volume == 50.0
    assert prov.source == "pmxt"
    assert prov.granularity == "1m"


def test_normalize_pmxt_candles_repairs_isolated_complement_bar():
    points = [
        PricePoint(timestamp=100, yes_price=0.94, no_price=0.06, volume=10.0),
        PricePoint(timestamp=200, yes_price=0.058, no_price=0.942, volume=11.0),
        PricePoint(timestamp=300, yes_price=0.945, no_price=0.055, volume=12.0),
    ]

    normalized = mcp_server._normalize_pmxt_candles_to_yes_space(
        points,
        outcome_side="yes",
        reference_yes_price=0.944,
    )

    assert normalized["ok"] is True
    assert normalized["batch_inverted"] is False
    assert len(normalized["repairs"]) == 1
    repaired = next(point for point in normalized["points"] if point.timestamp == 200)
    assert repaired.yes_price == pytest.approx(0.942, abs=1e-9)


def test_sync_data_repairs_localized_inversion_and_replaces_stale_pmxt_rows(tmp_path, monkeypatch):
    engine = create_engine(_sqlite_url(tmp_path / "cache.sqlite"))
    Base.metadata.create_all(engine)
    cache = DataCache(engine)

    fixed_now = 1_772_100_000
    start_ts = fixed_now - 24 * 3600
    market = Market(
        id="51338236787729560681434534660841415073585974762690814047670810862722808070955",
        condition_id="51338236787729560681434534660841415073585974762690814047670810862722808070955",
        platform=Platform.POLYMARKET,
        title="Kevin Warsh market",
        category="politics",
        tags=[],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=fixed_now + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    cache.upsert_market(market)

    stale_only = PricePoint(timestamp=start_ts + 1800, yes_price=0.051, no_price=0.949, volume=1.0)
    stale_bad = PricePoint(timestamp=start_ts + 3600, yes_price=0.058, no_price=0.942, volume=2.0)
    cache.upsert_price_points_batch(market.id, "polymarket", [stale_only, stale_bad], source="pmxt", granularity="1h")

    fetched_points = [
        PricePoint(timestamp=start_ts + 900, yes_price=0.941, no_price=0.059, volume=10.0),
        PricePoint(timestamp=start_ts + 3600, yes_price=0.058, no_price=0.942, volume=11.0),
        PricePoint(timestamp=start_ts + 7200, yes_price=0.944, no_price=0.056, volume=12.0),
    ]

    class FakeClient:
        def get_outcome_side(self, *_args, **_kwargs):
            return "yes"

        def get_candlesticks_with_status(self, *_args, **_kwargs):
            return {"points": list(fetched_points), "status": "ok", "error": None}

        def get_live_snapshot(self, *_args, **_kwargs):
            return {
                "status": "ok",
                "price": PricePoint(timestamp=fixed_now, yes_price=0.943, no_price=0.057, volume=0.0),
                "orderbook": None,
                "error": None,
                "timestamp": fixed_now,
            }

        def get_orderbook_snapshots_with_status(self, *_args, **_kwargs):
            return {"snapshots": [], "status": "empty", "error": None}

    class FakeOrderBookStore:
        def write(self, *_args, **_kwargs):
            return 0

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(cache, "sqlite-cache")])
    monkeypatch.setattr(mcp_server, "PmxtClient", lambda: FakeClient())
    monkeypatch.setattr(mcp_server, "OrderBookStore", lambda: FakeOrderBookStore())

    with patch("time.time", return_value=fixed_now):
        sync_payload = _payload(_run(mcp_server.call_tool("sync_data", {
            "platform": "polymarket",
            "market_ids": [market.id],
            "days": 1,
        })))
        history_payload = _payload(_run(mcp_server.call_tool("get_history", {
            "market_id": market.id,
            "platform": "polymarket",
            "days": 1,
            "include_raw": True,
        })))

    stored = cache.get_price_history(market.id, start_ts, fixed_now, platform="polymarket")
    stored_timestamps = [point.timestamp for point in stored]
    repaired_point = next(point for point in stored if point.timestamp == fetched_points[1].timestamp)
    warning_types = {warning["type"] for warning in sync_payload.get("warnings", [])}
    raw_prices = [point["yes_price"] for point in history_payload["history"]]

    assert sync_payload["ok"] is True
    assert sync_payload["market_results"][0]["price_orientation_mismatch"] is False
    assert sync_payload["market_results"][0]["price_orientation_repaired"] is True
    assert sync_payload["market_results"][0]["price_orientation_repair_count"] == 1
    assert "LocalizedPriceRepair" in warning_types
    assert stored_timestamps == [point.timestamp for point in fetched_points]
    assert stale_only.timestamp not in stored_timestamps
    assert repaired_point.yes_price == pytest.approx(0.942, abs=1e-9)
    assert history_payload["ok"] is True
    assert history_payload["timestamp_format"] == "unix_seconds"
    assert history_payload["history_timestamp_format"] == "unix_seconds"
    assert history_payload["provenance"]["selected_source"] == "sqlite-cache"
    assert min(raw_prices) > 0.9
    assert next(point for point in history_payload["history"] if point["timestamp"] == fetched_points[1].timestamp)["yes_price"] == pytest.approx(0.942, abs=1e-9)


def test_get_history_includes_provenance_and_timestamp_format(monkeypatch):
    now = 1_762_000_000
    point = SimpleNamespace(timestamp=now - 60, yes_price=0.55, no_price=0.45, volume=10.0)
    mock_source = MagicMock()
    mock_source.get_price_history.return_value = [point]
    mock_source.get_provenance.return_value = SimpleNamespace(source="index", observed=True, granularity="trade")

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(mock_source, "normalized-index")])

    with patch("time.time", return_value=now):
        result = _run(mcp_server.call_tool("get_history", {
            "market_id": "m1",
            "platform": "polymarket",
            "days": 7,
            "include_raw": True,
        }))

    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["timestamp_format"] == "unix_seconds"
    assert payload["history_timestamp_format"] == "unix_seconds"
    assert payload["provenance"]["selected_source"] == "normalized-index"
    assert payload["provenance"]["source"] == "index"
    assert payload["provenance"]["granularity"] == "trade"


def test_stop_paper_trade_clears_pid(monkeypatch):
    class FakePortfolio:
        id = "p-stop"
        pid = 12345
        status = "running"

    class FakeCache:
        def get_portfolio(self, _portfolio_id):
            return FakePortfolio()

    class FakeRow:
        status = "running"
        pid = 12345
        stopped_at = None

    fake_row = FakeRow()

    class FakeSession:
        def get(self, _cls, _pk):
            return fake_row

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())
    monkeypatch.setattr("os.kill", lambda _pid, _sig: None)

    result = _run(mcp_server.call_tool("stop_paper_trade", {"portfolio_id": "p-stop"}))
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["pid"] is None
    assert fake_row.pid is None


def test_get_portfolio_hides_stale_stopped_pid(monkeypatch):
    class FakePortfolio:
        id = "p-stopped"
        pid = 22222
        status = "stopped"
        initial_cash = 1000.0
        cash_balance = 1000.0
        last_reload = None
        reload_count = 0
        stopped_at = 1234567890

    class FakeCache:
        def get_portfolio(self, _portfolio_id):
            return FakePortfolio()

        def get_open_positions(self, _portfolio_id):
            return []

    class FakeRow:
        pid = 22222

    fake_row = FakeRow()

    class FakeSession:
        def get(self, _cls, _pk):
            return fake_row

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())

    result = _run(mcp_server.call_tool("get_portfolio", {"portfolio_id": "p-stopped"}))
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["pid"] is None
    assert fake_row.pid is None


def test_list_paper_tools_alias_and_active_filter(monkeypatch):
    running = SimpleNamespace(id="p-running", status="running", pid=None, started_at=20, stopped_at=None)
    stopped = SimpleNamespace(id="p-stopped", status="stopped", pid=None, started_at=10, stopped_at=30)

    class FakeCache:
        def list_paper_portfolios(self):
            return [stopped, running]

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(
        mcp_server,
        "read_runtime_status",
        lambda pid: {"last_live_update": 100, "markets_with_live_price": 1, "markets_degraded": 0} if pid == "p-running" else None,
    )

    filtered = _payload(_run(mcp_server.call_tool("list_paper_portfolios", {"active_only": True})))
    aliased = _payload(_run(mcp_server.call_tool("list_paper_trades", {})))

    assert [row["id"] for row in filtered["portfolios"]] == ["p-running"]
    assert filtered["active_count"] == 1
    assert aliased["deprecated"] is True
    assert aliased["canonical_tool"] == "list_paper_portfolios"
    assert len(aliased["portfolios"]) == 2


def test_flatten_portfolio_reports_partial_failure(monkeypatch):
    portfolio = SimpleNamespace(id="p-flat")

    class FakeCache:
        def get_portfolio(self, _portfolio_id):
            return portfolio

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(
        mcp_server,
        "_flatten_portfolio_positions",
        lambda *_args, **_kwargs: {
            "ok": False,
            "results": [{"market_id": "m1", "status": "failed", "error": "NoPositionError"}],
            "positions_attempted": 1,
            "positions_closed": 0,
            "positions_failed": 1,
            "remaining_positions": 1,
            "realized_pnl": 0.0,
            "cash_balance": 1000.0,
            "portfolio_value_after_flatten": 1000.5,
        },
    )

    result = _run(mcp_server.call_tool("flatten_portfolio", {"portfolio_id": "p-flat", "best_effort": True}))
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error"] == "PartialFlattenFailure"
    assert payload["best_effort"] is True
    assert payload["positions_failed"] == 1


def test_backtest_metrics_open_only_trades_are_not_scored():
    from agenttrader.core.backtest_engine import BacktestEngine

    metrics = BacktestEngine()._compute_metrics(
        equity_curve=[
            {"timestamp": 0, "value": 10000.0},
            {"timestamp": 3600, "value": 10001.0},
        ],
        trades=[
            {"action": "buy", "pnl": None, "slippage": 0.0},
        ],
    )

    assert metrics["total_trades"] == 1
    assert metrics["closed_trades"] == 0
    assert metrics["open_positions_at_end"] == 1
    assert metrics["win_rate"] is None
    assert metrics["profit_factor"] is None
