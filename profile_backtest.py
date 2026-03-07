"""
Profile a real backtest run against the DuckDB index.
Runs with an EmptyStrategy (pure overhead) and a ReadOnlyStrategy
to isolate data pipeline cost from strategy cost.

Usage:
    python profile_backtest.py [--markets N] [--days D] [--fidelity FIDELITY]
    python profile_backtest.py --repeated   # benchmark cache reuse across runs
"""

import cProfile
import pstats
import io
import sys
import time
import argparse

# ──────────────────────────────────────────────────────────────────────────────
# Minimal strategies
# ──────────────────────────────────────────────────────────────────────────────

from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.models import Market


class EmptyStrategy(BaseStrategy):
    def on_start(self) -> None:
        self.subscribe()  # subscribe to all markets (default: platform="all")

    def on_market_data(self, market: Market, price: float, orderbook=None) -> None:
        pass


class ReadOnlyStrategy2(BaseStrategy):
    def on_start(self) -> None:
        self.subscribe()

    def on_market_data(self, market: Market, price: float, orderbook=None) -> None:
        _ = self._ctx.get_history(market.id, lookback_hours=1)
        _ = self._ctx.get_position(market.id)


# ──────────────────────────────────────────────────────────────────────────────
# Profiling harness
# ──────────────────────────────────────────────────────────────────────────────

def run_backtest(strategy_cls, config):
    from agenttrader.core.backtest_engine import BacktestEngine
    engine = BacktestEngine()
    result = engine.run(strategy_cls, config)
    return result


def benchmark_repeated(strategy_cls, config, label: str, n_runs: int = 5):
    """Benchmark N consecutive runs on the SAME engine to show cache reuse."""
    from agenttrader.core.backtest_engine import BacktestEngine

    print(f"\n{'='*70}")
    print(f"  Repeated-run benchmark: {label}  ({n_runs} runs, same engine)")
    print(f"{'='*70}")

    engine = BacktestEngine()

    times = []
    for i in range(n_runs):
        t0 = time.perf_counter()
        engine.run(strategy_cls, config)
        elapsed = time.perf_counter() - t0
        times.append(elapsed * 1000)
        tag = "cold" if i == 0 else f"warm {i}"
        print(f"  Run {i+1} ({tag:6s}): {elapsed*1000:.0f}ms")

    cold = times[0]
    warm_avg = sum(times[1:]) / len(times[1:]) if len(times) > 1 else cold
    saving = cold - warm_avg
    pct = saving / cold * 100 if cold else 0
    print(f"\n  Cold run : {cold:.0f}ms")
    print(f"  Warm avg : {warm_avg:.0f}ms  (runs 2–{n_runs})")
    print(f"  Saving   : {saving:.0f}ms  ({pct:.0f}% faster on cache hit)")


def profile_strategy(strategy_cls, config, label: str, top_n: int = 25):
    print(f"\n{'='*70}")
    print(f"  Profiling: {label}")
    print(f"{'='*70}")

    # Warm up (avoid import/JIT noise)
    run_backtest(strategy_cls, config)

    # Profile run
    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    result = run_backtest(strategy_cls, config)
    pr.disable()
    elapsed = time.perf_counter() - t0

    events = result.get("markets_tested", "?")
    print(f"  Elapsed: {elapsed*1000:.0f}ms | Markets: {events}")

    # Print stats sorted by cumulative time
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s)
    ps.strip_dirs()
    ps.sort_stats("cumulative")
    ps.print_stats(top_n)
    output = s.getvalue()

    # Filter to agenttrader-relevant lines (skip stdlib noise at the top)
    lines = output.split("\n")
    print("\n".join(lines[:8]))  # header
    relevant = [l for l in lines[8:] if any(kw in l for kw in [
        "agenttrader", "duckdb", "index_adapter", "backtest_engine",
        "context.py", "base_strategy", "parquet", "heap", "merger",
        "{built-in", "method"
    ])]
    print("\n".join(relevant[:top_n]))

    # Also print totcalls breakdown
    print(f"\n  --- Top {top_n} by TOTTIME ---")
    s2 = io.StringIO()
    ps2 = pstats.Stats(pr, stream=s2)
    ps2.strip_dirs()
    ps2.sort_stats("tottime")
    ps2.print_stats(top_n)
    lines2 = s2.getvalue().split("\n")
    print("\n".join(lines2[:8]))
    relevant2 = [l for l in lines2[8:] if any(kw in l for kw in [
        "agenttrader", "duckdb", "index_adapter", "backtest_engine",
        "context.py", "base_strategy", "parquet", "heap", "merger",
        "{built-in", "method"
    ])]
    print("\n".join(relevant2[:top_n]))

    return elapsed, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", type=int, default=20)
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--fidelity", default="exact_trade")
    parser.add_argument("--start", default="2024-01-08")
    parser.add_argument("--repeated", action="store_true",
                        help="Run repeated-run cache benchmark instead of profiler")
    parser.add_argument("--runs", type=int, default=5,
                        help="Number of runs for --repeated mode")
    args = parser.parse_args()

    from datetime import date, timedelta
    start = date.fromisoformat(args.start)
    end = start + timedelta(days=args.days)

    from agenttrader.core.backtest_engine import BacktestConfig
    from agenttrader.data.models import ExecutionMode

    base_config = dict(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        initial_cash=10_000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
        max_markets=args.markets,
        fidelity=args.fidelity,
        execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        use_rust=False,
    )

    print(f"\nBacktest profile: {args.markets} markets, {args.days} days, fidelity={args.fidelity}")
    print(f"Date range: {start} to {end}")

    empty_cfg = BacktestConfig(strategy_path="<inline>", **base_config)
    readonly_cfg = BacktestConfig(strategy_path="<inline>", **base_config)

    if args.repeated:
        benchmark_repeated(EmptyStrategy, empty_cfg,
                           "EmptyStrategy  (pure pipeline overhead)", n_runs=args.runs)
        benchmark_repeated(ReadOnlyStrategy2, readonly_cfg,
                           "ReadOnlyStrategy (get_history + get_position per event)", n_runs=args.runs)
    else:
        profile_strategy(EmptyStrategy, empty_cfg,     "EmptyStrategy  (pure pipeline overhead)")
        profile_strategy(ReadOnlyStrategy2, readonly_cfg, "ReadOnlyStrategy (get_history + get_position per event)")


if __name__ == "__main__":
    main()
