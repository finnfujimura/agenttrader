# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import gzip
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import msgpack

from agenttrader.data.models import OrderBook, OrderLevel


class OrderBookStore:
    def __init__(self, base_path: Path | None = None):
        if base_path is None:
            base_path = Path.home() / ".agenttrader" / "orderbooks"
        self.base_path = base_path

    def write(self, platform: str, market_id: str, snapshots: list[OrderBook]) -> int:
        grouped: dict[str, dict[int, dict]] = defaultdict(dict)
        for snap in snapshots:
            day = datetime.fromtimestamp(snap.timestamp, tz=UTC).strftime("%Y-%m-%d")
            grouped[day][snap.timestamp] = {
                "ts": int(snap.timestamp),
                "bids": [[float(level.price), float(level.size)] for level in snap.bids],
                "asks": [[float(level.price), float(level.size)] for level in snap.asks],
            }

        files_written = 0
        for day, by_ts in grouped.items():
            path = self._file_path(platform, market_id, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = self._read_raw(path)
            merged = {item["ts"]: item for item in existing}
            merged.update(by_ts)
            payload = [merged[k] for k in sorted(merged)]
            with gzip.open(path, "wb") as fh:
                fh.write(msgpack.packb(payload, use_bin_type=True))
            files_written += 1
        return files_written

    def read(
        self,
        platform: str,
        market_id: str,
        start_ts: int,
        end_ts: int,
    ) -> list[OrderBook]:
        days = self._days_between(start_ts, end_ts)
        out: list[OrderBook] = []
        for day in days:
            path = self._file_path(platform, market_id, day)
            for item in self._read_raw(path):
                ts = int(item["ts"])
                if start_ts <= ts <= end_ts:
                    out.append(self._to_orderbook(market_id, item))
        out.sort(key=lambda x: x.timestamp)
        return out

    def get_nearest(self, platform: str, market_id: str, ts: int) -> OrderBook | None:
        # Simple robust lookup: scan available files for this market and return closest timestamp.
        root = self.base_path / platform / market_id
        if not root.exists():
            return None

        all_items: list[OrderBook] = []
        for path in sorted(root.glob("*.msgpack.gz")):
            for item in self._read_raw(path):
                all_items.append(self._to_orderbook(market_id, item))

        if not all_items:
            return None
        return min(all_items, key=lambda x: abs(x.timestamp - ts))

    def get_latest(self, platform: str, market_id: str) -> OrderBook | None:
        root = self.base_path / platform / market_id
        if not root.exists():
            return None
        latest: OrderBook | None = None
        for path in sorted(root.glob("*.msgpack.gz")):
            for item in self._read_raw(path):
                ob = self._to_orderbook(market_id, item)
                if latest is None or ob.timestamp > latest.timestamp:
                    latest = ob
        return latest

    def prune(self, older_than_ts: int, dry_run: bool = False) -> int:
        count = 0
        if not self.base_path.exists():
            return 0
        for file in self.base_path.rglob("*.msgpack.gz"):
            day = file.stem.replace(".msgpack", "")
            try:
                file_ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
            except ValueError:
                continue
            if file_ts < older_than_ts:
                count += 1
                if not dry_run:
                    file.unlink(missing_ok=True)
        return count

    def _file_path(self, platform: str, market_id: str, day: str) -> Path:
        # Sanitize path components to prevent directory traversal
        safe_platform = Path(platform).name
        safe_market_id = Path(market_id).name
        path = self.base_path / safe_platform / safe_market_id / f"{day}.msgpack.gz"
        if not path.resolve().is_relative_to(self.base_path.resolve()):
            raise ValueError(f"Path traversal detected: {platform}/{market_id}")
        return path

    def _read_raw(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            with gzip.open(path, "rb") as fh:
                payload = fh.read()
                if not payload:
                    return []
                if len(payload) > 100 * 1024 * 1024:  # 100MB limit
                    return []
                try:
                    return msgpack.unpackb(payload, raw=False, max_buffer_size=100 * 1024 * 1024)
                except TypeError:
                    # Older msgpack versions don't support max_buffer_size.
                    return msgpack.unpackb(payload, raw=False)
        except (msgpack.UnpackValueError, msgpack.ExtraData, ValueError, EOFError):
            return []  # skip corrupted files

    @staticmethod
    def _to_orderbook(market_id: str, raw: dict) -> OrderBook:
        return OrderBook(
            market_id=market_id,
            timestamp=int(raw["ts"]),
            bids=[OrderLevel(price=float(p), size=float(s)) for p, s in raw.get("bids", [])],
            asks=[OrderLevel(price=float(p), size=float(s)) for p, s in raw.get("asks", [])],
        )

    @staticmethod
    def _days_between(start_ts: int, end_ts: int) -> list[str]:
        start = datetime.fromtimestamp(start_ts, tz=UTC).date()
        end = datetime.fromtimestamp(end_ts, tz=UTC).date()
        days = []
        cur = start
        while cur <= end:
            days.append(cur.strftime("%Y-%m-%d"))
            cur = cur.fromordinal(cur.toordinal() + 1)
        return days
