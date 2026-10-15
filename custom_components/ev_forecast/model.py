"""Pure computation for the EV usage model.

No Home Assistant imports here on purpose: every rule that decides how much
charge the car must hold lives in this module so it can be unit-tested without
a running instance.

Signed power convention used throughout: positive = energy into the battery,
negative = energy out of the battery (driving, vampire drain).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

HOUR = timedelta(hours=1)

# Hour-bucket record fields written by the coordinator.
FIELD_DRIVE_KWH = "drive_kwh"  # kWh taken out of the pack during the hour
FIELD_PLUGED_HOURS = "plugged_hours"  # hours (0..1) the cable was connected
FIELD_RATE_MAX = "rate_max_kw"  # highest allowed charge rate seen in the hour


def floor_hour(value: datetime) -> datetime:
    """Return ``value`` floored to the top of its hour, microseconds dropped."""
    return value.replace(minute=0, second=0, microsecond=0)


def hour_key(value: datetime) -> str:
    """Stable serialisable key for an hour bucket."""
    return floor_hour(value).isoformat()


def soc_power_kw(prev_soc: float, soc: float, elapsed_minutes: float, capacity_kwh: float) -> float:
    """Signed kW implied by an SoC delta.

    ``dSoC% * capacity / 100`` is the energy, divided by the elapsed time.
    Returns 0.0 for non-positive windows so a stalled sensor cannot invent load.
    """
    if elapsed_minutes <= 0 or capacity_kwh <= 0:
        return 0.0
    return (soc - prev_soc) * capacity_kwh / 100.0 / (elapsed_minutes / 60.0)


def drive_power_kw(soc_power: float) -> float:
    """Load seen by the energy model: only energy leaving the pack counts."""
    return max(0.0, -soc_power) if soc_power < 0 else 0.0


def drive_energy_kwh(prev_soc: float, soc: float, elapsed_minutes: float, capacity_kwh: float) -> float:
    """kWh taken out of the pack over a window (0 when the pack gained charge)."""
    power = drive_power_kw(soc_power_kw(prev_soc, soc, elapsed_minutes, capacity_kwh))
    return power * (elapsed_minutes / 60.0)


def _slot(dt: datetime, weekday_aware: bool) -> str:
    return f"{dt.weekday()}-{dt.hour}" if weekday_aware else f"{dt.hour}"


def slot_averages(
    samples: dict[str, dict[str, float]],
    field: str,
    history_days: int,
    now: datetime,
    weekday_aware: bool = True,
) -> dict[str, float]:
    """Mean of ``field`` per hour-of-week (or hour-of-day) over recent history.

    Buckets at or after the current hour are excluded: a half-filled bucket must
    not drag tomorrow's forecast down.
    """
    cutoff = floor_hour(now) - timedelta(days=max(1, history_days))
    start_of_now = floor_hour(now)
    acc: dict[str, list[float]] = {}
    for iso, record in samples.items():
        try:
            stamp = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if stamp < cutoff or stamp >= start_of_now:
            continue
        value = float(record.get(field, 0.0))
        bucket = acc.setdefault(_slot(stamp, weekday_aware), [0.0, 0.0])
        bucket[0] += value
        bucket[1] += 1
    return {slot: total / count for slot, (total, count) in acc.items() if count}


def historical_forecast(
    samples: dict[str, dict[str, float]],
    field: str,
    start: datetime,
    hours: int,
    history_days: int,
    fallback: float = 0.0,
    weekday_aware: bool = True,
) -> list[tuple[datetime, float]]:
    """Hourly forecast built from the integration's own history store.

    Preference order per future hour: hour-of-week mean, then hour-of-day mean,
    then ``fallback``. Hour-of-week is what makes a Monday commute forecast
    differ from a Sunday one, which a plain day-shift cannot express.
    """
    weekly = slot_averages(samples, field, history_days, start, True)
    daily = slot_averages(samples, field, history_days, start, False)
    base = floor_hour(start)
    out: list[tuple[datetime, float]] = []
    for step in range(1, hours + 1):
        stamp = base + step * HOUR
        value = weekly.get(_slot(stamp, True), daily.get(_slot(stamp, False), fallback))
        out.append((stamp, round(float(value), 3)))
    return out


def merge_planned_trips(
    forecast: list[tuple[datetime, float]],
    trips: list[dict],
) -> list[tuple[datetime, float]]:
    """Add manually planned trips (kWh spread over their hours) to a drive forecast.

    A trip is ``{"date": "YYYY-MM-DD", "start_hour": 16, "hours": 2, "kwh": 12}``.
    Trips outside the forecast window are ignored here but stay in the store.
    """
    if not trips:
        return forecast
    by_time = {stamp: index for index, (stamp, _) in enumerate(forecast)}
    tzinfo = forecast[0][0].tzinfo if forecast else None
    values = [value for _, value in forecast]
    for trip in trips:
        try:
            day = datetime.strptime(str(trip["date"]), "%Y-%m-%d")
            start_hour = int(trip["start_hour"])
            span = max(1, int(trip.get("hours", 1)))
            energy = float(trip["kwh"])
        except (KeyError, TypeError, ValueError):
            continue
        per_hour = energy / span
        for offset in range(span):
            stamp = day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
            if tzinfo is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=tzinfo)
            stamp = stamp + offset * HOUR
            index = by_time.get(stamp)
            if index is not None:
                values[index] += per_hour
    return [(stamp, round(value, 3)) for (stamp, _), value in zip(forecast, values)]


def required_soc_pct(
    *,
    current_rate_kw: float,
    rate_forecast: list[tuple[datetime, float]],
    drive_forecast: list[tuple[datetime, float]],
    plugged_forecast: list[tuple[datetime, float]],
    capacity_kwh: float,
    efficiency_pct: float,
    buffer_kwh: float,
    horizon_hours: int = 72,
    max_soc_pct: float = 90.0,
) -> float:
    """Smallest SoC (%) that covers the worst cumulative deficit ahead.

    Walks the horizon hour by hour, accumulating drive energy and subtracting the
    energy that could be added inside a plugged window at the best available
    rate. The worst cumulative deficit plus the buffer, expressed as a share of
    capacity, is the floor the car must never drop below.

    Hours with no forecast coverage fall back to the **mean of the forecast
    pattern**, never to the present instantaneous value: extrapolating a momentary
    standby drain across the whole horizon pins the floor at the cap.
    """
    if capacity_kwh <= 0:
        return 0.0

    drive_values = [value for _, value in drive_forecast]
    plugged_values = [value for _, value in plugged_forecast]
    rate_values = [value for _, value in rate_forecast]

    drive_mean = sum(drive_values) / len(drive_values) if drive_values else 0.0
    plugged_mean = sum(plugged_values) / len(plugged_values) if plugged_values else 0.0
    best_rate = max([current_rate_kw, *rate_values]) if rate_values else current_rate_kw
    efficiency = max(0.0, min(1.0, efficiency_pct / 100.0))

    drive_by_time = {stamp: value for stamp, value in drive_forecast}
    plugged_by_time = {stamp: value for stamp, value in plugged_forecast}

    grid = drive_forecast or rate_forecast or plugged_forecast
    if not grid:
        return 0.0
    base = grid[0][0]

    cumulative_drive = 0.0
    cumulative_charge = 0.0
    worst_deficit = 0.0
    for step in range(horizon_hours):
        stamp = base + step * HOUR
        cumulative_drive += drive_by_time.get(stamp, drive_mean)
        cumulative_charge += best_rate * efficiency * plugged_by_time.get(stamp, plugged_mean)
        worst_deficit = max(worst_deficit, cumulative_drive - cumulative_charge)

    needed_kwh = max(0.0, worst_deficit) + max(0.0, buffer_kwh)
    return round(min(needed_kwh * 100.0 / capacity_kwh, max_soc_pct), 1)


@dataclass
class TripPlan:
    """A planned trip used to size the floor ahead of time."""

    date: str
    start_hour: int
    hours: int
    kwh: float

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "start_hour": self.start_hour,
            "hours": self.hours,
            "kwh": self.kwh,
        }


def daily_total(samples: dict[str, dict[str, float]], field: str, day: datetime) -> float:
    """Sum of ``field`` for hour buckets falling on ``day``'s local date."""
    target = day.date()
    total = 0.0
    for iso, record in samples.items():
        try:
            stamp = datetime.fromisoformat(iso)
        except ValueError:
            continue
        if stamp.date() == target:
            total += float(record.get(field, 0.0))
    return round(total, 3)


