# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import asyncio
import importlib.util
import inspect
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

from agenttrader.config import load_config
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import LiveContext
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine
from agenttrader.db.schema import PaperPortfolio


@dataclass
class DaemonRuntime:
    strategy: BaseStrategy | None = None
    context: LiveContext | None = None
    observer: Observer | None = None
    shutdown: bool = False
    reload_requested: threading.Event = field(default_factory=threading.Event)


class StrategyFileHandler(FileSystemEventHandler):
    def __init__(self, daemon: "PaperDaemon"):
        self._daemon = daemon

    def on_modified(self, event: FileSystemEvent) -> None:
        if Path(event.src_path).resolve() == self._daemon.strategy_path:
            # Signal the main loop to reload — don't mutate state from watchdog thread
            self._daemon._runtime.reload_requested.set()


class PaperDaemon:
    def __init__(self, portfolio_id: str, strategy_path: str, initial_cash: float):
        self.portfolio_id = portfolio_id
        self.strategy_path = Path(strategy_path).resolve()
        self.initial_cash = float(initial_cash)
        self._runtime = DaemonRuntime()
        self._emit_stdout = True

    def start_as_daemon(self) -> int:
        log_dir = Path.home() / ".agenttrader" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stderr_path = log_dir / f"daemon-{self.portfolio_id}.log"

        stderr_file = open(stderr_path, "w", encoding="utf-8")  # noqa: SIM115

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
        self._stderr_file = stderr_file
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

        engine = get_engine()
        cache = DataCache(engine)
        ob_store = OrderBookStore()
        self._runtime.context = LiveContext(self.portfolio_id, self.initial_cash, cache, ob_store)
        self._runtime.context.load_positions_from_db()

        self._load_strategy()
        self._setup_file_watcher()

        try:
            asyncio.run(self._main_loop())
        finally:
            if self._runtime.observer:
                self._runtime.observer.stop()
                self._runtime.observer.join(timeout=2)
            if self._runtime.strategy:
                self._runtime.strategy.on_stop()

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
        strategy = strategy_class(self._runtime.context)
        strategy.on_start()
        self._runtime.strategy = strategy
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
        if self._runtime.strategy:
            self._runtime.strategy.on_stop()

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

    async def _main_loop(self):
        if not self._runtime.context or not self._runtime.strategy:
            raise RuntimeError("Daemon not initialized")

        cfg = load_config()
        interval_seconds = int(cfg.get("schedule_interval_minutes", 15)) * 60
        next_schedule = time.time() + interval_seconds

        while not self._runtime.shutdown:
            # Check for reload request from watchdog thread (thread-safe)
            if self._runtime.reload_requested.is_set():
                self._runtime.reload_requested.clear()
                self._reload_strategy()

            subscriptions = self._runtime.context.subscriptions
            for market_id, market in subscriptions.items():
                latest = self._runtime.context._cache.get_latest_price(market_id)
                if latest is None:
                    continue
                self._runtime.context.set_live_price(market_id, latest.yes_price)
                orderbook = self._runtime.context.get_orderbook(market_id)
                self._runtime.strategy.on_market_data(market, latest.yes_price, orderbook)
                msg = f"Price update: {market.title[:30]} = {latest.yes_price:.3f}"
                self._runtime.context.log(msg)
                if self._emit_stdout:
                    print(msg, flush=True)

            now = time.time()
            if now >= next_schedule:
                now_dt = datetime.now(tz=UTC)
                for market in subscriptions.values():
                    self._runtime.strategy.on_schedule(now_dt, market)
                next_schedule = now + interval_seconds

            await asyncio.sleep(1.0)

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
