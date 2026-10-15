"""Unit tests for the pure EV usage model.

Numbers are taken from the live GAC Aion UT instance where possible so the
model is pinned against observed behaviour rather than tautologies.
"""

from datetime import datetime, timedelta, timezone

from custom_components.ev_forecast.model import (
    FIELD_DRIVE_KWH,
    FIELD_PLUGED_HOURS,
    FIELD_RATE_MAX,
    daily_total,
    drive_energy_kwh,
    drive_power_kw,
    historical_forecast,
    hour_key,
    merge_planned_trips,
    prune_samples,
    required_soc_pct,
    slot_averages,
    soc_power_kw,
    window_total,
)

TZ = timezone(timedelta(hours=11))
BASE = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)  # a Thursday


def samples_from(hours: dict[int, dict[str, float]], day_offset: int = 0) -> dict[str, dict[str, float]]:
    out = {}
    for hour, record in hours.items():
        stamp = (BASE + timedelta(days=day_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        out[hour_key(stamp)] = record
    return out


def build_window(start: datetime, hours: int, values: dict[int, float]) -> list[tuple[datetime, float]]:
    """Hourly forecast whose hour ``i`` carries ``values.get(i, 0.0)``."""
    return [(start + timedelta(hours=i + 1), values.get(i + 1, 0.0)) for i in range(hours)]


# --- SoC -> power / energy -------------------------------------------------


def test_soc_power_matches_observed_charge_rate():
    # Live: SoC climbed 4.772 %/h on a 60 kWh pack while the car drew 3.44 kW AC.
    # 5 min of that slope is 0.3977 % -> 2.863 kW into the pack.
    assert round(soc_power_kw(40.0, 40.3977, 5, 60.0), 3) == 2.863


def test_soc_power_negative_when_driving_and_zero_window_guarded():
    assert round(soc_power_kw(39.8, 39.1, 10, 60.0), 6) == -2.52
    assert soc_power_kw(39.8, 39.1, 0, 60.0) == 0.0
    assert soc_power_kw(39.8, 39.1, 10, 0.0) == 0.0


def test_drive_power_only_counts_energy_leaving_the_pack():
    assert drive_power_kw(-2.52) == 2.52
    assert drive_power_kw(2.866) == 0.0
    assert drive_power_kw(0.0) == 0.0


def test_drive_energy_over_observed_unplugged_window():
    # Recorder window 10:06 -> 10:16 local: SoC 39.5 -> 39.1 on a 60 kWh pack.
    assert round(drive_energy_kwh(39.5, 39.1, 10, 60.0), 3) == 0.24
    assert drive_energy_kwh(39.1, 39.5, 10, 60.0) == 0.0


# --- historical-shift forecaster ------------------------------------------


def test_forecast_averages_an_observed_hour_across_history_days():
    samples = {}
    samples.update(samples_from({17: {FIELD_DRIVE_KWH: 6.0}}, day_offset=-2))  # Tue
    samples.update(samples_from({17: {FIELD_DRIVE_KWH: 4.0}}, day_offset=-1))  # Wed
    fc = historical_forecast(samples, FIELD_DRIVE_KWH, BASE, 24, history_days=7)
    values = {stamp.hour: value for stamp, value in fc}
    assert values[17] == 5.0  # hour-of-day mean, since neither history day is a Thursday
    assert values[3] == 0.0  # no history for 03:00 -> no invented load


def test_forecast_is_weekday_aware_by_default():
    # Mondays at 08:00 carry a 10 kWh commute; Saturdays at 08:00 carry nothing.
    samples = {}
    for offset, kwh in ((-3, 10.0), (-10, 10.0)):
        stamp = (BASE + timedelta(days=offset)).replace(hour=8, minute=0, second=0, microsecond=0)
        assert stamp.weekday() == 0
        samples[hour_key(stamp)] = {FIELD_DRIVE_KWH: kwh}
    for offset in (-5, -12):
        stamp = (BASE + timedelta(days=offset)).replace(hour=8, minute=0, second=0, microsecond=0)
        assert stamp.weekday() == 5
        samples[hour_key(stamp)] = {FIELD_DRIVE_KWH: 0.0}
    weekly = slot_averages(samples, FIELD_DRIVE_KWH, history_days=30, now=BASE, weekday_aware=True)
    daily = slot_averages(samples, FIELD_DRIVE_KWH, history_days=30, now=BASE, weekday_aware=False)
    assert weekly["0-8"] == 10.0
    assert weekly["5-8"] == 0.0
    assert daily["8"] == 5.0  # hour-of-day would blur the two patterns together


def test_half_filled_current_hour_is_excluded_from_history():
    samples = {hour_key(BASE): {FIELD_DRIVE_KWH: 99.0}}
    fc = historical_forecast(samples, FIELD_DRIVE_KWH, BASE, 24, history_days=7)
    assert all(value == 0.0 for _, value in fc)


def test_rate_bucket_records_the_peak_rate():
    samples = samples_from({10: {FIELD_RATE_MAX: 3.3, FIELD_PLUGED_HOURS: 1.0}}, day_offset=-1)
    fc = historical_forecast(samples, FIELD_RATE_MAX, BASE, 24, history_days=7)
    values = {stamp.hour: value for stamp, value in fc}
    assert values[10] == 3.3


def test_planned_trip_is_added_to_the_drive_forecast():
    fc = build_window(BASE, 24, {})
    trips = [{"date": "2026-10-15", "start_hour": 16, "hours": 2, "kwh": 12.0}]
    merged = merge_planned_trips(fc, trips)
    values = {stamp.hour: value for stamp, value in merged}
    assert values[16] == 6.0
    assert values[17] == 6.0
    assert values[15] == 0.0


def test_malformed_planned_trip_is_ignored():
    fc = [(BASE + timedelta(hours=1), 1.0)]
    assert merge_planned_trips(fc, [{"date": "not-a-date", "start_hour": 1, "kwh": 5}]) == fc


# --- required SoC rule ----------------------------------------------------


def test_floor_covers_a_single_trip_plus_buffer():
    # 24 kWh trip over hours 7-8, charge window hours 20-26 at 3.3 kW.
    start = BASE
    floor = required_soc_pct(
        current_rate_kw=3.3,
        rate_forecast=build_window(start, 168, {i: 3.3 for i in range(20, 27)}),
        drive_forecast=build_window(start, 168, {7: 12.0, 8: 12.0}),
        plugged_forecast=build_window(start, 168, {i: 1.0 for i in range(20, 27)}),
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
    )
    assert floor == 48.3  # (24 + 5) / 60 * 100


def test_floor_accounts_for_several_days_of_trips_without_charging():
    # 10 kWh on each of three consecutive days, no plugged window at all.
    start = BASE
    floor = required_soc_pct(
        current_rate_kw=0.0,
        rate_forecast=build_window(start, 168, {}),
        drive_forecast=build_window(start, 168, {8: 10.0, 32: 10.0, 56: 10.0}),
        plugged_forecast=build_window(start, 168, {}),
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
    )
    assert floor == 58.3  # (30 + 5) / 60 * 100 -> the multi-day rule, not per-trip


def test_empty_forecasts_give_no_floor():
    floor = required_soc_pct(
        current_rate_kw=0.0,
        rate_forecast=[],
        drive_forecast=[],
        plugged_forecast=[],
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
    )
    assert floor == 0.0


def test_missing_hours_fall_back_to_pattern_mean_not_present_value():
    """Regression for the runaway-floor bug on the live instance.

    The present instantaneous standby drain (1.44 kW) must never be
    extrapolated across the horizon: uncovered hours use the mean of the
    forecast pattern instead.
    """
    start = BASE
    drive = build_window(start, 24, {i: 1.0 for i in range(1, 13)})  # 1 kW for 12 h
    floor = required_soc_pct(
        current_rate_kw=1.44,
        rate_forecast=build_window(start, 24, {}),
        drive_forecast=drive,
        plugged_forecast=build_window(start, 24, {}),
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
    )
    # 12 kWh inside the 24 h covered window, then 48 uncovered hours at the
    # 0.5 kW pattern mean = 24 kWh, and plugged mean is 0 so nothing pays it back.
    assert floor == 68.3  # (12 + 24 + 5) / 60 * 100


def test_floor_is_capped():
    start = BASE
    floor = required_soc_pct(
        current_rate_kw=0.0,
        rate_forecast=build_window(start, 168, {}),
        drive_forecast=build_window(start, 168, {i: 20.0 for i in range(1, 25)}),
        plugged_forecast=build_window(start, 168, {}),
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
        max_soc_pct=90.0,
    )
    assert floor == 90.0


def test_plugged_window_pays_down_the_deficit():
    start = BASE
    floor = required_soc_pct(
        current_rate_kw=3.3,
        rate_forecast=build_window(start, 168, {i: 3.3 for i in range(1, 13)}),
        drive_forecast=build_window(start, 168, {30: 24.0}),
        plugged_forecast=build_window(start, 168, {i: 1.0 for i in range(1, 13)}),
        capacity_kwh=60,
        efficiency_pct=85,
        buffer_kwh=5,
    )
    assert floor == 8.3  # 12 h * 3.3 kW * 0.85 = 33.7 kWh covers the 24 kWh trip


def test_zero_capacity_guard():
    assert (
        required_soc_pct(
            current_rate_kw=3.3,
            rate_forecast=build_window(BASE, 24, {}),
            drive_forecast=build_window(BASE, 24, {1: 5.0}),
            plugged_forecast=build_window(BASE, 24, {}),
            capacity_kwh=0,
            efficiency_pct=85,
            buffer_kwh=5,
        )
        == 0.0
    )


# --- store helpers --------------------------------------------------------


def test_daily_total_sums_only_the_requested_day():
    samples = samples_from({8: {FIELD_DRIVE_KWH: 1.0}, 18: {FIELD_DRIVE_KWH: 2.0}}, day_offset=0)
    samples.update(samples_from({9: {FIELD_DRIVE_KWH: 4.0}}, day_offset=-1))
    assert daily_total(samples, FIELD_DRIVE_KWH, BASE) == 3.0


def test_prune_samples_drops_old_buckets():
    samples = samples_from({8: {FIELD_DRIVE_KWH: 1.0}}, day_offset=-10)
    samples.update(samples_from({9: {FIELD_DRIVE_KWH: 1.0}}, day_offset=0))
    kept = prune_samples(samples, BASE - timedelta(days=3))
    assert len(kept) == 1


def test_window_total_sums_every_retained_bucket():
    samples = samples_from({8: {FIELD_DRIVE_KWH: 1.0}}, day_offset=0)
    samples.update(samples_from({9: {FIELD_DRIVE_KWH: 4.0}}, day_offset=-3))
    assert window_total(samples, FIELD_DRIVE_KWH) == 5.0
