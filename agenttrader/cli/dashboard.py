# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import click
import uvicorn

from agenttrader.cli.utils import ensure_initialized, json_errors


@click.command("dashboard")
@click.option("--port", type=int, default=8080)
@json_errors
def dashboard_cmd(port: int) -> None:
    ensure_initialized()
    uvicorn.run("agenttrader.dashboard.server:app", host="127.0.0.1", port=port, reload=False)
