"""Clock-aligned poll windows (default 15 minutes).

Server start at 6:03 waits until 6:15, then reads only 6:00–6:15. Mail before
the current slot is left alone — no backfill of old unprocessed mail.
"""

from __future__ import annotations

from datetime import datetime, timezone


def slot_seconds(interval_s: int) -> int:
    return max(60, int(interval_s))


def slot_floor(when: datetime | None = None, interval_s: int = 900) -> int:
    """Unix time of the :00/:15/:30/:45 (or whatever slot) at or before `when`."""
    slot = slot_seconds(interval_s)
    ts = int((when or datetime.now(timezone.utc)).timestamp())
    return (ts // slot) * slot


def next_slot_end(when: datetime | None = None, interval_s: int = 900) -> int:
    """Next clock boundary after `when`. On a boundary, wait a full slot.

    Start 5:00 → first run 5:15 covering 5:00–5:15.
    Start 6:03 → first run 6:15 covering 6:00–6:15.
    """
    slot = slot_seconds(interval_s)
    return slot_floor(when, slot) + slot


def seconds_until_next_slot(when: datetime | None = None, interval_s: int = 900) -> float:
    end = next_slot_end(when, interval_s)
    ts = (when or datetime.now(timezone.utc)).timestamp()
    return max(0.0, end - ts)


def slot_window(end_epoch: int, interval_s: int = 900) -> tuple[int, int]:
    """Closed-open [start, end) unix range ending at `end_epoch`."""
    slot = slot_seconds(interval_s)
    return int(end_epoch) - slot, int(end_epoch)
