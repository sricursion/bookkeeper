"""Time helpers. Everything is stored as integer epoch seconds; the merchant's
day and quiet hours are reckoned in IST, which has no daylight saving."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def parse_ts(value: int | float | str) -> int:
    """Accept epoch seconds or an ISO 8601 string. Naive strings are read as IST."""
    if isinstance(value, bool):
        raise TypeError("timestamp cannot be a boolean")
    if isinstance(value, (int, float)):
        return int(value)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return int(parsed.timestamp())


def ist(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=IST)


def ist_day_bounds(epoch: int) -> tuple[int, int]:
    """Start and end (exclusive) of the IST calendar day containing `epoch`."""
    local = ist(epoch)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def fmt(epoch: int) -> str:
    return ist(epoch).strftime("%Y-%m-%d %H:%M IST")
