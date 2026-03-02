# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

LOGGER = logging.getLogger(__name__)

from agenttrader.config import RUNTIME_DIR, load_config
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import LiveContext
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.data.pmxt_client import PmxtClient
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import PaperPortfolio


@dataclass
class DaemonRuntime:
    strategy: BaseStrategy | None = None
    context: LiveContext | None = None
    observer: Observer | None = None
    shutdown: bool = False
    strategy_started: bool = False
    reload_requested: threading.Event = field(default_factory=threading.Event)


class StrategyFileHandler(FileSystemEventHandler):
    def __init__(self, daemon: "PaperDaemon"):
        self._daemon = daemon

    def on_modified(self, event: FileSystemEvent) -> None:
        if Path(event.src_path).resolve() == self._daemon.strategy_path:
            # Signal the main loop to reload - don't mutate state from watchdog thread
            self._daemon._runtime.reload_requested.set()


def runtime_status_path(portfolio_id: str) -> Path:
    return RUNTIME_DIR / f"paper-{portfolio_id}.json"


def read_runtime_status(portfolio_id: str) -> dict | None:
    path = runtime_status_path(portfolio_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class PaperDaemon:
    def __init__(self, portfolio_id: str, strategy_path: str, initial_cash: float):
        self.portfolio_id = portfolio_id
        self.strategy_path = Path(strategy_path).resolve()
        self.initial_cash = float(initial_cash)
        self._runtime = DaemonRuntime()
        self._emit_stdout = True

    def start_as_daemon(self) -> int:
        stderr_path: Path | None = None
        try:
            log_dir = Path.home() / ".agenttrader" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            stderr_path = log_dir / f"daemon-{self.portfolio_id}.log"
            stderr_file = open(stderr_path, "w", encoding="utf-8")  # noqa: SIM115
        except OSError:
            LOGGER.warning("Failed to open daemon log for %s; falling back to os.devnull", self.portfolio_id)
            stderr_file = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_file,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # On Windows, start_new_session uses CREATE_NEW_PROCESS_GROUP which
            # can cause handle inheritance issues leading to readonly SQLite.
            # Use DETACHED_PROCESS + CREATE_NO_WINDOW for clean detachment.
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agenttrader.core.paper_daemon_runner",
                self.portfolio_id,
                str(self.strategy_path),
                str(self.initial_cash),
            ],
            **kwargs,
        )
        # Close the file handle in the parent process — the subprocess
        # inherited its own copy via Popen.
        stderr_file.close()
        if stderr_path is not None:
            self._stderr_path = stderr_path
        return proc

    def _run_detached(self):
        self._emit_stdout = False
        with open(os.devnull, "w", encoding="utf-8") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())
            os.dup2(sink.fileno(), sys.stderr.fileno())
            self._run()

    def _run(self):
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

        cfg = load_config()
        engine = get_engine()
        cache = DataCache(engine)
        ob_store = OrderBookStore()
        client = PmxtClient()
        self._runtime.context = LiveContext(
            self.portfolio_id,
            self.initial_cash,
            cache,
            ob_store,
            pmxt_client=client,
            history_buffer_hours=cfg["paper_history_buffer_hours"],
            poll_interval_seconds=cfg["paper_poll_interval_seconds"],
        )
        self._runtime.context.load_positions_from_db()
        self._write_runtime_status("starting", {
            "poll_interval_seconds": cfg["paper_poll_interval_seconds"],
            "persist_interval_seconds": cfg["paper_persist_interval_seconds"],
        })

        self._load_strategy()
        self._setup_file_watcher()

        loop_coro = self._main_loop(cfg)
        try:
            asyncio.run(loop_coro)
        except Exception as exc:
            LOGGER.exception("Daemon crash for portfolio %s", self.portfolio_id)
            self._write_runtime_status("failed", {"last_error": str(exc)})
            try:
                with get_session(get_engine()) as session:
                    row = session.get(PaperPortfolio, self.portfolio_id)
                    if row:
                        row.status = "failed"
                        session.commit()
            except Exception:
                LOGGER.exception("Failed to mark portfolio %s as failed", self.portfolio_id)
            raise
        finally:
            if self._runtime.observer:
                self._runtime.observer.stop()
                self._runtime.observer.join(timeout=2)
            if self._runtime.strategy and self._runtime.strategy_started:
                self._runtime.strategy.on_stop()
            if self._runtime.shutdown:
                self._write_runtime_status("stopped")

    def _load_strategy(self):
        if not self._runtime.context:
            raise RuntimeError("LiveContext not initialized")

        # Validate strategy file before executing it
        from agenttrader.cli.validate import validate_strategy_file

        validation = validate_strategy_file(str(self.strategy_path))
        if not validation.get("valid", False):
            errors = validation.get("errors", [])
            msg = "; ".join(e.get("message", str(e)) for e in errors) if errors else "Validation failed"
            raise RuntimeError(f"Strategy validation failed: {msg}")

        module = self._import_module(self.strategy_path)
        strategy_class = self._find_strategy_class(module)
        self._runtime.context._subscriptions = {}
        strategy = strategy_class(self._runtime.context)
        strategy.on_start()
        self._runtime.strategy = strategy
        self._runtime.strategy_started = True
        self._runtime.context.log(f"Strategy loaded: {strategy_class.__name__}")

    def _setup_file_watcher(self):
        handler = StrategyFileHandler(self)
        observer = Observer()
        observer.schedule(handler, str(self.strategy_path.parent), recursive=False)
        observer.start()
        self._runtime.observer = observer

    def _reload_strategy(self):
        if not self._runtime.context:
            return
        if self._runtime.strategy and self._runtime.strategy_started:
            self._runtime.strategy.on_stop()
        self._runtime.strategy_started = False

        self._load_strategy()
        now_ts = int(datetime.now(tz=UTC).timestamp())
        self._runtime.context.log(f"Strategy reloaded from {self.strategy_path}")

        engine = self._runtime.context._cache._engine
        with engine.begin() as conn:
            conn.execute(
                PaperPortfolio.__table__.update()
                .where(PaperPortfolio.id == self.portfolio_id)
                .values(last_reload=now_ts, reload_count=(PaperPortfolio.reload_count + 1))
            )

    async def _main_loop(self, cfg: dict | None = None):
        if not self._runtime.context or not self._runtime.strategy:
            raise RuntimeError("Daemon not initialized")

        if cfg is None:
            await self._legacy_main_loop()
            return

        schedule_interval_seconds = cfg["schedule_interval_minutes"] * 60
        poll_interval_seconds = max(int(cfg["paper_poll_interval_seconds"]), 1)
        persist_interval_seconds = max(int(cfg["paper_persist_interval_seconds"]), 1)
        max_concurrency = max(int(cfg["paper_max_concurrent_requests"]), 1)
        next_schedule = time.time() + schedule_interval_seconds

        initial_results = await self._refresh_subscriptions(
            persist_interval_seconds=persist_interval_seconds,
            force_persist=True,
            max_concurrency=max_concurrency,
        )
        self._dispatch_market_updates(initial_results, emit_unchanged=True)
        initial_summary = self._summarize_live_results(initial_results)
        if initial_summary["markets_with_live_price"] == 0:
            raise RuntimeError("Paper trading could not establish live market data for any subscribed markets.")
        self._write_runtime_status("running", initial_summary)

        while not self._runtime.shutdown:
            loop_started = time.time()

            # Check for reload request from watchdog thread (thread-safe)
            if self._runtime.reload_requested.is_set():
                self._runtime.reload_requested.clear()
                self._reload_strategy()
                reload_results = await self._refresh_subscriptions(
                    persist_interval_seconds=persist_interval_seconds,
                    force_persist=True,
                    max_concurrency=max_concurrency,
                )
                self._dispatch_market_updates(reload_results, emit_unchanged=True)

            results = await self._refresh_subscriptions(
                persist_interval_seconds=persist_interval_seconds,
                force_persist=False,
                max_concurrency=max_concurrency,
            )
            self._dispatch_market_updates(results)
            self._write_runtime_status("running", self._summarize_live_results(results))

            now = time.time()
            if now >= next_schedule:
                now_dt = datetime.now(tz=UTC)
                for market in self._runtime.context.subscriptions.values():
                    try:
                        self._runtime.strategy.on_schedule(now_dt, market)
                    except Exception:
                        LOGGER.exception("on_schedule error for %s", getattr(market, "id", "?"))
                next_schedule = now + schedule_interval_seconds

            elapsed = time.time() - loop_started
            await asyncio.sleep(max(poll_interval_seconds - elapsed, 0.1))

    async def _legacy_main_loop(self) -> None:
        if not self._runtime.context or not self._runtime.strategy:
            raise RuntimeError("Daemon not initialized")

        while not self._runtime.shutdown:
            for market_id, market in self._runtime.context.subscriptions.items():
                latest = self._runtime.context._cache.get_latest_price(market_id)
                if latest is None:
                    continue
                try:
                    orderbook = self._runtime.context.get_orderbook(market_id)
                except Exception:
                    orderbook = None
                try:
                    self._runtime.strategy.on_market_data(market, latest.yes_price, orderbook)
                except Exception:
                    LOGGER.exception("on_market_data error for %s", market_id)
            await asyncio.sleep(1)

    async def _refresh_subscriptions(
        self,
        persist_interval_seconds: int,
        force_persist: bool,
        max_concurrency: int,
    ) -> list[dict]:
        if not self._runtime.context:
            return []

        subscriptions = list(self._runtime.context.subscriptions.values())
        if not subscriptions:
            return []

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _poll_market(market):
            async with semaphore:
                return await asyncio.to_thread(
                    self._runtime.context.refresh_market_live,
                    market,
                    persist_interval_seconds,
                    force_persist,
                )

        return list(await asyncio.gather(*[_poll_market(market) for market in subscriptions]))

    def _dispatch_market_updates(self, results: list[dict], emit_unchanged: bool = False) -> None:
        if not self._runtime.context or not self._runtime.strategy:
            return

        for result in results:
            point = result.get("price")
            if point is None:
                continue
            if not emit_unchanged and not result.get("updated", False):
                continue
            market = self._runtime.context.subscriptions.get(result["market_id"])
            if market is None:
                continue
            orderbook = result.get("orderbook")
            try:
                self._runtime.strategy.on_market_data(market, point.yes_price, orderbook)
            except Exception:
                LOGGER.exception("on_market_data error for %s", result["market_id"])
            if self._emit_stdout:
                print(f"Live update: {market.title[:30]} = {point.yes_price:.3f}", flush=True)

    def _summarize_live_results(self, results: list[dict]) -> dict:
        if not self._runtime.context:
            return {}

        live_state = self._runtime.context.get_live_status()
        last_live_update = max(
            (
                state["last_live_update_ts"]
                for state in live_state.values()
                if state.get("last_live_update_ts") is not None
            ),
            default=None,
        )
        return {
            "market_count": len(results),
            "markets_with_live_price": sum(1 for result in results if result.get("price") is not None),
            "markets_persisted": sum(1 for result in results if result.get("persisted")),
            "markets_degraded": sum(1 for state in live_state.values() if state.get("degraded")),
            "last_live_update": last_live_update,
            "markets": list(live_state.values()),
        }

    def _write_runtime_status(self, state: str, summary: dict | None = None) -> None:
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            payload = {
                "portfolio_id": self.portfolio_id,
                "state": state,
                "updated_at": int(time.time()),
            }
            if summary:
                payload.update(summary)
            runtime_status_path(self.portfolio_id).write_text(
                json.dumps(payload, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            LOGGER.exception("Failed to write runtime status for %s", self.portfolio_id)

    def _handle_shutdown(self, signum, frame):  # noqa: ANN001
        self._runtime.shutdown = True

    @staticmethod
    def _import_module(path: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location("agenttrader_user_strategy", str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot import strategy file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            del sys.modules[spec.name]
            raise
        return module

    @staticmethod
    def _find_strategy_class(module: ModuleType):
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseStrategy) and cls is not BaseStrategy:
                return cls
        raise RuntimeError("No BaseStrategy subclass found in strategy file")
