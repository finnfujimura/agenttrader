# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import sys

from agenttrader.core.paper_daemon import PaperDaemon


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: paper_daemon_runner <portfolio_id> <strategy_path> <initial_cash>")
    portfolio_id, strategy_path, initial_cash = sys.argv[1], sys.argv[2], float(sys.argv[3])
    daemon = PaperDaemon(portfolio_id, strategy_path, initial_cash)
    daemon._run()


if __name__ == "__main__":
    main()
