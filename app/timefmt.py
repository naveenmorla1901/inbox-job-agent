"""Display timestamps in US Eastern time (ET), not UTC."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def as_et(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EASTERN)


def parse_et_datetime(raw: str) -> datetime | None:
    """Parse `YYYY-MM-DDTHH:MM` as US Eastern when no timezone is given."""
    if not (raw or "").strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=EASTERN)
    return value.astimezone(timezone.utc)


def fmt_et(value: datetime | None, fmt: str = "%b %d, %I:%M %p ET") -> str:
    local = as_et(value)
    return local.strftime(fmt) if local else ""


def et_day_label(value: datetime | None) -> str:
    local = as_et(value)
    return local.strftime("%A, %b %d") if local else ""


def group_by_et_day(rows: list, attr: str = "received_at") -> list[tuple[str, list]]:
    """Keep newest-first order; split into Eastern calendar days."""
    groups: list[tuple[str, list]] = []
    for row in rows:
        label = et_day_label(getattr(row, attr, None))
        if not groups or groups[-1][0] != label:
            groups.append((label, [row]))
        else:
            groups[-1][1].append(row)
    return groups
