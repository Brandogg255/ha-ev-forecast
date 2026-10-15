"""Runtime smoke test: the entry loads and publishes HAEO-shaped entities.

Needs an environment with Home Assistant installed
(``pip install pytest-homeassistant-custom-component``); it is skipped
anywhere else so the pure-model suite still runs with plain pytest.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

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
    SERVICE_CLEAR_TRIPS,
    SERVICE_BACKFILL,
    SERVICE_PLAN_TRIP,
)
from custom_components.ev_forecast.model import (  # noqa: E402
    FIELD_DRIVE_KWH,
    FIELD_PLUGED_HOURS,
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
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=CONFIG[CONF_NAME],
        data=CONFIG,
        version=1,
        unique_id=CONFIG[CONF_SOC_ENTITY],
    )
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


async def test_backfill_service_replays_a_long_run_at_the_live_cadence(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six hours of history becomes six plugged hours, not one capped bucket."""
    entry = await setup(hass, plugged="off")
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    def fake_history(hass, start_time, end_time, entity_ids, *args, **kwargs):
        # A wallbox that reported ``on`` for six hours, and an SoC sensor that
        # spoke every five minutes - what the recorder actually holds.
        grid = end_time - timedelta(hours=6)
        run_start = grid.replace(minute=0, second=0, microsecond=0)
        steps = range(0, 73)
        return {
            CONFIG[CONF_SOC_ENTITY]: [
                SimpleNamespace(
                    last_changed=run_start + timedelta(minutes=5 * step),
                    state=f"{40.0 - 0.05 * step:.2f}",
                )
                for step in steps
            ],
            CONFIG[CONF_PLUGGED_ENTITY]: [
                SimpleNamespace(last_changed=run_start, state="on"),
                SimpleNamespace(
                    last_changed=run_start + timedelta(hours=6), state="off"
                ),
            ],
        }

    monkeypatch.setattr(
        "homeassistant.components.recorder.history.get_significant_states", fake_history
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BACKFILL,
        {"entry_id": entry.entry_id, "days": 1},
        blocking=True,
    )
    await hass.async_block_till_done()

    hours = coordinator.store.hours
    plugged = [bucket.get(FIELD_PLUGED_HOURS, 0.0) for bucket in hours.values()]
    # The old replay credited one capped bucket for the whole run.
    assert sum(plugged) == pytest.approx(6.0, abs=0.05)
    assert sum(value > 0.99 for value in plugged) == 5
    # The 3.6 % ramp is spread over the hours it fell in rather than dumped in
    # the hour it finished.
    assert sum(bucket.get(FIELD_DRIVE_KWH, 0.0) for bucket in hours.values()) == (
        pytest.approx(2.16, abs=0.05)
    )
    assert sum(1 for bucket in hours.values() if bucket.get(FIELD_DRIVE_KWH, 0) > 0.01) >= 6

    # The granular buckets are what make the plugged forecast usable by HAEO:
    # six hour-of-day slots light up, and they are the same six the store holds.
    points = hass.states.get(
        "sensor.gac_aion_ut_plugged_flag_forecast"
    ).attributes[ATTR_FORECAST]
    plugged_slots = {
        dt_util.parse_datetime(point["time"]).hour
        for point in points
        if point["value"] > 0.5
    }
    bucket_slots = {
        dt_util.parse_datetime(key).hour
        for key, bucket in hours.items()
        if bucket.get(FIELD_PLUGED_HOURS, 0) > 0.5
    }
    assert plugged_slots == bucket_slots
    assert len(bucket_slots) == 6


async def test_config_flow_answers_a_bad_charger_page_with_form_errors(
    hass: HomeAssistant,
) -> None:
    """A page that names a missing entity re-shows itself; validation never 400s."""
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "40.0")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY]},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PLUGGED_ENTITY: "binary_sensor.not_there"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"
    assert result["errors"] == {CONF_PLUGGED_ENTITY: "entity_not_found"}


async def test_config_flow_requires_state_lists_for_a_status_sensor(
    hass: HomeAssistant,
) -> None:
    """A word-valued plugged entity needs lists, or every state reads unplugged."""
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "40.0")
    hass.states.async_set("sensor.de_ev_charger_status", "plugged_in")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY]},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PLUGGED_ENTITY: "sensor.de_ev_charger_status"},
    )
    assert result["errors"] == {CONF_PLUGGED_STATES: "states_required"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_PLUGGED_ENTITY: "sensor.de_ev_charger_status",
            CONF_PLUGGED_STATES: ["plugged_in", "waiting", "Charging"],
        },
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast"


