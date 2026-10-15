"""Tests for the hour-bucket accumulation path (live polling + backfill)."""

from datetime import datetime, timedelta, timezone

from custom_components.ev_forecast.model import (
    FIELD_DRIVE_KWH,
    FIELD_PLUGED_HOURS,
    FIELD_RATE_MAX,
    accumulate_interval,
    fill_missing,
    hour_key,
    soc_slope_pct_per_hour,
)

TZ = timezone(timedelta(hours=11))
BASE = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)


def test_first_sample_creates_no_bucket():
    hours: dict[str, dict[str, float]] = {}
    accumulate_interval(hours, None, (BASE, 40.0, False), 60.0, 3.3)
    assert hours == {}


def test_unplugged_discharge_lands_in_the_current_hour_bucket():
    hours: dict[str, dict[str, float]] = {}
    previous = (BASE - timedelta(minutes=5), 40.0, False)
    current = (BASE, 39.6, False)  # 0.4 % of 60 kWh = 0.24 kWh out
    accumulate_interval(hours, previous, current, 60.0, 3.3)
    assert list(hours) == [hour_key(BASE)]
    assert hours[hour_key(BASE)][FIELD_DRIVE_KWH] == 0.24
    assert FIELD_PLUGED_HOURS not in hours[hour_key(BASE)]


def test_plugged_interval_records_hours_and_peak_rate():
    hours: dict[str, dict[str, float]] = {}
    previous = (BASE - timedelta(minutes=5), 40.0, True)
    current = (BASE, 40.4, True)
    accumulate_interval(hours, previous, current, 60.0, 3.3)
    bucket = hours[hour_key(BASE)]
    assert round(bucket[FIELD_PLUGED_HOURS], 4) == round(5 / 60, 4)
    assert bucket[FIELD_RATE_MAX] == 3.3
    assert FIELD_DRIVE_KWH not in bucket  # charging is not drive energy


def test_plugged_hours_never_exceed_one_per_hour():
    hours = {hour_key(BASE): {FIELD_PLUGED_HOURS: 0.9}}
    previous = (BASE - timedelta(minutes=20), 40.0, True)
    accumulate_interval(hours, previous, (BASE, 40.5, True), 60.0, 3.3)
    assert hours[hour_key(BASE)][FIELD_PLUGED_HOURS] == 1.0


def test_long_gap_contributes_nothing():
    """A restart or an offline period must not invent load."""
    hours: dict[str, dict[str, float]] = {}
    previous = (BASE - timedelta(hours=6), 80.0, False)
    accumulate_interval(hours, previous, (BASE, 40.0, False), 60.0, 3.3, max_gap_minutes=45)
    assert hours == {}


def test_backfill_cannot_double_count_a_live_hour():
    hours = {hour_key(BASE): {FIELD_DRIVE_KWH: 0.24}}
    assert fill_missing(hours, hour_key(BASE), FIELD_DRIVE_KWH, 1.5) is False
    assert hours[hour_key(BASE)][FIELD_DRIVE_KWH] == 0.24
    older = hour_key(BASE - timedelta(hours=5))
    assert fill_missing(hours, older, FIELD_DRIVE_KWH, 1.5) is True
    assert hours[older][FIELD_DRIVE_KWH] == 1.5
    assert fill_missing(hours, hour_key(BASE - timedelta(hours=6)), FIELD_DRIVE_KWH, 0.0) is False


def test_soc_slope_ignores_samples_outside_the_window():
    samples = [
        (BASE - timedelta(minutes=30), 30.0),
        (BASE - timedelta(minutes=9), 39.0),
        (BASE - timedelta(minutes=4), 39.5),
        (BASE, 40.0),
    ]
    # Inside the 10 min window (09:50 onward): 39.0 -> 40.0 over 9 min = 6.667 %/h.
    assert round(soc_slope_pct_per_hour(samples, 10, BASE), 3) == 6.667
    assert soc_slope_pct_per_hour([samples[-1]], 10, BASE) == 0.0
