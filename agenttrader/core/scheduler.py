# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from datetime import datetime, timedelta


def next_schedule_time(now: datetime, interval_minutes: int) -> datetime:
    return now + timedelta(minutes=interval_minutes)