async def test_config_flow_walks_three_pages_and_types_the_form_output(
    hass: HomeAssistant,
) -> None:
    """Strings from the UI become numbers, and a blank name still sizes entities."""
    hass.states.async_set(CONFIG[CONF_SOC_ENTITY], "40.0", {"friendly_name": "Aion UT"})
    hass.states.async_set(CONFIG[CONF_PLUGGED_ENTITY], "on")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY], CONF_CAPACITY: "60"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "charger"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PLUGGED_ENTITY: CONFIG[CONF_PLUGGED_ENTITY]},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "forecast"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_HORIZON: "72"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    # ``async_finish_flow`` attaches the stored entry as ``result``; there is no
    # ``entry_id`` key on a config-flow create result.
    entry = result["result"]
    assert entry.title == "Aion UT"
    assert entry.unique_id == CONFIG[CONF_SOC_ENTITY]
    assert entry.data[CONF_NAME] == "Aion UT"
    assert entry.data[CONF_HORIZON] == 72
    assert isinstance(entry.data[CONF_HORIZON], int)
    assert entry.data[CONF_CAPACITY] == 60.0
    assert isinstance(entry.data[CONF_CAPACITY], float)
    # Keys the pages left at their defaults still reach the entry, so a page
    # can never overwrite another page's value with its own default.
    assert entry.data[CONF_PLUGGED_ENTITY] == CONFIG[CONF_PLUGGED_ENTITY]
    assert entry.data[CONF_EFFICIENCY] == 85.0
    assert entry.data[CONF_SOC_WINDOW] == 10
    assert entry.data[CONF_HISTORY_DAYS] == 7


async def test_config_flow_aborts_when_the_soc_sensor_is_claimed(
    hass: HomeAssistant,
) -> None:
    """One usage model per state-of-charge sensor, caught on the first page."""
    await setup(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
        data={CONF_SOC_ENTITY: CONFIG[CONF_SOC_ENTITY]},
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
    assert result["step_id"] == "charger"
    # The strict pages pre-fill everything, so an empty submission keeps the
    # stored values.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["step_id"] == "forecast"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_CHARGE_RATE] == 7.4
    assert hass.states.get("sensor.gac_aion_ut_charge_rate").state == "7.4"


async def test_options_flow_revalidates_the_charger_page(hass: HomeAssistant) -> None:
    """Retuning cannot point the model at an entity that is gone or uninterpretable."""
    entry = await setup(hass)
    hass.states.async_set("sensor.de_ev_charger_status", "plugged_in")
    form = await hass.config_entries.options.async_init(entry.entry_id)
    step = await hass.config_entries.options.async_configure(
        form["flow_id"], user_input={}
    )
    assert step["step_id"] == "charger"

    missing = await hass.config_entries.options.async_configure(
        step["flow_id"], user_input={CONF_PLUGGED_ENTITY: "sensor.gone_away"}
    )
    assert missing["type"] == FlowResultType.FORM
    assert missing["step_id"] == "charger"
    assert missing["errors"] == {CONF_PLUGGED_ENTITY: "entity_not_found"}

    lists = await hass.config_entries.options.async_configure(
        step["flow_id"], user_input={CONF_PLUGGED_ENTITY: "sensor.de_ev_charger_status"}
    )
    assert lists["errors"] == {CONF_PLUGGED_STATES: "states_required"}

    accepted = await hass.config_entries.options.async_configure(
        step["flow_id"],
        user_input={
            CONF_PLUGGED_ENTITY: "sensor.de_ev_charger_status",
            CONF_PLUGGED_STATES: ["plugged_in", "waiting", "charged", "paused"],
            CONF_UNPLUGGED_STATES: ["sleep", "idle"],
            CONF_CHARGING_STATES: ["charging"],
        },
    )
    assert accepted["step_id"] == "forecast"
    result = await hass.config_entries.options.async_configure(
        accepted["flow_id"], user_input={}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_PLUGGED_ENTITY] == "sensor.de_ev_charger_status"
    # ``_normalise`` folds ``_`` and ``-`` to spaces, which is what lets a user
    # typing ``plugged in`` match a charger that reports ``plugged_in``.
    assert entry.options[CONF_PLUGGED_STATES] == [
        "plugged in",
        "waiting",
        "charged",
        "paused",
    ]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.gac_aion_ut_plugged_flag").state == "1"


@pytest.mark.parametrize(
    ("status", "plugged_flag", "charging_flag", "charge_rate"),
    [
        ("charging", "1", "1", "3.3"),
        ("paused", "1", "0", "3.3"),
        ("sleep", "0", "0", "0.0"),
        ("unknown", "0", "0", "0.0"),
    ],
)
async def test_status_sensor_maps_configured_state_buckets(
    hass: HomeAssistant,
    status: str,
    plugged_flag: str,
    charging_flag: str,
    charge_rate: str,
) -> None:
    """A word-valued charger status sensor drives the same signals as a binary one.

    The charging list is deliberately written ``Charging`` to pin that the
    lists are matched case-insensitively.
    """
    name = f"Wallbox {status}"
    soc_entity = f"sensor.wallbox_{status}_soc"
    hass.states.async_set(soc_entity, "40.0")
    hass.states.async_set("sensor.de_ev_charger_status", status)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=name,
        data={
            **CONFIG,
            CONF_NAME: name,
            CONF_SOC_ENTITY: soc_entity,
            CONF_PLUGGED_ENTITY: "sensor.de_ev_charger_status",
            CONF_PLUGGED_STATES: ["plugged_in", "waiting", "charged", "paused"],
            CONF_UNPLUGGED_STATES: ["sleep", "idle"],
            CONF_CHARGING_STATES: ["Charging"],
        },
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    prefix = f"sensor.wallbox_{status}"
    assert hass.states.get(f"{prefix}_plugged_flag").state == plugged_flag
    assert hass.states.get(f"{prefix}_charging_flag").state == charging_flag
    assert hass.states.get(f"{prefix}_charge_rate").state == charge_rate
