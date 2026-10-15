# EV Forecast

Turns an EV's state-of-charge sensor into the signals an energy optimizer
needs to plan charging: charge rate, drive load, plugged behaviour, and the
minimum state of charge charging has to reach.

Built for [Home Assistant Energy Optimizer (HAEO)](https://haeo.io). It
replaces the hand-built helper stack (derivative helper, template sensors,
`input_number`, Utility Meter, three [HAFO](https://hafo.haeo.io)
`historical_shift` entries and a refresh automation) with one config entry.

## Why not HAFO for EV signals

- **Cold start.** HAFO's `historical_shift` needs a week of its own
  accumulated history before it produces anything useful. This integration
  keeps its own hour buckets and can be seeded from the recorder in one
  service call, so the forecasts are usable the same day.
- **Weekday vs weekend.** `historical_shift` shifts yesterday's pattern onto
  today. Monday 08:00 is not Sunday 08:00. This integration averages by
  hour-of-week and falls back to hour-of-day when a slot has no history.
- **EV drive load is invisible.** Nothing else in HA reports the kWh the car
  takes out of the pack, so it never appears in HAEO's load forecast.
- **A SoC target is not a requirement.** "Charge to 70 %" is a guess. This
  integration sizes the target from the trips ahead, the charge rate and the
  charging window, and caps it so an unreachable target never pins the
  charger at maximum for three days.

## How it works

1. Every 5 minutes the SoC sensor is polled. The slope over a configurable
   window (default 10 min) becomes `soc_power` in kW: positive into the pack,
   negative out of it. Energy leaving the pack is `drive_power`.
2. Each interval is folded into an hour bucket in the integration's own
   store: `drive_kwh`, `plugged_hours`, `rate_max_kw`. A gap longer than
   45 minutes (restart, offline EV) contributes nothing, so downtime cannot
   invent load.
3. Three hourly forecasts (default 168 h) are built from those buckets by
   hour-of-week averaging, with hour-of-day and finally a constant fallback.
   Planned trips are added on top of the drive forecast.
4. The required state of charge walks the horizon hour by hour, accumulating
   drive energy and the energy the charging window can deliver, and takes the
   worst cumulative deficit plus the comfort buffer. Hours with no history use
   the pattern mean, never the present instantaneous value — extrapolating a
   momentary standby drain across 72 h is what pins a target at the cap.

## Install

HACS: add this repository as a custom integration repository, or copy
`custom_components/ev_forecast` into `<config>/custom_components/ev_forecast`
and restart Home Assistant.

Then **Settings → Devices & Services → Add Integration → EV Forecast**.

## Configuration

| Option | Default | Meaning |
| --- | --- | --- |
| EV name | – | Drives entity ids: `GAC Aion UT` → `sensor.gac_aion_ut_charge_rate_forecast` |
| State of charge sensor | – | Sensor whose state is the pack percentage (0-100) |
| Plugged-in binary sensor | – | `on` while the cable is connected |
| Usable pack capacity | 60 kWh | Converts percentage into kW and kWh |
| Charging efficiency | 85 % | Wall-to-pack efficiency |
| Charge rate | 3.3 kW | AC power drawn while plugged; used where no history exists |
| Comfort buffer | 5 kWh | Extra energy kept above the trip requirement |
| Maximum state of charge | 90 % | Cap for the required state of charge |
| Pattern history | 7 days | Days of hour buckets averaged by the forecasts |
| State-of-charge window | 10 min | Slope window; shorter reacts faster, longer is smoother |
| Sizing horizon | 72 h | How far ahead the required state of charge looks |

Options are editable without recreating the entities; changing the source
entities reloads the entry.

## Entities

For an EV named *GAC Aion UT*:

| Entity id | Unit | Content |
| --- | --- | --- |
| `sensor.gac_aion_ut_soc_change` | %/h | SoC slope over the window |
| `sensor.gac_aion_ut_soc_power` | kW | Signed pack power (+ charging, − driving) |
| `sensor.gac_aion_ut_drive_power` | kW | Energy leaving the pack (drive, vampire drain) |
| `sensor.gac_aion_ut_drive_energy` | kWh | Drive energy across the retained window |
| `sensor.gac_aion_ut_drive_energy_daily` | kWh | Drive energy today |
| `sensor.gac_aion_ut_plugged_flag` | – | 1 plugged, 0 not |
| `sensor.gac_aion_ut_charge_rate` | kW | Allowed charge rate right now |
| `sensor.gac_aion_ut_soc_required` | % | Minimum SoC for the horizon |
| `sensor.gac_aion_ut_charge_rate_forecast` | kW | Hourly forecast, `forecast` attribute |
| `sensor.gac_aion_ut_drive_power_forecast` | kW | Hourly forecast, `forecast` + `planned_trips` |
| `sensor.gac_aion_ut_plugged_flag_forecast` | – | Hourly forecast, `forecast` attribute |

The three forecast entities carry the HAEO-consumable attribute:

```yaml
forecast:
  - time: "2026-10-15T11:00:00+11:00"
    value: 3.3
  - time: "2026-10-15T12:00:00+11:00"
    value: 0.0
```

## Wiring into HAEO

| HAEO field | Set to |
| --- | --- |
| Battery → `limits.min_charge_percentage` | `sensor.<ev>_soc_required` |
| Battery → `power_limits.max_power_target_source` | `sensor.<ev>_charge_rate_forecast` |
| A load subentry's forecast number | `sensor.<ev>_drive_power_forecast` |

Feed a forecast sensor, not the live scalar, into any field that expects a
time series — and never both into the same field: HAEO merges sensors on
shared timestamps, so a live value plus its own forecast double counts.

## Services

Seed the history so forecasts and the required SoC work on day one:

```yaml
service: ev_forecast.backfill_history
data:
  days: 14
```

Only hour buckets that are still empty are written, so replayed history can
never double count what the live path already accumulated. Requires the
`recorder` integration (default install).

Tell the model about a trip before it happens — this is what the historical
pattern cannot know:

```yaml
service: ev_forecast.plan_trip
data:
  date: "2026-10-18"
  start_hour: 7
  hours: 2
  kwh: 14
```

```yaml
service: ev_forecast.clear_trips
data: {}
```

`entry_id` is optional on all three services; leave it out to apply to every
configured EV.

## Tuning notes

- **Charge rate** is the AC power the EV actually draws. If the car charges at
  7.4 kW, set 7.4 — the sizing math uses it to decide how much a charging
  window can deliver.
- **Efficiency** is wall-to-pack. On-board chargers plus cable losses are
  typically 80-90 %.
- **Buffer** is the only "comfort" knob: raise it if the car arrives at the
  target and still needs more.
- The required SoC is recomputed on every poll, so it tracks reality instead
  of lagging a day behind a shifted forecast.

## Limitations

- Drive power is derived from SoC slope. EVs that report SoC in coarse steps
  (whole percent) or only every few minutes need a longer slope window; the
  resolution limit is roughly `1 % of pack per window`.
- Vampire drain while the car is asleep shows up as drive energy, which is
  correct but noisier than a real driving measurement.
- Hour buckets are local-time keyed; a DST changeover hour is attributed to
  one bucket.
- Planned trips are manual. Calendar-derived trips would be a better input
  and are not implemented.

## Migrating from the helper stack

The entity ids match the usual helper naming, so HAEO bindings keep working
if you install this first and delete the helpers afterwards:

1. Install and configure the integration; run `ev_forecast.backfill_history`.
2. Compare the integration's values against the helpers for a day.
3. Delete the old helpers, HAFO entries and the refresh automation.
4. Re-check the HAEO subentries still resolve.

## Development

```
python -m pytest tests -q
```

The sizing and forecasting rules live in `model.py` with no Home Assistant
imports, so the whole rule set is unit-tested without an instance.
