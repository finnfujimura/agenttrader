# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

from datetime import datetime, timedelta


def next_schedule_time(now: datetime, interval_minutes: int) -> datetime:
    return now + timedelta(minutes=interval_minutes)
