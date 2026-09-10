from datetime import datetime, timezone

from app.schedule import next_slot_end, seconds_until_next_slot, slot_floor, slot_window


def _utc(h, m, s=0) -> datetime:
    return datetime(2026, 9, 10, h, m, s, tzinfo=timezone.utc)


def test_start_at_hour_waits_full_slot():
    now = _utc(17, 0, 0)
    end = next_slot_end(now, 900)
    assert end == int(_utc(17, 15).timestamp())
    assert slot_window(end, 900) == (int(_utc(17, 0).timestamp()), end)


def test_start_at_603_runs_at_615_covering_600_to_615():
    now = _utc(18, 3, 0)
    end = next_slot_end(now, 900)
    start, stop = slot_window(end, 900)
    assert end == int(_utc(18, 15).timestamp())
    assert start == int(_utc(18, 0).timestamp())
    assert stop == end
    assert 11 * 60 < seconds_until_next_slot(now, 900) <= 12 * 60


def test_start_at_548_runs_at_600_covering_545_to_600():
    now = _utc(17, 48, 0)
    end = next_slot_end(now, 900)
    start, stop = slot_window(end, 900)
    assert end == int(_utc(18, 0).timestamp())
    assert start == int(_utc(17, 45).timestamp())
    assert stop == end


def test_slot_floor_snaps_to_quarter():
    assert slot_floor(_utc(18, 3), 900) == int(_utc(18, 0).timestamp())
    assert slot_floor(_utc(18, 15), 900) == int(_utc(18, 15).timestamp())
    assert slot_floor(_utc(18, 29, 59), 900) == int(_utc(18, 15).timestamp())
