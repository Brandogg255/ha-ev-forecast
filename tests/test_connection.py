"""Unit tests for the connection-status classifier.

The state lists are the only place the integration interprets a charger's
vocabulary, so they are pinned here without a running instance. The example
mapping is the one the owner's de wallbox reports.
"""

from custom_components.ev_forecast.model import (
    classify_connection,
    normalise_state_list,
)

# sensor.de_ev_charger_status vocabulary.
PLUGGED = ("plugged_in", "waiting", "charged", "paused")
UNPLUGGED = ("sleep", "idle")
CHARGING = ("charging",)


def test_binary_sensor_semantics_when_lists_are_empty():
    assert classify_connection("on") == (True, True)
    assert classify_connection("off") == (False, False)
    assert classify_connection("unknown") == (False, False)
    assert classify_connection(None) == (False, False)


def test_status_sensor_buckets():
    cases = {
        "plugged in": (True, False),
        "waiting": (True, False),
        "charged": (True, False),
        "paused": (True, False),
        "charging": (True, True),
        "sleep": (False, False),
        "idle": (False, False),
        "unknown": (False, False),
        "unavailable": (False, False),
        "fault": (False, False),
    }
    for state, expected in cases.items():
        assert classify_connection(state, PLUGGED, UNPLUGGED, CHARGING) == expected, state


def test_charging_implies_plugged_even_when_unlisted():
    plugged, charging = classify_connection("charging", (), UNPLUGGED, CHARGING)
    assert (plugged, charging) == (True, True)


def test_unplugged_list_wins_over_the_other_two():
    assert classify_connection("charging", PLUGGED, ("charging",), CHARGING) == (False, False)
    assert classify_connection("paused", PLUGGED, ("paused",), CHARGING) == (False, False)


def test_matching_ignores_case_and_surrounding_space():
    assert classify_connection("  Plugged In ", PLUGGED, UNPLUGGED, CHARGING) == (True, False)
    assert classify_connection("CHARGING", PLUGGED, UNPLUGGED, CHARGING) == (True, True)


def test_single_list_still_classifies():
    # Only the charging vocabulary supplied: everything else reads unplugged.
    assert classify_connection("charging", (), (), CHARGING) == (True, True)
    assert classify_connection("waiting", (), (), CHARGING) == (False, False)


def test_normalise_state_list_cleans_user_input():
    assert normalise_state_list([" Plugged In ", "plugged in", "", None, "Sleep"]) == (
        "plugged in",
        "sleep",
    )
    assert normalise_state_list("idle") == ("idle",)
    assert normalise_state_list(None) == ()
    assert normalise_state_list(()) == ()


def test_separators_are_folded_in_both_directions():
    # The charger reports ``plugged_in``; a user typing "plugged in" still hits.
    assert classify_connection("plugged_in", ("plugged in",), UNPLUGGED, CHARGING) == (
        True,
        False,
    )
    assert classify_connection("plugged in", PLUGGED, UNPLUGGED, CHARGING) == (True, False)
    assert normalise_state_list(["plugged_in", "plugged in", "Fast-Charge"]) == (
        "plugged in",
        "fast charge",
    )


def test_unlisted_charger_state_reads_disconnected():
    # ``fault`` is one of the charger's own options but in nobody's bucket.
    assert classify_connection("fault", PLUGGED, UNPLUGGED, CHARGING) == (False, False)
