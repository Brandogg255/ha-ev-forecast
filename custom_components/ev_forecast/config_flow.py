"""Config flow for the EV Forecast integration.

One usage model is created over three pages - the car, the charger, the
forecast - because the three groups of settings are read from different places:
the vehicle's telematics, the wallbox's status vocabulary, and how much history
the owner wants the forecasts to average. The options flow walks the same three
pages so a retune never has to hunt through one long form.
"""

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
from homeassistant.core import HomeAssistant, callback
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
_LIST_KEYS = (CONF_PLUGGED_STATES, CONF_UNPLUGGED_STATES, CONF_CHARGING_STATES)

# Every tunable at its default, so merging three pages can never drop a key.
_DEFAULTS: dict[str, Any] = {
    CONF_NAME: "",
    **_FLOAT_KEYS,
    **_INT_KEYS,
    CONF_PLUGGED_STATES: [],
    CONF_UNPLUGGED_STATES: [],
    CONF_CHARGING_STATES: [],
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


def _prefilled(defaults: dict[str, Any], key: str, default: Any) -> vol.Optional:
    """Optional field defaulting to the stored value, or to ``default``."""
    return vol.Optional(key, default=defaults.get(key, default))


def _picker(defaults: dict[str, Any], key: str, *, strict: bool) -> vol.Optional:
    """Entity picker: pre-filled in the options flow, blank in the user flow.

    Leaving it blank in the user step is what lets a submission that omits the
    entity re-show the form with an error instead of failing schema validation.
    """
    if strict:
        return vol.Optional(key, default=defaults.get(key))
    return vol.Optional(key)


def _car_schema(defaults: dict[str, Any], *, strict: bool) -> vol.Schema:
    """Page one: which EV, and the pack parameters that size its charging."""
    return vol.Schema(
        {
            _prefilled(defaults, CONF_NAME, ""): cv.string,
            _picker(defaults, CONF_SOC_ENTITY, strict=strict): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            _prefilled(defaults, CONF_CAPACITY, DEFAULT_CAPACITY): _number(
                1, 500, 0.1, "kWh"
            ),
            _prefilled(defaults, CONF_EFFICIENCY, DEFAULT_EFFICIENCY): _number(
                50, 100, 1, "%", selector.NumberSelectorMode.SLIDER
            ),
            _prefilled(defaults, CONF_CHARGE_RATE, DEFAULT_CHARGE_RATE): _number(
                0, 50, 0.1, "kW"
            ),
            _prefilled(defaults, CONF_BUFFER, DEFAULT_BUFFER): _number(0, 50, 0.5, "kWh"),
            _prefilled(defaults, CONF_MAX_SOC, DEFAULT_MAX_SOC): _number(
                10, 100, 1, "%", selector.NumberSelectorMode.SLIDER
            ),
        }
    )


def _charger_schema(defaults: dict[str, Any], *, strict: bool) -> vol.Schema:
    """Page two: how the integration learns the cable is connected."""
    return vol.Schema(
        {
            _picker(defaults, CONF_PLUGGED_ENTITY, strict=strict): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor", "sensor"])
            ),
            _prefilled(defaults, CONF_PLUGGED_STATES, []): _tags(),
            _prefilled(defaults, CONF_UNPLUGGED_STATES, []): _tags(),
            _prefilled(defaults, CONF_CHARGING_STATES, []): _tags(),
        }
    )


