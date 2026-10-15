"""Runtime smoke test: the entry loads and publishes HAEO-shaped entities.

Needs an environment with Home Assistant installed
(``pip install pytest-homeassistant-custom-component``); it is skipped
anywhere else so the pure-model suite still runs with plain pytest.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.ev_forecast.const import (  # noqa: E402
    ATTR_FORECAST,
    ATTR_PLANNED_TRIPS,
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
    DEFAULT_FORECAST_HOURS,
    DOMAIN,
    SERVICE_CLEAR_TRIPS,
    SERVICE_BACKFILL,
    SERVICE_PLAN_TRIP,
)

CONFIG = {
    CONF_NAME: "GAC Aion UT",
    CONF_SOC_ENTITY: "sensor.aion_ut_battery",
    CONF_PLUGGED_ENTITY: "binary_sensor.aion_ut_plugged_in",
    CONF_CAPACITY: 60.0,
    CONF_EFFICIENCY: 85.0,
    CONF_CHARGE_RATE: 3.3,
    CONF_BUFFER: 5.0,
    CONF_MAX_SOC: 90.0,
    CONF_HISTORY_DAYS: 7,
    CONF_SOC_WINDOW: 10,
    CONF_HORIZON: 72,
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let the loader find ``custom_components/ev_forecast``."""
    yield


async def setup(hass: HomeAssistant, soc: str = "40.0", plugged: str = "on") -> MockConfigEntry:
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], soc)
    hass.states.async_set(CONFIG[CONF_PLUGGED_ENTITY], plugged)
    entry = MockConfigEntry(domain=DOMAIN, title=CONFIG[CONF_NAME], data=CONFIG, version=1)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entry_publishes_haeo_shaped_entities(hass: HomeAssistant) -> None:
    entry = await setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.gac_aion_ut_charge_rate").state == "3.3"
    assert hass.states.get("sensor.gac_aion_ut_plugged_flag").state == "1"
    # No history yet: the 5 kWh buffer over a 60 kWh pack is the whole floor.
    assert hass.states.get("sensor.gac_aion_ut_soc_required").state == "8.3"

    forecast = hass.states.get("sensor.gac_aion_ut_charge_rate_forecast")
    points = forecast.attributes[ATTR_FORECAST]
    assert len(points) == DEFAULT_FORECAST_HOURS
    stamp = dt_util.parse_datetime(points[0]["time"])
    assert stamp is not None and stamp.utcoffset() is not None
    assert (stamp.minute, stamp.second, stamp.microsecond) == (0, 0, 0)
    # Plugged now with no history: the configured rate carries every hour.
    assert {point["value"] for point in points} == {3.3}

    for service in (SERVICE_BACKFILL, SERVICE_PLAN_TRIP, SERVICE_CLEAR_TRIPS):
        assert hass.services.has_service(DOMAIN, service)


async def test_plan_trip_service_feeds_the_drive_forecast(hass: HomeAssistant) -> None:
    await setup(hass, plugged="off")
    tomorrow = (dt_util.now() + timedelta(days=1)).replace(
        hour=16, minute=0, second=0, microsecond=0
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_PLAN_TRIP,
        {"date": tomorrow.strftime("%Y-%m-%d"), "start_hour": 16, "hours": 2, "kwh": 12.0},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("sensor.gac_aion_ut_drive_power_forecast")
    values = {point["time"]: point["value"] for point in state.attributes[ATTR_FORECAST]}
    assert values[tomorrow.isoformat()] == 6.0
    assert values[(tomorrow + timedelta(hours=1)).isoformat()] == 6.0
    assert values[(tomorrow + timedelta(hours=2)).isoformat()] == 0.0
    assert state.attributes[ATTR_PLANNED_TRIPS] == [
        {"date": tomorrow.strftime("%Y-%m-%d"), "start_hour": 16, "hours": 2, "kwh": 12.0}
    ]

    # A trip tomorrow at 16:00 is inside the 72 h sizing horizon, so the floor
    # now has to cover it: (12 + 5) kWh over 60 kWh.
    assert hass.states.get("sensor.gac_aion_ut_soc_required").state == "28.3"

    await hass.services.async_call(DOMAIN, SERVICE_CLEAR_TRIPS, {}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.gac_aion_ut_soc_required").state == "8.3"


async def test_soc_drop_accumulates_drive_energy(hass: HomeAssistant) -> None:
    entry = await setup(hass, plugged="off")
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "39.5")  # 0.5 % of 60 kWh
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("sensor.gac_aion_ut_drive_energy").state == "0.3"
    assert hass.states.get("sensor.gac_aion_ut_drive_energy_daily").state == "0.3"
    assert float(hass.states.get("sensor.gac_aion_ut_drive_power").state) > 0.0
    assert float(hass.states.get("sensor.gac_aion_ut_soc_power").state) < 0.0


async def test_config_flow_answers_a_partial_submission_with_form_errors(
    hass: HomeAssistant,
) -> None:
    """Missing source entities re-show the form; they never fail validation."""
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "40.0")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY]},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_PLUGGED_ENTITY: "entity_not_found"}


async def test_config_flow_types_form_output_and_derives_a_title(
    hass: HomeAssistant,
) -> None:
    """Strings from the UI become numbers, and a blank name still sizes entities."""
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "40.0", {"friendly_name": "Aion UT"})
    hass.states.async_set(CONFIG[CONF_PLUGGED_ENTITY], "on")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={
            CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY],
            CONF_PLUGGED_ENTITY: CONFIG[CONF_PLUGGED_ENTITY],
            CONF_CAPACITY: "60",
            CONF_HORIZON: "72",
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    entry = hass.config_entries.async_get_entry(result["entry_id"])
    assert entry.title == "Aion UT"
    assert entry.unique_id == CONFIG[CONF_SOC_ENTITY]
    assert entry.data[CONF_NAME] == "Aion UT"
    assert entry.data[CONF_HORIZON] == 72
    assert isinstance(entry.data[CONF_HORIZON], int)
    assert entry.data[CONF_CAPACITY] == 60.0
    assert isinstance(entry.data[CONF_CAPACITY], float)


async def test_config_flow_aborts_when_the_soc_sensor_is_claimed(
    hass: HomeAssistant,
) -> None:
    """One usage model per state-of-charge sensor."""
    await setup(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={
            CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY],
            CONF_PLUGGED_ENTITY: CONFIG[CONF_PLUGGED_ENTITY],
        },
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_applies_a_new_charge_rate_live(
    hass: HomeAssistant,
) -> None:
    """Tuning the model does not need the entry recreated."""
    entry = await setup(hass)
    form = await hass.config_entries.options.async_init(entry.entry_id)
    assert form["type"] == FlowResultType.FORM
    assert form["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        form["flow_id"], user_input={CONF_CHARGE_RATE: 7.4}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_CHARGE_RATE] == 7.4
    assert hass.states.get("sensor.gac_aion_ut_charge_rate").state == "7.4"
