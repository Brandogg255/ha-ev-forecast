"""Sensors exposing the EV usage model to HAEO and the Energy dashboard.

Entity ids follow ``<device name> <description name>``, so an EV configured as
"GAC Aion UT" produces ``sensor.gac_aion_ut_charge_rate_forecast`` and friends -
the same names HAEO battery fields already accept.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_FORECAST, ATTR_HISTORY_HOURS, ATTR_PLANNED_TRIPS, DOMAIN
from .coordinator import EVUsageCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class EVSensorDescription(SensorEntityDescription):
    """Sensor wired to one key of the coordinator payload."""

    value_key: str = ""
    forecast_key: str | None = None


DESCRIPTIONS: tuple[EVSensorDescription, ...] = (
    EVSensorDescription(
        key="soc_change",
        value_key="soc_change",
        name="SoC Change",
        native_unit_of_measurement=f"{PERCENTAGE}/{UnitOfTime.HOURS}",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:slope-uphill",
    ),
    EVSensorDescription(
        key="soc_power",
        value_key="soc_power",
        name="SoC Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:car-electric",
    ),
    EVSensorDescription(
        key="drive_power",
        value_key="drive_power",
        name="Drive Power",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:road",
    ),
    EVSensorDescription(
        key="drive_energy",
        value_key="drive_energy",
        name="Drive Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:gauge",
    ),
    EVSensorDescription(
        key="drive_energy_daily",
        value_key="drive_energy_daily",
        name="Drive Energy Daily",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:calendar-today",
    ),
    EVSensorDescription(
        key="plugged_flag",
        value_key="plugged_flag",
        name="Plugged Flag",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ev-station",
    ),
    EVSensorDescription(
        key="charging_flag",
        value_key="charging_flag",
        name="Charging Flag",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging",
    ),
    EVSensorDescription(
        key="charge_rate",
        value_key="charge_rate",
        name="Charge Rate",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
    ),
    EVSensorDescription(
        key="soc_required",
        value_key="soc_required",
        name="SoC Required",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:battery-charging-50",
    ),
    EVSensorDescription(
        key="charge_rate_forecast",
        value_key="charge_rate",
        forecast_key="rate_forecast",
        name="Charge Rate Forecast",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash-outline",
    ),
    EVSensorDescription(
        key="drive_power_forecast",
        value_key="drive_power",
        forecast_key="drive_forecast",
        name="Drive Power Forecast",
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:road-variant",
    ),
    EVSensorDescription(
        key="plugged_flag_forecast",
        value_key="plugged_flag",
        forecast_key="plugged_forecast",
        name="Plugged Flag Forecast",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:ev-plug-ccs2",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EVUsageCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        EVModelSensor(coordinator, description, entry.entry_id) for description in DESCRIPTIONS
    )


class EVModelSensor(CoordinatorEntity[EVUsageCoordinator], SensorEntity):
    """One value (and optionally a forecast time series) from the model."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EVUsageCoordinator,
        description: EVSensorDescription,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}:{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=coordinator.config.name,
            manufacturer="EV Forecast",
            model="EV usage model",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self.coordinator.data)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        forecast_key = self.entity_description.forecast_key
        if forecast_key:
            series = data.get(forecast_key) or []
            if series:
                return float(series[0][1])
        value = data.get(self.entity_description.value_key)
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        attributes: dict[str, Any] = {ATTR_HISTORY_HOURS: data.get("history_hours", 0)}
        forecast_key = self.entity_description.forecast_key
        if forecast_key:
            series = data.get(forecast_key) or []
            attributes[ATTR_FORECAST] = [
                {"time": stamp.isoformat(), "value": round(float(value), 3)} for stamp, value in series
            ]
            if forecast_key == "drive_forecast":
                attributes[ATTR_PLANNED_TRIPS] = data.get("planned_trips", [])
        return attributes
