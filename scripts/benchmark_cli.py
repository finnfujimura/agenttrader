from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import import_module, reload
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = REPO_ROOT / "benchmarks" / "runtime"
STATE_ROOT = BENCHMARK_ROOT / "state"
DATA_ROOT = BENCHMARK_ROOT / "shared"
BASELINES_DIR = REPO_ROOT / "benchmarks" / "baselines"
STRATEGY_PATH = REPO_ROOT / "tests" / "fixtures" / "sample_strategy.py"
BENCHMARK_MARKET_ID = "poly-bench-000"
DEFAULT_REAL_DATA_ROOT = Path.home() / ".agenttrader"


@dataclass(frozen=True)
class CommandSpec:
    name: str
    args: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark local agenttrader CLI commands.")
    parser.add_argument("--iterations", type=int, default=5, help="Measured runs per command")
    parser.add_argument("--warmups", type=int, default=1, help="Warmup runs per command")
    parser.add_argument("--backtest-iterations", type=int, default=1, help="Measured runs for the heavy backtest command")
    parser.add_argument("--backtest-warmups", type=int, default=0, help="Warmup runs for the heavy backtest command")
    parser.add_argument(
        "--backtest-data-root",
        type=Path,
        default=DEFAULT_REAL_DATA_ROOT,
        help="Shared data root containing the real backtest index and parquet dataset",
    )
    parser.add_argument("--backtest-from", default="2024-01-01", help="Start date for the heavy real-index backtest")
    parser.add_argument("--backtest-to", default="2025-12-31", help="End date for the heavy real-index backtest")
    parser.add_argument(
        "--output",
        type=Path,
        default=BASELINES_DIR / f"cli_baseline_{datetime.now(tz=UTC).strftime('%Y-%m-%d')}.json",
        help="JSON file to write benchmark results to",
    )
    return parser.parse_args()


