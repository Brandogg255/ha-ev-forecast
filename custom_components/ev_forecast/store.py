"""Persistent per-entry store for hour buckets and planned trips.

The integration keeps its own hourly history instead of relying on Home
Assistant's recorder statistics. That is what makes backfill and forecasting
possible on day one: the store can be seeded from the SoC sensor's recorded
state history, and it survives restarts without waiting for upstream
integrations to accumulate data.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class UsageStore:
    """Hour buckets keyed by local ISO hour, plus manually planned trips."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}/{entry_id}.json")
        self.hours: dict[str, dict[str, float]] = {}
        self.trips: list[dict[str, Any]] = []
        # The lifetime consumption figure: the running mean plus the km and kWh
        # behind it, so a reload continues the average instead of re-seeding it.
        self.efficiency: dict[str, float] = {}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        hours = data.get("hours") or {}
        loaded: dict[str, dict[str, float]] = {}
        for key, record in hours.items():
            if not isinstance(key, str) or not isinstance(record, dict):
                continue
            loaded[key] = {}
            for field, value in record.items():
                try:
                    loaded[key][str(field)] = float(value)
                except (TypeError, ValueError):
                    continue
        self.hours = loaded
        self.trips = [trip for trip in (data.get("trips") or []) if isinstance(trip, dict)]
        efficiency = data.get("efficiency")
        if isinstance(efficiency, dict):
            self.efficiency = {
                str(field): float(value)
                for field, value in efficiency.items()
                if isinstance(value, (int, float))
            }

    async def async_save(self) -> None:
        await self._store.async_save(
            {"hours": self.hours, "trips": self.trips, "efficiency": self.efficiency}
        )
