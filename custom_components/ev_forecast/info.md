EV charge planning signals for Home Assistant Energy Optimizer (HAEO).

Reads an EV's state-of-charge and plugged-in sensors and publishes the
signals an optimizer needs to schedule charging:

- **Charge rate** now and as a 168 h hourly forecast.
- **Drive power and drive energy** - the kWh leaving the pack, which nothing
  else in Home Assistant reports, plus a matching load forecast.
- **Plugged behaviour** as an hourly forecast.
- **Required state of charge** sized from the trips ahead, the charge rate and
  the charging window, capped so an unreachable target never pins the charger.

Owns its own hour buckets, so `ev_forecast.backfill_history` seeds the
forecasts from recorder history on day one instead of waiting a week for
accumulation: the replay ticks at the same five-minute cadence the live model
polls at, so a six-hour plugged run becomes six hour buckets rather than one,
and only empty buckets are written. `ev_forecast.plan_trip` adds trips the
historical pattern cannot know about.

Setup and retuning are three pages - the car, the charger, the forecast.

One config entry per EV. No dependency on HAFO, HAEO or a specific car brand:
any numeric SoC sensor plus a plugged-in entity will do. The plugged input can
be a `binary_sensor` (`on` = connected) or any `sensor` whose state is a word -
`sensor.de_ev_charger_status` reporting `plugged_in`, `waiting`, `charged`,
`paused`, `sleep`, `idle` or `charging`, for example - which you map with three
free-form state lists. A `charging_flag` sensor separates plugged-but-idle from
power actually flowing.
