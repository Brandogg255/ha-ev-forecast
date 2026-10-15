"""Tests for the cadence-matched history replay that backfill drives.

The point of these tests is agreement with live polling: the replay ticks on
the same interval the coordinator polls on and folds each tick through the same
accumulator, so a long run becomes many hour buckets rather than one.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.ev_forecast.model import (
    FIELD_DRIVE_KWH,
    FIELD_PLUGED_HOURS,
    FIELD_RATE_MAX,
    hour_key,
    replay_history,
)

TZ = timezone(timedelta(hours=11))
START = datetime(2026, 10, 12, 0, 0, tzinfo=TZ)
STEP = timedelta(minutes=5)
CAPACITY = 60.0
RATE = 3.3

# The de wallbox vocabulary, to pin that replay classifies words as live does.
PLUGGED = ("plugged_in", "waiting", "charged", "paused")
UNPLUGGED = ("sleep", "idle")
CHARGING = ("charging",)


def at(hour: int, minute: int = 0) -> datetime:
    return START + timedelta(hours=hour, minutes=minute)


def rows(*pairs: tuple[int, int, str]) -> list[tuple[datetime, str]]:
    """Build a recorder-shaped series: ``(hour, minute, state)`` ascending."""
    return [(at(hour, minute), state) for hour, minute, state in pairs]


def replay(
    soc: list[tuple[datetime, str]],
    connection: list[tuple[datetime, str]],
    *,
    end_hour: int = 8,
    hours: dict[str, dict[str, float]] | None = None,
    **kwargs,
) -> tuple[dict[str, dict[str, float]], int]:
    hours = hours if hours is not None else {}
    written = replay_history(
        hours,
        soc,
        connection,
        start=START,
        end=at(end_hour),
        step=STEP,
        capacity_kwh=CAPACITY,
        charge_rate_kw=RATE,
        **kwargs,
    )
    return hours, written


def plugged_hours(hours: dict[str, dict[str, float]], hour: int) -> float:
    return hours[hour_key(at(hour))][FIELD_PLUGED_HOURS]


def test_six_hour_plugged_run_becomes_six_hour_buckets():
    # One recorder row for a state that never changed: the old transition-based
    # replay credited at most one capped hour, in the hour the run ended. The
    # tolerance is the accumulator's own - it rounds every 5-minute credit to 4
    # decimals, so twelve of them total 0.9996 instead of 1.0. Live polling
    # drifts exactly the same way, and matching it is the point of the replay.
    soc = rows((0, 0, "40.0"))
    connection = rows((0, 0, "on"))
    hours, written = replay(soc, connection)

    for hour in range(1, 8):
        assert plugged_hours(hours, hour) == pytest.approx(1.0, abs=1e-3)
        assert hours[hour_key(at(hour))][FIELD_RATE_MAX] == RATE
    # The first interval only seeds the previous sample, the last tick lands in
    # the bucket it ends, exactly as live polling credits them.
    assert plugged_hours(hours, 0) == pytest.approx(55 / 60, abs=1e-3)
    assert plugged_hours(hours, 8) == pytest.approx(5 / 60, abs=1e-3)
    assert FIELD_DRIVE_KWH not in hours[hour_key(at(4))]
    assert written == len(hours) == 9


def test_discharge_ramp_spreads_over_the_hours_it_spanned():
    soc = rows(
        (0, 0, "40.0"),
        (0, 30, "39.5"),
        (1, 0, "39.0"),
        (1, 30, "38.5"),
        (2, 0, "38.0"),
        (2, 30, "37.5"),
        (3, 0, "37.0"),
    )
    connection = rows((0, 0, "off"))
    hours, _ = replay(soc, connection)

    # 0.5 % of 60 kWh = 0.3 kWh per half hour, credited to the hour each step
    # lands in rather than dumped into the hour the ramp finished.
    assert hours[hour_key(at(0))][FIELD_DRIVE_KWH] == 0.3
    assert hours[hour_key(at(1))][FIELD_DRIVE_KWH] == 0.6
    assert hours[hour_key(at(2))][FIELD_DRIVE_KWH] == 0.6
    assert hours[hour_key(at(3))][FIELD_DRIVE_KWH] == 0.3
    assert sum(bucket.get(FIELD_DRIVE_KWH, 0.0) for bucket in hours.values()) == 1.8
    assert all(FIELD_PLUGED_HOURS not in bucket for bucket in hours.values())


def test_outage_breaks_the_run_instead_of_being_bridged():
    soc = rows((0, 0, "40.0"))
    connection = rows((0, 0, "on"), (2, 0, "unavailable"), (4, 0, "on"))
    hours, _ = replay(soc, connection)

    assert plugged_hours(hours, 0) == pytest.approx(55 / 60, abs=1e-3)
    assert plugged_hours(hours, 1) == pytest.approx(1.0, abs=1e-3)
    # The interval ending at 02:00 was still plugged when it started.
    assert plugged_hours(hours, 2) == pytest.approx(5 / 60, abs=1e-3)
    # Nothing is credited across the outage, and no bucket is invented for it.
    assert hour_key(at(3)) not in hours
    assert plugged_hours(hours, 4) == pytest.approx(55 / 60, abs=1e-3)


def test_replay_never_overwrites_a_bucket_live_already_recorded():
    soc = rows((0, 0, "40.0"), (1, 0, "39.0"), (2, 0, "38.0"))
    connection = rows((0, 0, "off"))
    seeded = {hour_key(at(1)): {FIELD_DRIVE_KWH: 0.5}}
    hours, written = replay(soc, connection, end_hour=2, hours=seeded)

    assert hours[hour_key(at(1))][FIELD_DRIVE_KWH] == 0.5
    assert hours[hour_key(at(2))][FIELD_DRIVE_KWH] == 0.6
    assert hour_key(at(0)) not in hours
    assert written == 1


def test_replay_starts_when_the_entity_first_reported():
    soc = rows((5, 0, "40.0"))
    connection = rows((5, 0, "on"))
    hours, _ = replay(soc, connection, end_hour=6)

    assert hour_key(at(4)) not in hours
    assert plugged_hours(hours, 5) == pytest.approx(55 / 60, abs=1e-3)
    assert plugged_hours(hours, 6) == pytest.approx(5 / 60, abs=1e-3)


def test_word_valued_status_sensor_is_classified_during_replay():
    soc = rows((0, 0, "40.0"))
    connection = rows((0, 0, "plugged_in"), (2, 0, "sleep"), (3, 0, "charging"))
    hours, _ = replay(
        soc,
        connection,
        plugged_states=PLUGGED,
        unplugged_states=UNPLUGGED,
        charging_states=CHARGING,
    )

    assert plugged_hours(hours, 1) == pytest.approx(1.0, abs=1e-3)
    assert plugged_hours(hours, 2) == pytest.approx(5 / 60, abs=1e-3)
    # ``sleep`` credits nothing, and ``charging`` counts as connected again.
    assert plugged_hours(hours, 3) == pytest.approx(55 / 60, abs=1e-3)


def test_replay_of_an_empty_window_writes_nothing():
    hours, written = replay([], [], end_hour=1)
    assert hours == {}
    assert written == 0
