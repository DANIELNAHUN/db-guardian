from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Current time as naive UTC (the storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_display(dt: datetime | None, tz: str) -> str | None:
    """Render a stored naive-UTC datetime as ISO-8601 in the display timezone."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(ZoneInfo(tz)).isoformat()


def from_client(dt: datetime, tz: str) -> datetime:
    """Convert a client datetime to naive UTC; naive input is read in the display timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz))
    return dt.astimezone(UTC).replace(tzinfo=None)
