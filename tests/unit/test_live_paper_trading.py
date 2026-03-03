import asyncio
import importlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from sqlalchemy import create_engine

from agenttrader.data.cache import DataCache
from agenttrader.db import get_session
from agenttrader.db.schema import Base, PaperPortfolio, Trade


context_mod = importlib.import_module("agenttrader.core.context")
paper_daemon_mod = importlib.import_module("agenttrader.core.paper_daemon")
mcp_server = importlib.import_module("agenttrader.mcp.server")
pmxt_client_mod = importlib.import_module("agenttrader.data.pmxt_client")
models = importlib.import_module("agenttrader.data.models")


def _run(coro):
    return asyncio.run(coro)


def _sqlite_url(path: Path) -> str:
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    return f"sqlite:///{raw.replace('\\', '/')}"


def test_pmxt_live_snapshot_derives_mid_price():
    client = object.__new__(pmxt_client_mod.PmxtClient)
    client._poly = SimpleNamespace(
        fetch_order_book=lambda _market_id: SimpleNamespace(
            bids=[SimpleNamespace(price=0.40, size=25)],
            asks=[SimpleNamespace(price=0.60, size=30)],
        )
    )
    client._kalshi = SimpleNamespace(fetch_order_book=lambda _market_id: None)

    snapshot = client.get_live_snapshot("poly-token", models.Platform.POLYMARKET)

    assert snapshot["status"] == "ok"
    assert snapshot["price"] is not None
    assert snapshot["price"].yes_price == 0.5
    assert snapshot["orderbook"] is not None
    assert snapshot["orderbook"].best_bid == 0.40
    assert snapshot["orderbook"].best_ask == 0.60


