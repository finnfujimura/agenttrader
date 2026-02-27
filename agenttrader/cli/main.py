# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import asyncio

import click

from agenttrader.cli.backtest import backtest_cmd
from agenttrader.cli.config import config_group, init_cmd
from agenttrader.cli.dataset import dataset_group
from agenttrader.cli.dashboard import dashboard_cmd
from agenttrader.cli.experiments import experiments_group
from agenttrader.cli.markets import markets_group
from agenttrader.cli.paper import paper_group
from agenttrader.cli.prune import prune_cmd
from agenttrader.cli.sync import sync_cmd
from agenttrader.cli.validate import validate_cmd


@click.group()
def cli():
    """agenttrader — prediction market strategy platform"""


@cli.command("mcp")
def mcp_cmd() -> None:
    """Start the MCP server on stdio transport."""
    from agenttrader.mcp.server import main as mcp_main

    asyncio.run(mcp_main())


cli.add_command(init_cmd)
cli.add_command(config_group)
cli.add_command(dataset_group)
cli.add_command(sync_cmd)
cli.add_command(markets_group)
cli.add_command(validate_cmd)
cli.add_command(backtest_cmd)
cli.add_command(paper_group)
cli.add_command(experiments_group)
cli.add_command(dashboard_cmd)
cli.add_command(prune_cmd)


if __name__ == "__main__":
    cli()
