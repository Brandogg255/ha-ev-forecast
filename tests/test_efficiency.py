"""Tests for the lifetime consumption figure (kWh/100km).

Pure model rules only: the seed, the per-sample measurement, the guards that
keep charging and parked drain out of the average, and the store round trip.
"""

from datetime import datetime, timedelta, timezone

from custom_components.ev_forecast.model import (
    FIELD_AVG_KWH_100KM,
    FIELD_SEED_KWH_100KM,
    FIELD_TRACKED_KM,
    FIELD_TRACKED_KWH,
    EfficiencyState,
    add_efficiency_sample,
    efficiency_from_store,
    efficiency_sample_kwh_100km,
    seed_efficiency,
    seed_prior_km,
)

TZ = timezone(timedelta(hours=11))
BASE = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)


def sample(previous_minutes: int, previous_soc: float, previous_km: float, soc: float, km: float):
    """One interval: SoC/odometer pair 5 min apart by default."""
    return efficiency_sample_kwh_100km(
        (BASE - timedelta(minutes=previous_minutes), previous_soc, previous_km),
        (BASE, soc, km),
        60.0,
    )


# --- seeding ----------------------------------------------------------------

def test_seed_becomes_one_pack_of_prior_distance():
    """A bare average carries no distance, so the prior is one full pack."""
    state = seed_efficiency(15.0, 60.0)
    assert state.avg_kwh_100km == 15.0
    assert state.tracked_km == 400.0  # 60 kWh at 15 kWh/100km
    assert state.tracked_kwh == 60.0  # exactly one pack
    assert state.seed_kwh_100km == 15.0
    assert seed_prior_km(15.0, 60.0) == 400.0


def test_seed_below_one_pack_is_worth_more_distance():
    state = seed_efficiency(10.0, 60.0)
    assert state.tracked_km == 600.0
    assert state.tracked_kwh == 60.0


def test_unusable_seed_gives_an_empty_figure():
    for seed, capacity in ((0.0, 60.0), (-5.0, 60.0), (15.0, 0.0), (float("inf"), 60.0)):
        state = seed_efficiency(seed, capacity)
        assert (state.avg_kwh_100km, state.tracked_km, state.tracked_kwh) == (0.0, 0.0, 0.0)


def test_state_round_trips_through_a_dict():
    state = EfficiencyState(17.25, 512.5, 88.4, 15.0)
    record = state.as_dict()
    assert record == {
        FIELD_AVG_KWH_100KM: 17.25,
        FIELD_TRACKED_KM: 512.5,
        FIELD_TRACKED_KWH: 88.4,
        FIELD_SEED_KWH_100KM: 15.0,
    }
    assert efficiency_from_store(record, 15.0, 60.0) == state


# --- one measured sample ----------------------------------------------------

def test_discharge_over_distance_gives_kwh_per_100km():
    # 0.6 % of a 60 kWh pack = 0.36 kWh over 4 km -> 9 kWh/100km.
    assert sample(5, 40.0, 1000.0, 39.4, 1004.0) == 9.0


def test_first_sample_has_nothing_to_compare_against():
    assert efficiency_sample_kwh_100km(None, (BASE, 39.4, 1004.0), 60.0) is None


def test_charging_is_not_consumption():
    assert sample(5, 40.0, 1000.0, 41.0, 1004.0) is None


def test_flat_soc_over_moving_wheels_is_not_a_sample():
    assert sample(5, 40.0, 1000.0, 40.0, 1004.0) is None


def test_parked_drain_moves_no_distance():
    """Vampire drain drops SoC while the odometer stands still."""
    assert sample(30, 40.0, 1000.0, 39.0, 1000.0) is None


def test_rewound_or_replaced_odometer_is_rejected():
    assert sample(5, 40.0, 1004.0, 39.4, 1000.0) is None  # negative delta
    assert sample(5, 40.0, 1000.0, 39.4, 1000.0) is None  # zero delta


