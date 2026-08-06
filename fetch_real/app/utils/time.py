from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def ms_to_datetime(ms: int | float | str) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)


def datetime_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)
