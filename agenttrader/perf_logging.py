# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agenttrader.config import APP_DIR


_LOG_LOCK = threading.Lock()
_PROCESS_STARTED_AT = time.time()
_DEFAULT_SESSION_ID = os.environ.get("AGENTTRADER_AGENT_SESSION_ID") or f"agent-{uuid.uuid4().hex[:12]}"


def _resolve_log_path() -> Path:
    override = os.environ.get("AGENTTRADER_PERF_LOG_PATH")
    if override:
        return Path(override).expanduser()
    return APP_DIR / "logs" / "performance.jsonl"


def log_performance_event(
    *,
    source: str,
    operation: str,
    started_at: float,
    duration_ms: float,
    status: str,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Append one performance event to a JSONL log file.

    Logging failures are intentionally swallowed to avoid impacting user flows.
    """
    finished_at = started_at + (duration_ms / 1000.0)
    entry: dict[str, Any] = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "source": source,
        "operation": operation,
        "status": status,
        "error": error,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": round(float(duration_ms), 3),
        "agent_elapsed_ms": int((time.time() - _PROCESS_STARTED_AT) * 1000),
        "session_id": session_id or _DEFAULT_SESSION_ID,
        "pid": os.getpid(),
    }
    if metadata:
        entry["metadata"] = metadata

    try:
        path = _resolve_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str)
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return