def test_distance_below_the_threshold_is_noise():
    assert sample(5, 40.0, 1000.0, 39.4, 1000.1) is None
    assert (
        efficiency_sample_kwh_100km(
            (BASE - timedelta(minutes=5), 40.0, 1000.0),
            (BASE, 39.4, 1000.1),
            60.0,
            min_distance_km=0.05,
        )
        == 360.0
    )


def test_gap_longer_than_the_poll_window_is_rejected():
    assert sample(46, 40.0, 1000.0, 39.4, 1004.0) is None
    assert sample(45, 40.0, 1000.0, 39.4, 1004.0) == 9.0


def test_backwards_or_zero_elapsed_time_is_rejected():
    assert (
        efficiency_sample_kwh_100km(
            (BASE, 40.0, 1000.0), (BASE, 39.4, 1004.0), 60.0
        )
        is None
    )
    assert (
        efficiency_sample_kwh_100km(
            (BASE + timedelta(minutes=5), 40.0, 1000.0), (BASE, 39.4, 1004.0), 60.0
        )
        is None
    )


def test_non_finite_reading_is_rejected():
    for soc, km in ((float("inf"), 1004.0), (float("nan"), 1004.0), (39.4, float("inf"))):
        assert sample(5, 40.0, 1000.0, soc, km) is None


# --- folding samples in -----------------------------------------------------

def test_samples_are_weighted_by_distance():
    state = EfficiencyState(0.0, 0.0, 0.0, 0.0)
    state = add_efficiency_sample(state, 20.0, 20.0)
    assert state.avg_kwh_100km == 20.0
    state = add_efficiency_sample(state, 10.0, 10.0)
    # 20 km at 20 plus 10 km at 10 = 5 kWh over 30 km.
    assert state.avg_kwh_100km == 16.667
    assert state.tracked_km == 30.0
    assert state.tracked_kwh == 5.0


def test_a_seed_is_argued_down_by_measurement_not_overridden():
    seeded = seed_efficiency(15.0, 60.0)
    moved = add_efficiency_sample(seeded, 9.0, 4.0)
    assert moved.avg_kwh_100km < 15.0
    assert moved.avg_kwh_100km > 9.0  # one short trip cannot rewrite the lifetime
    assert moved.seed_kwh_100km == 15.0


def test_rejected_sample_leaves_the_figure_alone():
    state = seed_efficiency(15.0, 60.0)
    for value, km in ((None, 4.0), (9.0, None), (9.0, 0.0), (-9.0, 4.0), (9.0, -4.0)):
        assert add_efficiency_sample(state, value, km) == state


def test_long_run_converges_on_the_measured_value():
    state = seed_efficiency(15.0, 60.0)
    for _ in range(50):
        state = add_efficiency_sample(state, 20.0, 10.0)
    assert state.tracked_km == 900.0
    assert state.avg_kwh_100km == 17.778  # 160 kWh over 900 km


# --- persistence ------------------------------------------------------------

def test_reload_continues_from_the_stored_average():
    stored = EfficiencyState(18.4, 1200.0, 220.8, 15.0).as_dict()
    state = efficiency_from_store(stored, 15.0, 60.0)
    assert state == EfficiencyState(18.4, 1200.0, 220.8, 15.0)


def test_changed_seed_rebaselines_the_figure():
    stored = EfficiencyState(18.4, 1200.0, 220.8, 15.0).as_dict()
    state = efficiency_from_store(stored, 14.0, 60.0)
    assert state == seed_efficiency(14.0, 60.0)


def test_missing_or_broken_record_falls_back_to_the_seed():
    for record in ({}, {"avg_kwh_100km": 18.4}, {"avg_kwh_100km": "x", "tracked_km": 1, "tracked_kwh": 1, "seed_kwh_100km": 15.0}, {"avg_kwh_100km": -1.0, "tracked_km": 1.0, "tracked_kwh": 1.0, "seed_kwh_100km": 15.0}):
        assert efficiency_from_store(record, 15.0, 60.0) == seed_efficiency(15.0, 60.0)