def base_env(state_dir: Path, data_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["AGENTTRADER_STATE_DIR"] = str(state_dir)
    env["AGENTTRADER_DATA_ROOT"] = str(data_root)
    env.setdefault("PYTHONUTF8", "1")
    return env


def run_cli(args: list[str], env: dict[str, str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agenttrader.cli.main", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
    )


def parse_json_output(stdout: str) -> dict | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def summarize_payload(payload: dict | None) -> dict[str, object]:
    if not payload:
        return {}

    summary: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        elif isinstance(value, list) and key in {"markets", "history", "trades", "equity_curve", "runs"}:
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict) and key in {"metrics", "resolution_accuracy"}:
            summary[key] = value
    return summary


def compute_stats(samples_ms: list[float]) -> dict[str, float]:
    stats = {
        "min_ms": round(min(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
        "mean_ms": round(statistics.mean(samples_ms), 3),
        "median_ms": round(statistics.median(samples_ms), 3),
    }
    stats["stdev_ms"] = round(statistics.stdev(samples_ms), 3) if len(samples_ms) > 1 else 0.0
    return stats


def benchmark_command(spec: CommandSpec, env: dict[str, str], iterations: int, warmups: int) -> dict:
    warmup_samples: list[float] = []
    measured_samples: list[float] = []
    last_payload: dict | None = None

    for phase, count, bucket in (("warmup", warmups, warmup_samples), ("measured", iterations, measured_samples)):
        for _ in range(count):
            started = time.perf_counter()
            result = run_cli(spec.args, env)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if result.returncode != 0:
                raise RuntimeError(
                    f"{spec.name} failed during {phase} run\n"
                    f"command: {' '.join(spec.args)}\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                )
            bucket.append(elapsed_ms)
            payload = parse_json_output(result.stdout)
            if payload is not None:
                last_payload = payload

    return {
        "name": spec.name,
        "command": " ".join(spec.args),
        "warmup_runs": warmups,
        "iterations": iterations,
        "warmup_ms": [round(sample, 3) for sample in warmup_samples],
        "samples_ms": [round(sample, 3) for sample in measured_samples],
        "stats": compute_stats(measured_samples),
        "last_payload_keys": sorted(last_payload.keys()) if last_payload else [],
        "payload_summary": summarize_payload(last_payload),
        "last_payload": last_payload,
    }


def benchmark_init(iterations: int, warmups: int) -> dict:
    warmup_samples: list[float] = []
    measured_samples: list[float] = []
    init_root = BENCHMARK_ROOT / "init_runs"
    shutil.rmtree(init_root, ignore_errors=True)
    init_root.mkdir(parents=True, exist_ok=True)

    run_index = 0
    for phase, count, bucket in (("warmup", warmups, warmup_samples), ("measured", iterations, measured_samples)):
        for _ in range(count):
            state_dir = init_root / f"state_{run_index:02d}"
            data_root = init_root / f"data_{run_index:02d}"
            env = base_env(state_dir, data_root)
            started = time.perf_counter()
            result = run_cli(["init"], env, input_text="2\n")
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if result.returncode != 0:
                raise RuntimeError(
                    f"init failed during {phase} run\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            bucket.append(elapsed_ms)
            run_index += 1

    return {
        "name": "init",
        "command": "init",
        "warmup_runs": warmups,
        "iterations": iterations,
        "warmup_ms": [round(sample, 3) for sample in warmup_samples],
        "samples_ms": [round(sample, 3) for sample in measured_samples],
        "stats": compute_stats(measured_samples),
    }


def prepare_seeded_environment() -> tuple[dict[str, str], dict]:
    shutil.rmtree(BENCHMARK_ROOT, ignore_errors=True)
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    env = base_env(STATE_ROOT, DATA_ROOT)

    init_result = run_cli(["init"], env, input_text="2\n")
    if init_result.returncode != 0:
        raise RuntimeError(f"Failed to initialize benchmark runtime:\n{init_result.stdout}\n{init_result.stderr}")

    os.environ.update({
        "AGENTTRADER_STATE_DIR": str(STATE_ROOT),
        "AGENTTRADER_DATA_ROOT": str(DATA_ROOT),
    })

    config_mod = reload(import_module("agenttrader.config"))
    db_mod = reload(import_module("agenttrader.db"))
    cache_mod = reload(import_module("agenttrader.data.cache"))
    models_mod = import_module("agenttrader.data.models")

    engine = db_mod.get_engine(config_mod.DB_PATH)
    cache = cache_mod.DataCache(engine)
    Market = models_mod.Market
    MarketType = models_mod.MarketType
    Platform = models_mod.Platform
    PricePoint = models_mod.PricePoint

    points_per_market = 14 * 24
    end_dt = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(hours=points_per_market - 1)
    categories = ["politics", "crypto", "sports"]

    polymarket_count = 96
    kalshi_count = 24
    close_time = int((end_dt + timedelta(days=30)).timestamp())

    def seed_market(index: int, platform_name: str, platform_enum) -> None:
        category = categories[index % len(categories)]
        market = Market(
            id=f"{platform_name[:4]}-bench-{index:03d}",
            condition_id=f"{platform_name[:4]}-cond-{index:03d}",
            platform=platform_enum,
            title=f"{platform_name.title()} benchmark market {index:03d}",
            category=category,
            tags=[category, "baseline", "benchmark"],
            market_type=MarketType.BINARY,
            volume=float(50_000 - (index * 123)),
            close_time=close_time,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        )
        cache.upsert_market(market)

        points: list = []
        for hour in range(points_per_market):
            ts = int((start_dt + timedelta(hours=hour)).timestamp())
            wave = math.sin((hour / 12.0) + (index * 0.07))
            drift = ((index % 7) - 3) * 0.002
            yes_price = min(max(0.08, 0.5 + (wave * 0.09) + drift), 0.92)
            points.append(
                PricePoint(
                    timestamp=ts,
                    yes_price=round(yes_price, 4),
                    no_price=round(1.0 - yes_price, 4),
                    volume=round(2_500 + hour + (index * 3), 2),
                )
            )
        cache.upsert_price_points_batch(
            market.id,
            market.platform.value,
            points,
            source="pmxt",
            granularity="1h",
        )

    for index in range(polymarket_count):
        seed_market(index, "polymarket", Platform.POLYMARKET)
    for index in range(kalshi_count):
        seed_market(index, "kalshi", Platform.KALSHI)

    metadata = {
        "state_dir": str(STATE_ROOT),
        "data_root": str(DATA_ROOT),
        "polymarket_markets": polymarket_count,
        "kalshi_markets": kalshi_count,
        "total_markets": polymarket_count + kalshi_count,
        "points_per_market": points_per_market,
        "total_price_points": (polymarket_count + kalshi_count) * points_per_market,
        "seed_start_utc": start_dt.isoformat(),
        "seed_end_utc": end_dt.isoformat(),
    }
    return env, metadata


def main() -> int:
    args = parse_args()
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)

    report: dict[str, object] = {
        "captured_at_utc": datetime.now(tz=UTC).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": sys.version,
            "executable": sys.executable,
        },
        "config": {
            "iterations": args.iterations,
            "warmups": args.warmups,
            "backtest_iterations": args.backtest_iterations,
            "backtest_warmups": args.backtest_warmups,
            "backtest_from": args.backtest_from,
            "backtest_to": args.backtest_to,
        },
    }

    print("Benchmarking init...")
    results: list[dict] = [benchmark_init(args.iterations, args.warmups)]

    env, seed_metadata = prepare_seeded_environment()
    report["seed_data"] = seed_metadata

    command_specs = [
        CommandSpec("validate", ["validate", str(STRATEGY_PATH), "--json"]),
        CommandSpec("markets_list", ["markets", "list", "--platform", "polymarket", "--limit", "100", "--json"]),
        CommandSpec("markets_price", ["markets", "price", BENCHMARK_MARKET_ID, "--json"]),
        CommandSpec("markets_history", ["markets", "history", BENCHMARK_MARKET_ID, "--days", "7", "--json"]),
        CommandSpec(
            "markets_screen",
            [
                "markets",
                "screen",
                "--condition",
                "current_price > 0.45",
                "--platform",
                "polymarket",
                "--limit",
                "20",
                "--json",
            ],
        ),
    ]

    last_run_id: str | None = None
    for spec in command_specs:
        print(f"Benchmarking {spec.name}...")
        results.append(benchmark_command(spec, env, args.iterations, args.warmups))

    real_data_root = args.backtest_data_root.expanduser().resolve()
    real_index_path = real_data_root / "backtest_index.duckdb"
    real_parquet_dir = real_data_root / "data"
    if not real_index_path.exists():
        raise RuntimeError(f"Real backtest index not found at {real_index_path}")
    if not real_parquet_dir.exists():
        raise RuntimeError(f"Real parquet dataset not found at {real_parquet_dir}")

    backtest_env = base_env(STATE_ROOT, real_data_root)
    report["backtest_dataset"] = {
        "mode": "real-normalized-index",
        "data_root": str(real_data_root),
        "index_path": str(real_index_path),
        "parquet_dir": str(real_parquet_dir),
        "from_date": args.backtest_from,
        "to_date": args.backtest_to,
        "fidelity": "exact_trade",
        "strategy": str(STRATEGY_PATH),
    }

    backtest_spec = CommandSpec(
        "backtest_run",
        [
            "backtest",
            str(STRATEGY_PATH),
            "--from",
            args.backtest_from,
            "--to",
            args.backtest_to,
            "--cash",
            "1000",
            "--json",
        ],
    )
    print(f"Benchmarking {backtest_spec.name}...")
    backtest_result = benchmark_command(
        backtest_spec,
        backtest_env,
        args.backtest_iterations,
        args.backtest_warmups,
    )
    payload = backtest_result.get("last_payload") or {}
    last_run_id = payload.get("run_id")
    if not last_run_id:
        raise RuntimeError("Backtest benchmark did not produce a run_id")
    results.append(backtest_result)

    if last_run_id is None:
        raise RuntimeError("Missing run_id for backtest follow-up benchmarks")

    follow_up_specs = [
        CommandSpec("backtest_list", ["backtest", "list", "--json"]),
        CommandSpec("backtest_show", ["backtest", "show", last_run_id, "--json"]),
    ]
    for spec in follow_up_specs:
        print(f"Benchmarking {spec.name}...")
        results.append(benchmark_command(spec, backtest_env, args.iterations, args.warmups))

    report["commands"] = []
    for result in results:
        sanitized = dict(result)
        sanitized.pop("last_payload", None)
        report["commands"].append(sanitized)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    latest_path = BASELINES_DIR / "latest.json"
    latest_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote benchmark baseline to {args.output}")
    for command in report["commands"]:
        stats = command["stats"]
        print(
            f"{command['name']}: mean={stats['mean_ms']} ms "
            f"median={stats['median_ms']} ms min={stats['min_ms']} ms max={stats['max_ms']} ms"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
