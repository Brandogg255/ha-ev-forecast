"""Constants for the EV Forecast integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "ev_forecast"
PLATFORMS: Final = [Platform.SENSOR]

# Not "name": the ha-mcp config-entry tools treat ``name`` as their own
# parameter and drop it from the flow payload, so the field would arrive empty.
CONF_NAME: Final = "ev_name"
CONF_SOC_ENTITY: Final = "soc_entity_id"
CONF_PLUGGED_ENTITY: Final = "plugged_entity_id"
CONF_PLUGGED_STATES: Final = "plugged_states"
CONF_UNPLUGGED_STATES: Final = "unplugged_states"
CONF_CHARGING_STATES: Final = "charging_states"
CONF_CAPACITY: Final = "capacity_kwh"
CONF_EFFICIENCY: Final = "efficiency_pct"
CONF_CHARGE_RATE: Final = "charge_rate_kw"
CONF_BUFFER: Final = "buffer_kwh"
CONF_MAX_SOC: Final = "max_soc_pct"
CONF_HISTORY_DAYS: Final = "history_days"
CONF_SOC_WINDOW: Final = "soc_window_minutes"
CONF_HORIZON: Final = "horizon_hours"
CONF_ODOMETER_ENTITY: Final = "odometer_entity_id"
CONF_AVG_KWH_100KM: Final = "avg_kwh_100km"

DEFAULT_CAPACITY: Final = 60.0
DEFAULT_EFFICIENCY: Final = 85.0
DEFAULT_CHARGE_RATE: Final = 3.3
DEFAULT_BUFFER: Final = 5.0
DEFAULT_MAX_SOC: Final = 90.0
DEFAULT_HISTORY_DAYS: Final = 7
MIN_HISTORY_DAYS: Final = 2
MAX_HISTORY_DAYS: Final = 60
DEFAULT_SOC_WINDOW: Final = 10
MIN_SOC_WINDOW: Final = 5
MAX_SOC_WINDOW: Final = 60
DEFAULT_HORIZON: Final = 72
DEFAULT_FORECAST_HOURS: Final = 168
RETENTION_MULTIPLIER: Final = 3
DEFAULT_AVG_KWH_100KM: Final = 15.0
MIN_AVG_KWH_100KM: Final = 1.0
MAX_AVG_KWH_100KM: Final = 100.0
# An interval shorter than this is noise: a whole percent of SoC over a few
# metres of travel cannot say anything about consumption.
MIN_EFFICIENCY_SAMPLE_KM: Final = 0.2

SCAN_INTERVAL: Final = timedelta(minutes=5)
STORAGE_VERSION: Final = 1

SERVICE_BACKFILL: Final = "backfill_history"
SERVICE_PLAN_TRIP: Final = "plan_trip"
SERVICE_CLEAR_TRIPS: Final = "clear_trips"

ATTR_FORECAST: Final = "forecast"
ATTR_PLANNED_TRIPS: Final = "planned_trips"
ATTR_HISTORY_HOURS: Final = "history_hours"
ATTR_TRACKED_KM: Final = "tracked_km"
ATTR_TRACKED_KWH: Final = "tracked_kwh"

UNIT_KWH_100KM: Final = "kWh/100km"
