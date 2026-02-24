# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import json
import traceback
from functools import wraps
from typing import Any, Callable

import click

from agenttrader.config import is_initialized
from agenttrader.errors import AgentTraderError, NotInitializedError


def emit_json(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, default=str))


def ensure_initialized() -> None:
    if not is_initialized():
        raise NotInitializedError()


def json_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        json_output = bool(kwargs.get("json_output", False))
        try:
            return func(*args, **kwargs)
        except AgentTraderError as exc:
            payload = {"ok": False, "error": exc.error, "message": exc.message}
            payload.update(exc.extra)
            if json_output:
                emit_json(payload)
                raise click.exceptions.Exit(1)
            raise click.ClickException(exc.message)
        except click.ClickException:
            raise
        except Exception as exc:  # pragma: no cover
            if json_output:
                emit_json(
                    {
                        "ok": False,
                        "error": exc.__class__.__name__,
                        "message": str(exc),
                        "traceback": traceback.format_exc(),
                    }
                )
                raise click.exceptions.Exit(1)
            raise

    return wrapper