def _forecast_schema(defaults: dict[str, Any], *, strict: bool) -> vol.Schema:
    """Page three: how much history is averaged, and how far ahead it looks."""
    return vol.Schema(
        {
            _prefilled(defaults, CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS): _number(
                MIN_HISTORY_DAYS, MAX_HISTORY_DAYS, 1
            ),
            _prefilled(defaults, CONF_SOC_WINDOW, DEFAULT_SOC_WINDOW): _number(
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


def _normalise(values: dict[str, Any]) -> dict[str, Any]:
    """Type one page's form output, leaving keys it does not carry alone.

    Pages are merged in order, so a page must not invent the keys of another:
    that would let a later page's default overwrite a value already captured.
    """
    data = dict(values)
    if CONF_NAME in data:
        data[CONF_NAME] = str(data[CONF_NAME] or "").strip()
    for key, fallback in _FLOAT_KEYS.items():
        if key in data:
            data[key] = float(fallback if data[key] in (None, "") else data[key])
    for key, fallback in _INT_KEYS.items():
        if key in data:
            data[key] = int(fallback if data[key] in (None, "") else data[key])
    for key in _LIST_KEYS:
        if key in data:
            data[key] = list(normalise_state_list(data[key]))
    return data


def _derived_name(friendly_name: Any, entity_id: str) -> str:
    """Fall back to the state-of-charge sensor's name when none was given."""
    label = str(friendly_name or entity_id.split(".", 1)[-1]).replace("_", " ").strip()
    return label or entity_id


def _charger_errors(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """Errors for the charger page: the entity must exist and be interpretable."""
    errors: dict[str, str] = {}
    plugged_id = str(data.get(CONF_PLUGGED_ENTITY) or "")
    if not plugged_id or hass.states.get(plugged_id) is None:
        errors[CONF_PLUGGED_ENTITY] = "entity_not_found"
    elif not plugged_id.startswith("binary_sensor."):
        # A status sensor reports words, not on/off: with no list at all every
        # state would read as disconnected, silently.
        if not any(normalise_state_list(data.get(key)) for key in _LIST_KEYS):
            errors[CONF_PLUGGED_STATES] = "states_required"
    return errors


class EVForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one EV usage model per vehicle, over three pages."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._pages: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page one: the car."""
        errors: dict[str, str] = {}
        if user_input is not None:
            car = _normalise(user_input)
            soc = str(car.get(CONF_SOC_ENTITY) or "")
            if not soc or self.hass.states.get(soc) is None:
                errors[CONF_SOC_ENTITY] = "entity_not_found"
            if not errors:
                # One usage model per state-of-charge sensor, and it is worth
                # failing here rather than after two more pages.
                await self.async_set_unique_id(soc)
                self._abort_if_unique_id_configured()
                if not car[CONF_NAME]:
                    state = self.hass.states.get(soc)
                    car[CONF_NAME] = _derived_name(
                        state.attributes.get("friendly_name"), soc
                    )
                self._pages.update(car)
                return await self.async_step_charger()
        return self.async_show_form(
            step_id="user",
            data_schema=_car_schema({**self._pages, **(user_input or {})}, strict=False),
            errors=errors,
        )

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page two: the charger."""
        errors: dict[str, str] = {}
        if user_input is not None:
            charger = _normalise(user_input)
            errors = _charger_errors(self.hass, charger)
            if not errors:
                self._pages.update(charger)
                return await self.async_step_forecast()
        return self.async_show_form(
            step_id="charger",
            data_schema=_charger_schema(
                {**self._pages, **(user_input or {})}, strict=False
            ),
            errors=errors,
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page three: the forecast."""
        if user_input is not None:
            data = _normalise({**_DEFAULTS, **self._pages, **user_input})
            return self.async_create_entry(title=data[CONF_NAME], data=data)
        return self.async_show_form(
            step_id="forecast",
            data_schema=_forecast_schema(self._pages, strict=False),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowHandler:
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlow):
    """Tune the model without recreating the entities, page by page."""

    def __init__(self) -> None:
        """Initialize the flow."""
        super().__init__()
        self._pages: dict[str, Any] = {}

    @property
    def _stored(self) -> dict[str, Any]:
        """Everything already configured, options winning over entry data."""
        entry: ConfigEntry = self.config_entry
        return {**_DEFAULTS, **entry.data, **entry.options}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page one: the car."""
        if user_input is not None:
            self._pages.update(_normalise(user_input))
            return await self.async_step_charger()
        return self.async_show_form(
            step_id="init", data_schema=_car_schema(self._stored, strict=True)
        )

    async def async_step_charger(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page two: the charger."""
        errors: dict[str, str] = {}
        if user_input is not None:
            charger = _normalise(user_input)
            errors = _charger_errors(self.hass, charger)
            if not errors:
                self._pages.update(charger)
                return await self.async_step_forecast()
        return self.async_show_form(
            step_id="charger",
            data_schema=_charger_schema(
                {**self._stored, **self._pages, **(user_input or {})}, strict=True
            ),
            errors=errors,
        )

    async def async_step_forecast(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Page three: the forecast."""
        if user_input is not None:
            self._pages.update(_normalise(user_input))
            # The strict schemas pre-fill every field of every page, so the
            # merged pages are a complete replacement for the old options.
            return self.async_create_entry(title="", data=self._pages)
        return self.async_show_form(
            step_id="forecast",
            data_schema=_forecast_schema({**self._stored, **self._pages}, strict=True),
        )
