import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine

from agenttrader.data.cache import DataCache
from agenttrader.data.models import PricePoint
from agenttrader.db.schema import Base


mcp_server = importlib.import_module("agenttrader.mcp.server")


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    return json.loads(result[0].text)


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


def test_cache_upsert_price_points_batch_updates_existing_row(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.sqlite'}")
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
