# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import json
import time
import traceback
from functools import wraps
from typing import Any, Callable

import click

from agenttrader.config import is_initialized
from agenttrader.errors import AgentTraderError, NotInitializedError
from agenttrader.perf_logging import log_performance_event


def emit_json(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, default=str))


def ensure_initialized() -> None:
    if not is_initialized():
        raise NotInitializedError()


def json_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started_at = time.time()
        started_perf = time.perf_counter()
        json_output = bool(kwargs.get("json_output", False))
        ctx = click.get_current_context(silent=True)
        operation = ctx.command_path if ctx is not None else func.__name__
        status = "ok"
        error: str | None = None
        try:
            return func(*args, **kwargs)
        except AgentTraderError as exc:
            status = "error"
            error = exc.error
            payload = {"ok": False, "error": exc.error, "message": exc.message}
            if exc.fix:
                payload["fix"] = exc.fix
            payload.update(exc.extra)
            if json_output:
                emit_json(payload)
                raise click.exceptions.Exit(1)
            raise click.ClickException(exc.message)
        except click.ClickException as exc:
            status = "error"
            error = exc.__class__.__name__
            raise
        except Exception as exc:  # pragma: no cover
            status = "error"
            error = exc.__class__.__name__
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
        finally:
            duration_ms = (time.perf_counter() - started_perf) * 1000.0
            log_performance_event(
                source="cli",
                operation=operation,
                started_at=started_at,
                duration_ms=duration_ms,
                status=status,
                error=error,
                metadata={
                    "json_output": json_output,
                    "params": sorted(kwargs.keys()),
                },
            )

    return wrapper
