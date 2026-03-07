import json
import os
import subprocess
import sys
import tempfile
import time

import pytest


AGENTTRADER_CLI = f"{sys.executable} -m agenttrader.cli.main"
pytestmark = pytest.mark.live


def run(cmd: str) -> dict:
    result = subprocess.run(
        f"{AGENTTRADER_CLI} {cmd} --json",
        shell=True,
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"No JSON output for command: {cmd}\nstderr={result.stderr}")
    return json.loads(result.stdout)


def test_full_agent_workflow():
    subprocess.run(
        f"{AGENTTRADER_CLI} init",
        shell=True,
        check=True,
        input="2\n",
        text=True,
    )

    r = run("sync --days 7 --platform polymarket --limit 10")
    assert r["ok"]
    assert r["markets_synced"] > 0

    r = run("markets list --limit 200")
    assert r["ok"]
    assert len(r["markets"]) > 0
    market_id = None
    for market in r["markets"]:
        probe = run(f"markets history {market['id']} --days 7")
        if probe["ok"] and len(probe["history"]) > 0:
            market_id = market["id"]
            break
    if market_id is None:
        pytest.skip("No markets with price history available from API")

    strategy = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w")
    strategy.write(
        """
from agenttrader import BaseStrategy

class IntegrationTestStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform=\"polymarket\")

    def on_market_data(self, market, price, orderbook):
        if price < 0.5 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=10)
        elif price > 0.6 and self.get_position(market.id):
            self.sell(market.id)
"""
    )
    strategy.close()

    r = run(f"validate {strategy.name}")
    assert r["ok"]
    assert r["valid"]

    r = run(f"backtest {strategy.name} --from 2024-01-01 --to 2024-01-07 --cash 1000")
    assert r["ok"]
    assert "metrics" in r
    assert "equity_curve" not in r
    run_id = r["run_id"]

    r = run(f"backtest show {run_id}")
    assert r["ok"]
    assert "equity_curve" in r
    assert "trades" in r

    r = run(f"paper start {strategy.name} --cash 1000")
    assert r["ok"]
    portfolio_id = r["portfolio_id"]

    time.sleep(3)

    r = run(f"paper status {portfolio_id}")
    assert r["ok"]
    assert r["status"] == "running"

    r = run(f"paper stop {portfolio_id}")
    assert r["ok"]

    os.unlink(strategy.name)
    print("Full integration test PASSED")


if __name__ == "__main__":
    test_full_agent_workflow()
