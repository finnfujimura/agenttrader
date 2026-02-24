# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import ast
from pathlib import Path

import click

from agenttrader.cli.utils import emit_json, json_errors


ALLOWED_SELF_METHODS = {
    "subscribe",
    "search_markets",
    "get_price",
    "get_orderbook",
    "get_history",
    "get_position",
    "get_cash",
    "get_portfolio_value",
    "buy",
    "sell",
    "log",
    "set_state",
    "get_state",
}

FORBIDDEN_IMPORTS = {"requests", "httpx", "aiohttp", "urllib", "dome_api_sdk"}


class StrategyValidator(ast.NodeVisitor):
    def __init__(self, file_path: Path):
        self.file_path = str(file_path)
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.strategy_classes: list[ast.ClassDef] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BaseStrategy":
                self.strategy_classes.append(node)
            elif isinstance(base, ast.Attribute) and base.attr == "BaseStrategy":
                self.strategy_classes.append(node)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                self.warnings.append(
                    {
                        "type": "NetworkImport",
                        "message": f"Import '{alias.name}' detected. Network calls from strategies are not supported.",
                        "file": self.file_path,
                        "line": node.lineno,
                    }
                )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            root = node.module.split(".")[0]
            if root in FORBIDDEN_IMPORTS:
                self.warnings.append(
                    {
                        "type": "NetworkImport",
                        "message": f"Import '{node.module}' detected. Network calls from strategies are not supported.",
                        "file": self.file_path,
                        "line": node.lineno,
                    }
                )

    def validate_structure(self) -> None:
        if len(self.strategy_classes) != 1:
            self.errors.append(
                {
                    "type": "ClassDefinitionError",
                    "message": "File must define exactly one class that subclasses BaseStrategy.",
                    "file": self.file_path,
                    "line": 1,
                }
            )
            return

        cls = self.strategy_classes[0]
        method = None
        for item in cls.body:
            if isinstance(item, ast.FunctionDef) and item.name == "on_market_data":
                method = item
                break

        if method is None:
            self.errors.append(
                {
                    "type": "MissingMethod",
                    "message": "Strategy must implement on_market_data(self, market, price, orderbook).",
                    "file": self.file_path,
                    "line": cls.lineno,
                }
            )
            return

        arg_names = [a.arg for a in method.args.args]
        if arg_names != ["self", "market", "price", "orderbook"]:
            self.errors.append(
                {
                    "type": "InvalidSignature",
                    "message": "on_market_data must accept exactly (self, market, price, orderbook).",
                    "file": self.file_path,
                    "line": method.lineno,
                }
            )

        for node in ast.walk(cls):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == "self":
                    method_name = node.func.attr
                    if method_name not in ALLOWED_SELF_METHODS:
                        self.errors.append(
                            {
                                "type": "InvalidMethodCall",
                                "message": f"Call to undefined method 'self.{method_name}()'. Not in BaseStrategy interface.",
                                "file": self.file_path,
                                "line": node.lineno,
                            }
                        )


def validate_strategy_file(strategy_path: str) -> dict:
    path = Path(strategy_path)
    if not path.exists():
        return {
            "ok": True,
            "valid": False,
            "errors": [
                {
                    "type": "FileNotFoundError",
                    "message": f"Strategy file not found: {strategy_path}",
                    "file": strategy_path,
                    "line": 1,
                }
            ],
            "warnings": [],
        }

    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    validator = StrategyValidator(path)
    validator.visit(tree)
    validator.validate_structure()

    return {
        "ok": True,
        "valid": len(validator.errors) == 0,
        "errors": validator.errors,
        "warnings": validator.warnings,
    }


@click.command("validate")
@click.argument("strategy_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON output")
@json_errors
def validate_cmd(strategy_path: str, json_output: bool) -> None:
    result = validate_strategy_file(strategy_path)
    if json_output:
        emit_json(result)
        return

    if result["valid"]:
        click.echo("Strategy validation passed")
        return

    click.echo("Validation errors:")
    for err in result["errors"]:
        click.echo(f"- {err['type']} line {err['line']}: {err['message']}")
    if result["warnings"]:
        click.echo("Warnings:")
        for warn in result["warnings"]:
            click.echo(f"- {warn['type']} line {warn['line']}: {warn['message']}")
    raise click.exceptions.Exit(1)
