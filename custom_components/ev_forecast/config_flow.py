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
    DEFAULT_BUFFER,
    DEFAULT_CAPACITY,
    DEFAULT_CHARGE_RATE,
    DEFAULT_EFFICIENCY,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HORIZON,
    DEFAULT_MAX_SOC,
    DEFAULT_SOC_WINDOW,
    DOMAIN,
    MAX_HISTORY_DAYS,
    MAX_SOC_WINDOW,
    MIN_HISTORY_DAYS,
    MIN_SOC_WINDOW,
)
from .model import normalise_state_list

_LOGGER = logging.getLogger(__name__)

# Select selectors exchange strings with the UI, so the horizon choices are
# strings and are coerced back to int when the entry is written.
HORIZON_CHOICES = ["24", "48", "72", "120", "168"]

_FLOAT_KEYS: dict[str, float] = {
    CONF_CAPACITY: DEFAULT_CAPACITY,
    CONF_EFFICIENCY: DEFAULT_EFFICIENCY,
    CONF_CHARGE_RATE: DEFAULT_CHARGE_RATE,
    CONF_BUFFER: DEFAULT_BUFFER,
    CONF_MAX_SOC: DEFAULT_MAX_SOC,
}
_INT_KEYS: dict[str, int] = {
    CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
    CONF_SOC_WINDOW: DEFAULT_SOC_WINDOW,
    CONF_HORIZON: DEFAULT_HORIZON,
}


def _number(
    minimum: float,
    maximum: float,
    step: float,
    unit: str | None = None,
    mode: selector.NumberSelectorMode = selector.NumberSelectorMode.BOX,
) -> selector.NumberSelector:
    """Build a bounded number selector, omitting the unit when there is none."""
    config: dict[str, Any] = {"min": minimum, "max": maximum, "step": step, "mode": mode}
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(**config))

def _tags() -> selector.SelectSelector:
    """Free-form list of state names the user types themselves.

    ``custom_value`` (core 2024.10+) on a multiple dropdown accepts typed
    values as well as listed ones, and with no preset options the field is a
    pure text-in, chips-out list - which is what a charger's status vocabulary
    needs, since it differs per brand.
    """
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[],
            multiple=True,
            custom_value=True,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _schema(defaults: dict[str, Any], *, strict: bool) -> vol.Schema:
    """Build the config or options form for one EV.

    ``strict`` is what the options flow uses: every field is pre-filled with
    the stored value. The user step leaves the two entity pickers with no
    value at all, so a submission that omits one re-shows the form with an
    error instead of failing schema validation with a hard 400.
    """

    def prefilled(key: str, default: Any) -> vol.Optional:
        return vol.Optional(key, default=defaults.get(key, default))

    def picker(key: str) -> vol.Optional:
        if strict:
            return vol.Optional(key, default=defaults.get(key))
        return vol.Optional(key)

    return vol.Schema(
        {
            prefilled(CONF_NAME, ""): cv.string,
            picker(CONF_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            picker(CONF_PLUGGED_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "sensor"]
                )
            ),
            prefilled(CONF_PLUGGED_STATES, []): _tags(),
            prefilled(CONF_UNPLUGGED_STATES, []): _tags(),
            prefilled(CONF_CHARGING_STATES, []): _tags(),
            prefilled(CONF_CAPACITY, DEFAULT_CAPACITY): _number(1, 500, 0.1, "kWh"),
            prefilled(CONF_EFFICIENCY, DEFAULT_EFFICIENCY): _number(
                50, 100, 1, "%", selector.NumberSelectorMode.SLIDER
            ),
            prefilled(CONF_CHARGE_RATE, DEFAULT_CHARGE_RATE): _number(0, 50, 0.1, "kW"),
            prefilled(CONF_BUFFER, DEFAULT_BUFFER): _number(0, 50, 0.5, "kWh"),
            prefilled(CONF_MAX_SOC, DEFAULT_MAX_SOC): _number(
                10, 100, 1, "%", selector.NumberSelectorMode.SLIDER
            ),
            prefilled(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS): _number(
                MIN_HISTORY_DAYS, MAX_HISTORY_DAYS, 1
            ),
            prefilled(CONF_SOC_WINDOW, DEFAULT_SOC_WINDOW): _number(
                MIN_SOC_WINDOW, MAX_SOC_WINDOW, 1, "min"
            ),
            vol.Optional(
                CONF_HORIZON,
                default=str(defaults.get(CONF_HORIZON, DEFAULT_HORIZON)),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=HORIZON_CHOICES, translation_key="horizon_hours"
                )
            ),
        }
    )


def _normalise(user_input: dict[str, Any]) -> dict[str, Any]:
    """Type the form output so entry data is numbers, not strings."""
    data = dict(user_input)
    data[CONF_NAME] = str(data.get(CONF_NAME, "")).strip()
    for key, fallback in _FLOAT_KEYS.items():
        data[key] = float(data.get(key, fallback))
    for key, fallback in _INT_KEYS.items():
        data[key] = int(data.get(key, fallback))
    for key in (CONF_PLUGGED_STATES, CONF_UNPLUGGED_STATES, CONF_CHARGING_STATES):
        data[key] = list(normalise_state_list(data.get(key, ())))
    return data


def _derived_name(friendly_name: Any, entity_id: str) -> str:
    """Fall back to the state-of-charge sensor's name when none was given."""
    label = str(friendly_name or entity_id.split(".", 1)[-1]).replace("_", " ").strip()
    return label or entity_id


class EVForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one EV usage model per vehicle."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _normalise(user_input)
            for key in (CONF_SOC_ENTITY, CONF_PLUGGED_ENTITY):
                entity = data.get(key)
                if not entity or self.hass.states.get(str(entity)) is None:
                    errors[key] = "entity_not_found"
            # A status sensor reports words, not on/off: without at least one
            # list every state would read as disconnected, silently.
            plugged_id = str(data.get(CONF_PLUGGED_ENTITY) or "")
            if plugged_id and not plugged_id.startswith("binary_sensor."):
                buckets = (CONF_PLUGGED_STATES, CONF_UNPLUGGED_STATES, CONF_CHARGING_STATES)
                if not any(normalise_state_list(data.get(key)) for key in buckets):
                    errors[CONF_PLUGGED_STATES] = "states_required"
            if not errors:
                await self.async_set_unique_id(str(data[CONF_SOC_ENTITY]))
                self._abort_if_unique_id_configured()
                if not data[CONF_NAME]:
                    state = self.hass.states.get(str(data[CONF_SOC_ENTITY]))
                    data[CONF_NAME] = _derived_name(
                        state.attributes.get("friendly_name"), str(data[CONF_SOC_ENTITY])
                    )
                return self.async_create_entry(title=data[CONF_NAME], data=data)
        return self.async_show_form(
            step_id="user", data_schema=_schema({}, strict=False), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Tune the model without recreating the entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # The strict schema pre-fills every field, so this submission is
            # complete; the coordinator overlays options on entry data.
            return self.async_create_entry(title="", data=_normalise(user_input))
        entry: ConfigEntry = self.config_entry
        return self.async_show_form(
            step_id="init",
            data_schema=_schema({**entry.data, **entry.options}, strict=True),
        )
