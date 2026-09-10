"""Poll windows measured from process/revision start, not the wall clock.

Boot at 6:03 → skip mail before 6:03 → first extract one interval later.
Same revision waking from Cloud Run sleep keeps that origin so idle time is not dropped.
"""

from __future__ import annotations

from datetime import datetime, timezone


def slot_seconds(interval_s: int) -> int:
    return max(60, int(interval_s))


def next_tick_epoch(origin_epoch: int, interval_s: int, when: datetime | None = None) -> int:
    """Unix time of the next interval boundary after `when`, counted from `origin_epoch`."""
    slot = slot_seconds(interval_s)
    origin = int(origin_epoch)
    ts = (when or datetime.now(timezone.utc)).timestamp()
    if ts <= origin:
        return origin + slot
    n = int((ts - origin) // slot) + 1
    return origin + n * slot


def seconds_until_next_tick(
    origin_epoch: int, interval_s: int, when: datetime | None = None
) -> float:
    end = next_tick_epoch(origin_epoch, interval_s, when)
    ts = (when or datetime.now(timezone.utc)).timestamp()
    return max(0.0, end - ts)


def tick_window(end_epoch: int, origin_epoch: int, interval_s: int) -> tuple[int, int]:
    """Closed-open [start, end) unix range ending at `end_epoch`."""
    slot = slot_seconds(interval_s)
    origin = int(origin_epoch)
    end = int(end_epoch)
    start = max(origin, end - slot)
    return start, end
