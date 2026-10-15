"""Config flow for the EV Forecast integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv, selector

from .const import (
    CONF_BUFFER,
    CONF_CAPACITY,
    CONF_CHARGE_RATE,
    CONF_EFFICIENCY,
    CONF_HISTORY_DAYS,
    CONF_HORIZON,
    CONF_MAX_SOC,
    CONF_NAME,
    CONF_PLUGGED_ENTITY,
    CONF_SOC_ENTITY,
    CONF_SOC_WINDOW,
    DEFAULT_BUFFER,
    DEFAULT_CAPACITY,
    DEFAULT_CHARGE_RATE,
    DEFAULT_EFFICIENCY,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_MAX_SOC,
    DEFAULT_SOC_WINDOW,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SOC_WINDOW,
    MIN_HISTORY_DAYS,
    MIN_SOC_WINDOW,
)

_LOGGER = logging.getLogger(__name__)

HORIZON_CHOICES = [24, 48, 72, 120, 168]


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the shared config/options form for one EV."""
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME)): cv.string,
            vol.Required(
                CONF_SOC_ENTITY, default=defaults.get(CONF_SOC_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Required(
                CONF_PLUGGED_ENTITY, default=defaults.get(CONF_PLUGGED_ENTITY)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
            vol.Required(
                CONF_CAPACITY, default=defaults.get(CONF_CAPACITY, DEFAULT_CAPACITY)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=500,
                    step=0.1,
                    unit_of_measurement="kWh",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_EFFICIENCY, default=defaults.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=50,
                    max=100,
                    step=1,
                    unit_of_measurement="%",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_CHARGE_RATE, default=defaults.get(CONF_CHARGE_RATE, DEFAULT_CHARGE_RATE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=50,
                    step=0.1,
                    unit_of_measurement="kW",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_BUFFER, default=defaults.get(CONF_BUFFER, DEFAULT_BUFFER)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=50,
                    step=0.5,
                    unit_of_measurement="kWh",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_MAX_SOC, default=defaults.get(CONF_MAX_SOC, DEFAULT_MAX_SOC)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=10,
                    max=100,
                    step=1,
                    unit_of_measurement="%",
                    mode=selector.NumberSelectorMode.SLIDER,
                )
            ),
            vol.Required(
                CONF_HISTORY_DAYS, default=defaults.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_HISTORY_DAYS,
                    max=MAX_HISTORY_DAYS,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_SOC_WINDOW, default=defaults.get(CONF_SOC_WINDOW, DEFAULT_SOC_WINDOW)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=MIN_SOC_WINDOW,
                    max=MAX_SOC_WINDOW,
                    step=1,
                    unit_of_measurement="min",
                    mode=selector.NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_HORIZON, default=defaults.get(CONF_HORIZON, 72)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=HORIZON_CHOICES, translation_key="horizon_hours")
            ),
        }
    )


class EVForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one EV usage model per vehicle."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            soc_entity = user_input[CONF_SOC_ENTITY]
            if self.hass.states.get(soc_entity) is None:
                errors[CONF_SOC_ENTITY] = "entity_not_found"
            else:
                await self.async_set_unique_id(soc_entity)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Tune the model without recreating the entities."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        entry: ConfigEntry = self.config_entry
        return self.async_show_form(
            step_id="init", data_schema=_schema({**entry.data, **entry.options})
        )
