# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import gzip

import msgpack

from agenttrader.config import ARTIFACTS_DIR


def write_backtest_artifact(run_id: str, equity_curve: list, trades: list) -> str:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"{run_id}.msgpack.gz"
    payload = msgpack.packb({"equity_curve": equity_curve, "trades": trades}, use_bin_type=True)
    with gzip.open(path, "wb") as f:
        f.write(payload)
    return str(path)


def read_backtest_artifact(run_id: str) -> dict:
    path = ARTIFACTS_DIR / f"{run_id}.msgpack.gz"
    if not path.exists():
        return {"equity_curve": [], "trades": []}
    with gzip.open(path, "rb") as f:
        return msgpack.unpackb(f.read(), raw=False)
