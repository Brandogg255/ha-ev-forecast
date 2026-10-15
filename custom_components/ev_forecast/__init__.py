"""The EV Forecast integration.

Turns an EV's state-of-charge sensor into the load, energy and forecast
signals that HAEO needs to plan charging: charge rate, drive power, plugged
behaviour, and the minimum state of charge that charging must reach.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_BACKFILL,
    SERVICE_CLEAR_TRIPS,
    SERVICE_PLAN_TRIP,
)
from .coordinator import EVModelConfig, EVUsageCoordinator
from .store import UsageStore

_LOGGER = logging.getLogger(__name__)

SERVICES_KEY = "_services_registered"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one EV usage model from a config entry."""
    config = EVModelConfig.from_entry(entry)
    store = UsageStore(hass, entry.entry_id)
    coordinator = EVUsageCoordinator(hass, entry, config, store)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "store": store,
        "config": config,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist the hour store, then tear the entry down."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data is not None:
        coordinator: EVUsageCoordinator = data["coordinator"]
        await coordinator.async_flush()
        await coordinator.async_shutdown()
    if not hass.data.get(DOMAIN):
        hass.data.pop(DOMAIN, None)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Apply option changes live; reload only when the source entities change."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is None:
        return False
    new_config = EVModelConfig.from_entry(entry)
    old_config = data["config"]
    if (new_config.soc_entity_id, new_config.plugged_entity_id) != (
        old_config.soc_entity_id,
        old_config.plugged_entity_id,
    ):
        return await hass.config_entries.async_reload(entry.entry_id)
    data["config"] = new_config
    data["coordinator"].config = new_config
    await data["coordinator"].async_request_refresh()
    return True


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[EVUsageCoordinator]:
    entry_id = call.data.get("entry_id")
    entries = hass.data.get(DOMAIN, {})
    if entry_id:
        return [entries[entry_id]["coordinator"]]
    return [data["coordinator"] for data in entries.values()]


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.data.get(SERVICES_KEY):
        return

    async def handle_backfill(call: ServiceCall) -> None:
        days = int(call.data.get("days", 7))
        for coordinator in _coordinators(hass, call):
            await coordinator.async_backfill(days)

    async def handle_plan_trip(call: ServiceCall) -> None:
        trip = {
            "date": str(call.data["date"]),
            "start_hour": int(call.data["start_hour"]),
            "hours": int(call.data.get("hours", 1)),
            "kwh": float(call.data["kwh"]),
        }
        for coordinator in _coordinators(hass, call):
            await coordinator.async_plan_trip(trip)

    async def handle_clear_trips(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_clear_trips()

    base: dict[Any, Any] = {vol.Optional("entry_id"): cv.string}
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        handle_backfill,
        schema=vol.Schema(
            {
                **base,
                vol.Optional("days", default=7): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAN_TRIP,
        handle_plan_trip,
        schema=vol.Schema(
            {
                **base,
                vol.Required("date"): cv.string,
                vol.Required("start_hour"): cv.positive_int,
                vol.Optional("hours", default=1): cv.positive_int,
                vol.Required("kwh"): vol.All(vol.Coerce(float), vol.Range(min=0)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_TRIPS,
        handle_clear_trips,
        schema=vol.Schema({**base}),
    )
    hass.data[SERVICES_KEY] = True
    _LOGGER.debug("Registered %s services", DOMAIN)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Accept future schema migrations; version 1 is the current shape."""
    if entry.version == 1:
        return True
    _LOGGER.error("Unsupported config entry version %s for %s", entry.version, entry.title)
    return False
