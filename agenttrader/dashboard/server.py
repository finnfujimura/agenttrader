# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agenttrader import __version__
from agenttrader.config import DB_PATH
from agenttrader.data.cache import DataCache
from agenttrader.db import get_engine


app = FastAPI(title="agenttrader dashboard")
cache = DataCache(get_engine())
static_dir = Path(__file__).resolve().parent / "static"

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/api/status")
def api_status():
    markets = cache.get_markets(limit=1_000_000)
    db_size = DB_PATH.stat().st_size / (1024 * 1024) if DB_PATH.exists() else 0.0
    return {"ok": True, "version": __version__, "db_size_mb": round(db_size, 3), "markets_cached": len(markets)}


@app.get("/api/portfolios")
def api_portfolios():
    portfolios = cache.list_paper_portfolios()
    return {
        "ok": True,
        "portfolios": [
            {
                "id": p.id,
                "strategy_path": p.strategy_path,
                "status": p.status,
                "pid": p.pid,
                "started_at": p.started_at,
                "cash_balance": p.cash_balance,
                "initial_cash": p.initial_cash,
                "reload_count": p.reload_count or 0,
                "last_reload": p.last_reload,
            }
            for p in portfolios
        ],
    }


@app.get("/api/portfolios/{portfolio_id}")
def api_portfolio(portfolio_id: str):
    portfolio = cache.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="portfolio not found")

    positions = cache.get_open_positions(portfolio_id)
    trades = cache.get_trades(portfolio_id, limit=500)
    out_positions = []
    for p in positions:
        m = cache.get_market(p.market_id)
        latest = cache.get_latest_price(p.market_id)
        current = latest.yes_price if latest else p.avg_cost
        out_positions.append(
            {
                "market_id": p.market_id,
                "market_title": m.title if m else p.market_id,
                "platform": p.platform,
                "side": p.side,
                "contracts": p.contracts,
                "avg_cost": p.avg_cost,
                "current_price": current,
                "unrealized_pnl": (current - p.avg_cost) * p.contracts,
            }
        )

    return {
        "ok": True,
        "portfolio": {
            "id": portfolio.id,
            "strategy_path": portfolio.strategy_path,
            "status": portfolio.status,
            "pid": portfolio.pid,
            "started_at": portfolio.started_at,
            "cash_balance": portfolio.cash_balance,
            "initial_cash": portfolio.initial_cash,
            "reload_count": portfolio.reload_count or 0,
            "last_reload": portfolio.last_reload,
            "positions": out_positions,
            "trades": [
                {
                    "id": t.id,
                    "market_id": t.market_id,
                    "action": t.action,
                    "side": t.side,
                    "contracts": t.contracts,
                    "price": t.price,
                    "slippage": t.slippage,
                    "filled_at": t.filled_at,
                    "pnl": t.pnl,
                }
                for t in trades
            ],
        },
    }


@app.get("/api/portfolios/{portfolio_id}/logs")
def api_portfolio_logs(portfolio_id: str):
    return {"ok": True, "logs": cache.get_logs(portfolio_id, limit=100)}


@app.get("/api/backtests")
def api_backtests():
    rows = cache.list_backtest_runs(limit=500)
    payload = []
    for row in rows:
        metrics = {}
        final_value = None
        if row.results_json:
            data = json.loads(row.results_json)
            metrics = data.get("metrics", {})
            final_value = data.get("final_value")
        payload.append(
            {
                "id": row.id,
                "strategy_path": row.strategy_path,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "status": row.status,
                "created_at": row.created_at,
                "completed_at": row.completed_at,
                "metrics": metrics,
                "final_value": final_value,
            }
        )
    return {"ok": True, "runs": payload}


@app.get("/api/backtests/{run_id}")
def api_backtest(run_id: str):
    row = cache.get_backtest_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="backtest not found")
    if row.results_json:
        data = json.loads(row.results_json)
        data["ok"] = True
        return data
    return {
        "ok": True,
        "run_id": row.id,
        "strategy_path": row.strategy_path,
        "status": row.status,
        "error": row.error,
    }


@app.get("/api/markets")
def api_markets(limit: int = Query(default=500, ge=1, le=5000)):
    markets = cache.get_markets(limit=limit)
    return {
        "ok": True,
        "markets": [
            {
                "id": m.id,
                "platform": m.platform.value,
                "title": m.title,
                "category": m.category,
                "volume": m.volume,
                "close_time": m.close_time,
                "resolved": m.resolved,
                "price": (cache.get_latest_price(m.id).yes_price if cache.get_latest_price(m.id) else None),
            }
            for m in markets
        ],
    }


@app.get("/api/markets/{market_id}/history")
def api_market_history(market_id: str, days: int = Query(default=7, ge=1, le=365)):
    import time

    end = int(time.time())
    start = end - days * 24 * 3600
    history = cache.get_price_history(market_id, start, end)
    return {
        "ok": True,
        "market_id": market_id,
        "days": days,
        "history": [
            {
                "timestamp": p.timestamp,
                "yes_price": p.yes_price,
                "no_price": p.no_price,
                "volume": p.volume,
            }
            for p in history
        ],
    }


@app.get("/{path:path}")
def spa(path: str):
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    raise HTTPException(status_code=404, detail="frontend not built")