def test_live_context_refresh_uses_cached_price_when_snapshot_errors(tmp_path):
    engine = create_engine(_sqlite_url(tmp_path / "live-cache.sqlite"), echo=False)
    Base.metadata.create_all(engine)
    cache = DataCache(engine)

    market = models.Market(
        id="KXELONMARS-99",
        condition_id="KXELONMARS-99",
        platform=models.Platform.KALSHI,
        title="Will Elon visit Mars?",
        category="world",
        tags=["World", "International", "kxelonmars"],
        market_type=models.MarketType.BINARY,
        volume=1000.0,
        close_time=0,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    cache.upsert_market(market)
    cached_point = models.PricePoint(
        timestamp=1_700_000_000,
        yes_price=0.61,
        no_price=0.39,
        volume=25.0,
    )
    cache.upsert_price_points_batch(market.id, "kalshi", [cached_point], source="pmxt", granularity="1h")

    class BrokenPmxt:
        def get_live_snapshot(self, *_args, **_kwargs):
            return {
                "status": "error",
                "error": "orderbook unavailable",
                "timestamp": 1_700_000_100,
                "price": None,
                "orderbook": None,
            }

    class DummyOrderBookStore:
        def write(self, *_args, **_kwargs):
            return 0

    context = context_mod.LiveContext(
        portfolio_id="p-live-fallback",
        initial_cash=1000.0,
        cache=cache,
        ob_store=DummyOrderBookStore(),
        pmxt_client=BrokenPmxt(),
    )
    result = context.refresh_market_live(market, force_persist=True)
    live_status = context.get_live_status()[market.id]

    assert result["status"] == "degraded"
    assert result["persisted"] is False
    assert result["price"] is not None
    assert result["price"].timestamp == cached_point.timestamp
    assert result["price"].yes_price == cached_point.yes_price
    assert live_status["degraded"] is True
    assert live_status["has_live_price"] is True
    assert live_status["has_live_orderbook"] is False
    assert "cached price history fallback" in str(live_status["last_error"]).lower()


def test_paper_daemon_starts_running_with_cached_price_fallback(tmp_path, monkeypatch):
    strategy_path = tmp_path / "startup_strategy.py"
    strategy_path.write_text(
        "from agenttrader import BaseStrategy\n"
        "\n"
        "class StartupStrategy(BaseStrategy):\n"
        "    def on_start(self):\n"
        "        self.subscribe(platform='kalshi', market_ids=['KXELONMARS-99'])\n"
        "\n"
        "    def on_market_data(self, market, price, orderbook):\n"
        "        return\n"
    )

    db_path = tmp_path / "paper-startup.db"
    engine = create_engine(_sqlite_url(db_path), echo=False)
    Base.metadata.create_all(engine)
    portfolio_id = "p-startup-fallback"

    with get_session(engine) as session:
        session.add(
            PaperPortfolio(
                id=portfolio_id,
                strategy_path=str(strategy_path),
                strategy_hash="hash",
                initial_cash=1000.0,
                cash_balance=1000.0,
                status="running",
                started_at=1,
                reload_count=0,
            )
        )
        session.commit()

    cache = DataCache(engine)
    market = models.Market(
        id="KXELONMARS-99",
        condition_id="KXELONMARS-99",
        platform=models.Platform.KALSHI,
        title="Will Elon visit Mars?",
        category="world",
        tags=["World", "International", "kxelonmars"],
        market_type=models.MarketType.BINARY,
        volume=1000.0,
        close_time=0,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    cache.upsert_market(market)
    cache.upsert_price_points_batch(
        market.id,
        "kalshi",
        [models.PricePoint(timestamp=1_700_000_000, yes_price=0.62, no_price=0.38, volume=20.0)],
        source="pmxt",
        granularity="1h",
    )

    class FailingLivePmxt:
        def get_markets(self, **_kwargs):
            return [market]

        def get_live_snapshot(self, *_args, **_kwargs):
            return {
                "status": "error",
                "error": "orderbook unavailable",
                "timestamp": 1_700_000_100,
                "price": None,
                "orderbook": None,
            }

    writes: list[tuple[str, dict | None]] = []

    async def fake_sleep(_seconds):
        daemon._runtime.shutdown = True

    monkeypatch.setattr(paper_daemon_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(paper_daemon_mod, "PmxtClient", lambda: FailingLivePmxt())
    monkeypatch.setattr(
        paper_daemon_mod,
        "OrderBookStore",
        lambda: importlib.import_module("agenttrader.data.orderbook_store").OrderBookStore(tmp_path / "orderbooks"),
    )
    monkeypatch.setattr(
        paper_daemon_mod,
        "load_config",
        lambda: {
            "schedule_interval_minutes": 60,
            "paper_poll_interval_seconds": 1,
            "paper_persist_interval_seconds": 60,
            "paper_max_concurrent_requests": 1,
            "paper_history_buffer_hours": 24,
        },
    )
    monkeypatch.setattr(paper_daemon_mod.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paper_daemon_mod.PaperDaemon, "_setup_file_watcher", lambda self: None)
    monkeypatch.setattr(
        paper_daemon_mod.PaperDaemon,
        "_write_runtime_status",
        lambda self, state, summary=None: writes.append((state, summary)),
    )
    monkeypatch.setattr(paper_daemon_mod.asyncio, "sleep", fake_sleep)

    try:
        daemon = paper_daemon_mod.PaperDaemon(portfolio_id, str(strategy_path), 1000.0)
        daemon._emit_stdout = False
        daemon._run()
    finally:
        engine.dispose()

    assert writes
    running_state, running_summary = next((state, summary) for state, summary in writes if state == "running")
    assert running_state == "running"
    assert running_summary is not None
    assert running_summary["markets_with_live_price"] == 1
    assert running_summary["markets_degraded"] >= 1


def test_get_portfolio_prefers_runtime_live_price(monkeypatch):
    class FakePortfolio:
        id = "p-live"
        pid = 123
        status = "running"
        initial_cash = 1000.0
        cash_balance = 900.0
        last_reload = None
        reload_count = 0

    class FakePosition:
        market_id = "m1"
        platform = "polymarket"
        side = "yes"
        contracts = 10.0
        avg_cost = 0.4

    class FakeLatest:
        yes_price = 0.5

    class FakeCache:
        def get_portfolio(self, _pid):
            return FakePortfolio()

        def get_open_positions(self, _pid):
            return [FakePosition()]

        def get_latest_price(self, _market_id):
            return FakeLatest()

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        mcp_server,
        "read_runtime_status",
        lambda _pid: {
            "last_live_update": 1234567890,
            "markets_with_live_price": 1,
            "markets_degraded": 0,
            "markets": [
                {
                    "market_id": "m1",
                    "current_price": 0.9,
                }
            ],
        },
    )

    result = _run(mcp_server.call_tool("get_portfolio", {"portfolio_id": "p-live"}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is True
    assert payload["positions"][0]["current_price"] == 0.9
    assert payload["portfolio_value"] == 909.0
    assert payload["last_live_update"] == 1234567890


def test_paper_daemon_live_loop_executes_churn_strategy(monkeypatch):
    base_tmp_dir = Path.cwd() / "codex_tmp_test_live_manual"
    if base_tmp_dir.exists():
        shutil.rmtree(base_tmp_dir, ignore_errors=True)
    base_tmp_dir.mkdir(exist_ok=True)
    tmp_path = base_tmp_dir
    strategy_path = tmp_path / "churn_strategy.py"
    strategy_path.write_text(
        "from agenttrader import BaseStrategy\n"
        "\n"
        "class ChurnStrategy(BaseStrategy):\n"
        "    def on_start(self):\n"
        "        self.subscribe(platform='polymarket', market_ids=['m1'])\n"
        "\n"
        "    def on_market_data(self, market, price, orderbook):\n"
        "        pos = self.get_position(market.id)\n"
        "        if pos is None and price < 0.5:\n"
        "            self.buy(market.id, 1)\n"
        "        elif pos is not None and price > 0.5:\n"
        "            self.sell(market.id)\n"
    )

    db_path = tmp_path / "paper.db"
    engine = create_engine(_sqlite_url(db_path), echo=False)
    Base.metadata.create_all(engine)

    portfolio_id = "p-churn"
    with get_session(engine) as session:
        session.add(
            PaperPortfolio(
                id=portfolio_id,
                strategy_path=str(strategy_path),
                strategy_hash="hash",
                initial_cash=1000.0,
                cash_balance=1000.0,
                status="running",
                started_at=1,
                reload_count=0,
            )
        )
        session.commit()

    market = models.Market(
        id="m1",
        condition_id="m1",
        platform=models.Platform.POLYMARKET,
        title="Test Market",
        category="politics",
        tags=[],
        market_type=models.MarketType.BINARY,
        volume=1000.0,
        close_time=0,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )

    prices = [0.40, 0.60, 0.40, 0.60]
    tick = {"n": 0}

    class FakePmxtClient:
        def get_markets(self, **_kwargs):
            return [market]

        def search_markets(self, _query, platform="all", limit=100):
            _ = (platform, limit)
            return [market]

        def get_live_snapshot(self, outcome_id, platform):
            _ = (outcome_id, platform)
            idx = min(tick["n"], len(prices) - 1)
            price = prices[idx]
            tick["n"] += 1
            ts = 1_700_000_000 + tick["n"]
            orderbook = models.OrderBook(
                market_id="m1",
                timestamp=ts,
                bids=[models.OrderLevel(price=max(0.0, price - 0.01), size=100)],
                asks=[models.OrderLevel(price=min(1.0, price + 0.01), size=100)],
            )
            return {
                "status": "ok",
                "error": None,
                "timestamp": ts,
                "price": models.PricePoint(
                    timestamp=ts,
                    yes_price=price,
                    no_price=1.0 - price,
                    volume=0.0,
                ),
                "orderbook": orderbook,
            }

    sleep_calls = {"n": 0}

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            daemon._runtime.shutdown = True

    monkeypatch.setattr(paper_daemon_mod, "get_engine", lambda: engine)
    monkeypatch.setattr(paper_daemon_mod, "PmxtClient", lambda: FakePmxtClient())
    monkeypatch.setattr(
        paper_daemon_mod,
        "OrderBookStore",
        lambda: importlib.import_module("agenttrader.data.orderbook_store").OrderBookStore(tmp_path / "orderbooks"),
    )
    monkeypatch.setattr(
        paper_daemon_mod,
        "load_config",
        lambda: {
            "schedule_interval_minutes": 60,
            "paper_poll_interval_seconds": 1,
            "paper_persist_interval_seconds": 60,
            "paper_max_concurrent_requests": 1,
            "paper_history_buffer_hours": 24,
        },
    )
    monkeypatch.setattr(paper_daemon_mod.signal, "signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(paper_daemon_mod.PaperDaemon, "_setup_file_watcher", lambda self: None)
    monkeypatch.setattr(paper_daemon_mod.PaperDaemon, "_write_runtime_status", lambda self, *_args, **_kwargs: None)
    monkeypatch.setattr(paper_daemon_mod.asyncio, "sleep", fake_sleep)

    try:
        daemon = paper_daemon_mod.PaperDaemon(portfolio_id, str(strategy_path), 1000.0)
        daemon._emit_stdout = False
        daemon._run()

        with get_session(engine) as session:
            trades = list(session.query(Trade).order_by(Trade.filled_at.asc()).all())

        actions = [trade.action for trade in trades]
        assert actions == ["buy", "sell", "buy", "sell"]
    finally:
        engine.dispose()
        shutil.rmtree(tmp_path, ignore_errors=True)