def window_total(samples: dict[str, dict[str, float]], field: str) -> float:
    """Sum of ``field`` across every retained hour bucket."""
    return round(sum(float(record.get(field, 0.0)) for record in samples.values()), 3)


def prune_samples(samples: dict[str, dict[str, float]], keep_from: datetime) -> dict[str, dict[str, float]]:
    """Drop hour buckets older than ``keep_from``."""
    boundary = floor_hour(keep_from).isoformat()
    return {iso: record for iso, record in samples.items() if iso >= boundary}


def soc_slope_pct_per_hour(
    samples: list[tuple[datetime, float]], window_minutes: int, now: datetime
) -> float:
    """Percent-per-hour slope over the samples inside ``window_minutes``.

    Positive means the pack is filling; negative means energy is leaving it.
    """
    window_start = now - timedelta(minutes=window_minutes)
    inside = [(stamp, value) for stamp, value in samples if stamp >= window_start]
    if len(inside) < 2:
        return 0.0
    first_ts, first_soc = inside[0]
    last_ts, last_soc = inside[-1]
    minutes = (last_ts - first_ts).total_seconds() / 60.0
    if minutes <= 0:
        return 0.0
    return (last_soc - first_soc) * 60.0 / minutes


def accumulate_interval(
    hours: dict[str, dict[str, float]],
    previous: tuple[datetime, float, bool] | None,
    current: tuple[datetime, float, bool],
    capacity_kwh: float,
    charge_rate_kw: float,
    max_gap_minutes: float = 45.0,
) -> float:
    """Fold the interval between two polls into the current hour bucket.

    ``previous``/``current`` are ``(stamp, soc_pct, plugged)``. A gap longer
    than ``max_gap_minutes`` contributes nothing: a restart or an offline
    period must not invent load. Returns the kWh added to the bucket.
    """
    if previous is None:
        return 0.0
    prev_ts, prev_soc, prev_plugged = previous
    stamp, soc, _ = current
    elapsed_minutes = (stamp - prev_ts).total_seconds() / 60.0
    if elapsed_minutes <= 0 or elapsed_minutes > max_gap_minutes:
        return 0.0
    bucket = hours.setdefault(hour_key(stamp), {})
    energy = drive_energy_kwh(prev_soc, soc, elapsed_minutes, capacity_kwh)
    if energy > 0:
        bucket[FIELD_DRIVE_KWH] = round(bucket.get(FIELD_DRIVE_KWH, 0.0) + energy, 4)
    if prev_plugged:
        elapsed_hours = elapsed_minutes / 60.0
        bucket[FIELD_PLUGED_HOURS] = round(
            min(1.0, bucket.get(FIELD_PLUGED_HOURS, 0.0) + elapsed_hours), 4
        )
        bucket[FIELD_RATE_MAX] = max(bucket.get(FIELD_RATE_MAX, 0.0), charge_rate_kw)
    return energy


def fill_missing(
    hours: dict[str, dict[str, float]], key: str, field: str, value: float
) -> bool:
    """Write ``value`` only when ``field`` is still empty for that hour.

    Backfill uses this so replayed history can never double count a bucket the
    live path already accumulated.
    """
    bucket = hours.setdefault(key, {})
    if bucket.get(field, 0.0):
        return False
    if value <= 0:
        return False
    bucket[field] = round(float(value), 4)
    return True
