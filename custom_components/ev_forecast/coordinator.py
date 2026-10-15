"""Coordinator turning EV state of charge into load, energy and forecasts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BUFFER,
    CONF_CAPACITY,
    CONF_CHARGE_RATE,
    CONF_CHARGING_STATES,
    CONF_EFFICIENCY,
    CONF_HISTORY_DAYS,
    CONF_HORIZON,
    CONF_MAX_SOC,
    CONF_NAME,
    CONF_PLUGGED_ENTITY,
    CONF_PLUGGED_STATES,
    CONF_SOC_ENTITY,
    CONF_SOC_WINDOW,
    CONF_UNPLUGGED_STATES,
    DEFAULT_FORECAST_HOURS,
    DOMAIN,
    RETENTION_MULTIPLIER,
    SCAN_INTERVAL,
)
from .model import (
    FIELD_DRIVE_KWH,
    FIELD_PLUGED_HOURS,
    FIELD_RATE_MAX,
    accumulate_interval,
    classify_connection,
    daily_total,
    drive_power_kw,
    historical_forecast,
    merge_planned_trips,
    normalise_state_list,
    prune_samples,
    replay_history,
    required_soc_pct,
    soc_power_kw,
    soc_slope_pct_per_hour,
    window_total,
)
from .store import UsageStore

_LOGGER = logging.getLogger(__name__)

# A gap longer than this many poll intervals is treated as "we were not watching"
# and contributes no energy, so restarts and offline periods cannot invent load.
MAX_ACCUMULATE_GAP = timedelta(minutes=45)


@dataclass(frozen=True)
class EVModelConfig:
    """Tunable parameters of one EV's usage model."""

    name: str
    soc_entity_id: str
    plugged_entity_id: str
    plugged_states: tuple[str, ...]
    unplugged_states: tuple[str, ...]
    charging_states: tuple[str, ...]
    capacity_kwh: float
    efficiency_pct: float
    charge_rate_kw: float
    buffer_kwh: float
    max_soc_pct: float
    history_days: int
    soc_window_minutes: int
    horizon_hours: int
    forecast_hours: int

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> EVModelConfig:
        merged = {**entry.data, **entry.options}
        return cls(
            name=str(merged[CONF_NAME]),
            soc_entity_id=str(merged[CONF_SOC_ENTITY]),
            plugged_entity_id=str(merged[CONF_PLUGGED_ENTITY]),
            plugged_states=normalise_state_list(merged.get(CONF_PLUGGED_STATES, ())),
            unplugged_states=normalise_state_list(merged.get(CONF_UNPLUGGED_STATES, ())),
            charging_states=normalise_state_list(merged.get(CONF_CHARGING_STATES, ())),
            capacity_kwh=float(merged[CONF_CAPACITY]),
            efficiency_pct=float(merged[CONF_EFFICIENCY]),
            charge_rate_kw=float(merged[CONF_CHARGE_RATE]),
            buffer_kwh=float(merged[CONF_BUFFER]),
            max_soc_pct=float(merged[CONF_MAX_SOC]),
            history_days=int(merged[CONF_HISTORY_DAYS]),
            soc_window_minutes=int(merged[CONF_SOC_WINDOW]),
            horizon_hours=int(merged.get(CONF_HORIZON, 72)),
            forecast_hours=int(merged.get("forecast_hours", DEFAULT_FORECAST_HOURS)),
        )


class EVUsageCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the EV sensors, accumulates hour buckets, publishes derived values."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        config: EVModelConfig,
        store: UsageStore,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {config.name}",
            update_interval=SCAN_INTERVAL,
        )
        self.entry = entry
        self.config = config
        self.store = store
        self._soc_samples: deque[tuple[datetime, float]] = deque(maxlen=400)
        self._previous: tuple[datetime, float, bool] | None = None
        self._last_saved: datetime | None = None

    # --- lifecycle ---------------------------------------------------------

    async def _async_setup(self) -> None:
        await self.store.async_load()

    async def _async_update_data(self) -> dict[str, Any]:
        config = self.config
        now = dt_util.now()

        soc = self._numeric_state(config.soc_entity_id)
        if soc is None:
            self._previous = None
            raise UpdateFailed(f"{config.soc_entity_id} has no numeric state")

        plugged, charging = self._connection(config.plugged_entity_id)
        self._accumulate(now, soc, plugged)
        self._soc_samples.append((now, soc))
        window = timedelta(minutes=config.soc_window_minutes)
        while self._soc_samples and now - self._soc_samples[0][0] > window:
            self._soc_samples.popleft()

        soc_change = self._soc_slope_pct_per_hour(now)
        soc_power = soc_change * config.capacity_kwh / 100.0
        drive_power = drive_power_kw(soc_power)
        rate_now = config.charge_rate_kw if plugged else 0.0

        drive_forecast, plugged_forecast, rate_forecast = self._build_forecasts(now, plugged)
        soc_required = required_soc_pct(
            current_rate_kw=rate_now,
            rate_forecast=rate_forecast,
            drive_forecast=drive_forecast,
            plugged_forecast=plugged_forecast,
            capacity_kwh=config.capacity_kwh,
            efficiency_pct=config.efficiency_pct,
            buffer_kwh=config.buffer_kwh,
            horizon_hours=config.horizon_hours,
            max_soc_pct=config.max_soc_pct,
        )

        self.store.hours = prune_samples(self.store.hours, now - timedelta(days=config.history_days * RETENTION_MULTIPLIER))
        await self._maybe_save(now)

        return {
            "soc": soc,
            "plugged": plugged,
            "soc_change": round(soc_change, 3),
            "soc_power": round(soc_power, 3),
            "drive_power": round(drive_power, 3),
            "drive_energy": window_total(self.store.hours, FIELD_DRIVE_KWH),
            "drive_energy_daily": round(daily_total(self.store.hours, FIELD_DRIVE_KWH, now), 3),
            "plugged_flag": 1 if plugged else 0,
            "charging_flag": 1 if charging else 0,
            "charge_rate": rate_now,
            "soc_required": soc_required,
            "drive_forecast": drive_forecast,
            "plugged_forecast": plugged_forecast,
            "rate_forecast": rate_forecast,
            "history_hours": len(self.store.hours),
            "planned_trips": list(self.store.trips),
        }

    # --- accumulation ------------------------------------------------------

    def _accumulate(self, now: datetime, soc: float, plugged: bool) -> None:
        """Fold the interval since the previous poll into the current hour bucket."""
        previous = self._previous
        self._previous = (now, soc, plugged)
        accumulate_interval(
            self.store.hours,
            previous,
            (now, soc, plugged),
            self.config.capacity_kwh,
            self.config.charge_rate_kw,
            MAX_ACCUMULATE_GAP.total_seconds() / 60.0,
        )

    def _soc_slope_pct_per_hour(self, now: datetime) -> float:
        return soc_slope_pct_per_hour(list(self._soc_samples), self.config.soc_window_minutes, now)

    def _build_forecasts(
        self, now: datetime, plugged_now: bool
    ) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]], list[tuple[datetime, float]]]:
        config = self.config
        hours = self.store.hours
        drive = historical_forecast(
            hours, FIELD_DRIVE_KWH, now, config.forecast_hours, config.history_days, fallback=0.0
        )
        drive = merge_planned_trips(drive, self.store.trips)
        plugged_fc = historical_forecast(
            hours,
            FIELD_PLUGED_HOURS,
            now,
            config.forecast_hours,
            config.history_days,
            fallback=1.0 if plugged_now else 0.0,
        )
        rate_fc = historical_forecast(
            hours,
            FIELD_RATE_MAX,
            now,
            config.forecast_hours,
            config.history_days,
            fallback=config.charge_rate_kw if plugged_now else 0.0,
        )
        return drive, plugged_fc, rate_fc

    async def _maybe_save(self, now: datetime) -> None:
        if self._last_saved is not None and now - self._last_saved < timedelta(minutes=15):
            return
        await self.store.async_save()
        self._last_saved = now

    async def async_flush(self) -> None:
        await self.store.async_save()
        self._last_saved = dt_util.now()

    # --- services ----------------------------------------------------------

    async def async_backfill(self, days: int) -> dict[str, int]:
        """Replay recorded SoC / plugged history into the hour store.

        The replay ticks at ``SCAN_INTERVAL`` and folds each tick through the
        same accumulator the live loop uses, so a six-hour plugged run becomes
        six hour buckets and a three-hour SoC ramp is spread over the hours it
        actually spanned. Only hour buckets still empty for a field are
        written, so live data is never counted twice.
        """
        from homeassistant.components.recorder.history import get_significant_states

        end = dt_util.now()
        start = end - timedelta(days=max(1, days))
        entity_ids = [self.config.soc_entity_id, self.config.plugged_entity_id]
        try:
            result = await self.hass.async_add_executor_job(
                get_significant_states,
                self.hass,
                start,
                end,
                entity_ids,
                None,
                True,
                False,
                False,
                True,
            )
        except Exception as err:  # noqa: BLE001 - recorder may be disabled
            _LOGGER.error("Backfill for %s failed: %s", self.config.name, err)
            return {"error": 1, "hours": 0}

        soc_series = self._raw_series(result.get(self.config.soc_entity_id))
        connection_series = self._raw_series(result.get(self.config.plugged_entity_id))
        config = self.config
        hours = replay_history(
            self.store.hours,
            soc_series,
            connection_series,
            start=start,
            end=end,
            step=SCAN_INTERVAL,
            capacity_kwh=config.capacity_kwh,
            charge_rate_kw=config.charge_rate_kw,
            plugged_states=config.plugged_states,
            unplugged_states=config.unplugged_states,
            charging_states=config.charging_states,
            max_gap_minutes=MAX_ACCUMULATE_GAP.total_seconds() / 60.0,
        )

        await self.async_flush()
        _LOGGER.info(
            "Backfilled %s: %s hour buckets from %s days of history at %s cadence",
            self.config.name,
            hours,
            days,
            SCAN_INTERVAL,
        )
        # Force the new buckets through the payload now. ``async_request_refresh``
        # is debounced with a ten-second cooldown, so a service call that follows
        # another refresh within that window would silently wait; HAEO reads
        # soc_required right after a backfill or a planned trip.
        await self.async_refresh()
        return {"hours": hours}

    def _state_pair(self, state: Any) -> tuple[Any, Any]:
        """Return ``(stamp, raw_state)`` for a recorder row or live state."""
        stamp = getattr(state, "last_changed", None)
        raw = getattr(state, "state", None)
        if raw is None and isinstance(state, dict):
            stamp = dt_util.parse_datetime(str(state.get("last_changed")))
            raw = state.get("state")
        return stamp, raw

    def _raw_series(self, states: Any) -> list[tuple[datetime, str]]:
        """Recorder rows as ``(stamp, raw_state)``, ``unknown`` kept in place.

        The replay needs the holes: an entity that stopped reporting is what
        tells it to stop folding intervals, exactly as the live path does. A
        numeric-only or match-only series would bridge the outage instead.
        """
        series: list[tuple[datetime, str]] = []
        for state in states or []:
            stamp, raw = self._state_pair(state)
            if stamp is None or raw is None:
                continue
            series.append((dt_util.as_local(stamp), str(raw)))
        series.sort(key=lambda item: item[0])
        return series

    async def async_plan_trip(self, trip: dict[str, Any]) -> None:
        self.store.trips = [
            existing
            for existing in self.store.trips
            if not (existing.get("date") == trip.get("date") and existing.get("start_hour") == trip.get("start_hour"))
        ]
        self.store.trips.append(trip)
        await self.async_flush()
        await self.async_refresh()

    async def async_clear_trips(self) -> None:
        self.store.trips = []
        await self.async_flush()
        await self.async_refresh()

    # --- helpers -----------------------------------------------------------

    def _numeric_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _connection(self, entity_id: str) -> tuple[bool, bool]:
        """Classify the plugged entity's current state as (plugged, charging)."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return False, False
        return classify_connection(
            state.state,
            self.config.plugged_states,
            self.config.unplugged_states,
            self.config.charging_states,
        )
