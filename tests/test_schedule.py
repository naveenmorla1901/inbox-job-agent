from datetime import datetime, timezone

from app.schedule import next_tick_epoch, seconds_until_next_tick, tick_window


def _utc(h, m, s=0) -> datetime:
    return datetime(2026, 9, 10, h, m, s, tzinfo=timezone.utc)


def test_boot_at_603_first_run_is_618():
    origin = _utc(18, 3, 0)
    now = origin
    end = next_tick_epoch(int(origin.timestamp()), 900, now)
    start, stop = tick_window(end, int(origin.timestamp()), 900)
    assert end == int(_utc(18, 18).timestamp())
    assert start == int(origin.timestamp())
    assert stop == end
    assert seconds_until_next_tick(int(origin.timestamp()), 900, now) == 900


def test_mid_window_points_at_next_boundary():
    origin = _utc(18, 3, 0)
    now = _utc(18, 10, 0)
    end = next_tick_epoch(int(origin.timestamp()), 900, now)
    assert end == int(_utc(18, 18).timestamp())
    assert 7 * 60 <= seconds_until_next_tick(int(origin.timestamp()), 900, now) <= 8 * 60


def test_after_first_tick_covers_second_window():
    origin = _utc(17, 48, 0)
    end = next_tick_epoch(int(origin.timestamp()), 900, _utc(18, 4, 0))
    start, stop = tick_window(end, int(origin.timestamp()), 900)
    assert start == int(_utc(18, 3).timestamp())
    assert stop == int(_utc(18, 18).timestamp())
