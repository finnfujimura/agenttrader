# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import logging
import sys

from agenttrader.core.paper_daemon import PaperDaemon

LOGGER = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: paper_daemon_runner <portfolio_id> <strategy_path> <initial_cash>")
    portfolio_id, strategy_path, initial_cash = sys.argv[1], sys.argv[2], float(sys.argv[3])
    daemon = PaperDaemon(portfolio_id, strategy_path, initial_cash)
    try:
        daemon._run()
    except Exception:
        LOGGER.exception("Daemon runner crash for portfolio %s", portfolio_id)
        try:
            from agenttrader.db import get_engine, get_session
            from agenttrader.db.schema import PaperPortfolio

            with get_session(get_engine()) as session:
                row = session.get(PaperPortfolio, portfolio_id)
                if row and row.status == "running":
                    row.status = "failed"
                    session.commit()
        except Exception:
            LOGGER.exception("Failed to mark portfolio %s as failed in runner", portfolio_id)
        raise


if __name__ == "__main__":
    main()
