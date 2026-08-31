"""OPC UA quality gating.

A server that reports Bad quality is stating that its own reading is
untrustworthy -- stale, sensor-faulted, or the comms dropped and this is the
last value it saw. Until 2026-08-31 the wrapper was unwrapped and the quality
DISCARDED, so a disowned reading shipped under a canonical field at full
confidence with no null state, no flag and no invariant violation. Every
downstream gate passed it, because the number itself is usually plausible --
a stale-but-in-range value is the normal shape of a dropout.

The wrapper is the only evidence the value is not a reading. These tests exist
so it cannot be dropped again.
"""

import pytest

from app.value_coercion import coerce_value


def _coerce(raw):
    value, applied, _origtype = coerce_value(raw)
    return value, applied


# ── the coercion layer ───────────────────────────────────────────────────────

@pytest.mark.parametrize("wrapper", [
    {"Value": 8420, "Quality": "Bad"},
    {"value": 8420, "quality": "bad"},
    {"Value": 8420, "Quality": "Bad_NoCommunication"},
    {"Value": 8420, "Quality": "BAD_OUT_OF_SERVICE"},
    {"Value": 8420, "StatusCode": 2147483648},        # 0x80000000, severity 2
    {"Value": 8420, "StatusCode": 3221225472},        # 0xC0000000, severity 3
    {"Value": 8420, "StatusCode": -2147483648},       # same bits, read signed
])
def test_bad_quality_is_nulled_with_a_reason(wrapper):
    value, applied = _coerce(wrapper)
    assert value is None
    assert applied == "opc_quality_bad"


@pytest.mark.parametrize("wrapper", [
    {"Value": 8420, "Quality": "Good"},
    {"value": 8420, "quality": "good"},
    {"Value": 8420, "StatusCode": 0},
])
def test_good_quality_unwraps_normally(wrapper):
    value, applied = _coerce(wrapper)
    assert value == 8420
    assert applied.startswith("unwrapped_object:")


@pytest.mark.parametrize("wrapper", [
    {"Value": 8420, "Quality": "Uncertain"},
    {"Value": 8420, "Quality": "Uncertain_LastUsableValue"},
    {"Value": 8420, "StatusCode": 1073741824},        # 0x40000000, severity 1
])
def test_uncertain_passes_but_is_reported(wrapper):
    """Uncertain is not Bad -- the server still offers the value. It passes,
    but it is REPORTED so an integrator can see which readings carried a
    caveat."""
    value, applied = _coerce(wrapper)
    assert value == 8420
    assert applied == "opc_quality_uncertain"


@pytest.mark.parametrize("wrapper,expect", [
    ({"Value": 8420}, 8420),
    ({"value": 8420}, 8420),
    ({"val": 8420}, 8420),
    ({"Value": "8420"}, 8420.0),                      # coercion still runs
])
def test_no_quality_field_is_backwards_compatible(wrapper, expect):
    """The plain {"value": x} shape predates this gate. Absent quality is NOT
    Good -- it is no claim at all -- and must unwrap exactly as it always did."""
    value, applied = _coerce(wrapper)
    assert value == expect
    assert applied.startswith("unwrapped_object:")


def test_boolean_quality_is_not_a_status_code():
    """bool is an int subclass; {"Quality": True} must not read as severity 0."""
    value, applied = _coerce({"Value": 8420, "Quality": True})
    assert value == 8420
    assert applied.startswith("unwrapped_object:")


def test_wrapper_with_no_value_key_is_still_unprocessable():
    value, applied = _coerce({"Quality": "Good"})
    assert applied == "unprocessable_object"


# ── end to end, through /v1/normalize ────────────────────────────────────────

def test_bad_quality_nulls_a_resolved_field(normalize):
    out = normalize("haas", {"SPINDLE_SPEED": {"Value": 8420, "Quality": "Bad"}})
    assert (out["normalized"] or {}).get("spindle_speed_rpm") is None
    ns = (out.get("null_states") or {}).get("spindle_speed_rpm")
    assert ns, "a Bad reading must carry a null_state, not just a null"
    assert "opc_quality_bad" in ns["null_reason"]
    assert ns["stage"] == "pre_conversion"


def test_bad_quality_nulls_an_unresolved_tag(normalize):
    """Unresolved is not a licence to ship it. The tags the kernel does not
    recognise are exactly the ones nobody has looked at."""
    out = normalize("siemens", {"Totally_Unknown_Tag": {"Value": 8420,
                                                       "Quality": "Bad"}})
    assert (out["normalized"] or {}).get("Totally_Unknown_Tag") is None
    ns = (out.get("null_states") or {}).get("Totally_Unknown_Tag")
    assert ns and "opc_quality_bad" in ns["null_reason"]


def test_good_and_bad_do_not_produce_the_same_answer(normalize):
    """The regression in one line: before the fix these were identical."""
    good = normalize("haas", {"SPINDLE_SPEED": {"Value": 8420, "Quality": "Good"}})
    bad = normalize("haas", {"SPINDLE_SPEED": {"Value": 8420, "Quality": "Bad"}})
    assert good["normalized"]["spindle_speed_rpm"] == 8420
    assert bad["normalized"]["spindle_speed_rpm"] is None


def test_bad_quality_does_not_trip_the_relief_valve(normalize):
    """A gated reading is a CLEAN response, not a rescued one. If the valve has
    to catch it, the gate above it did not do its job."""
    out = normalize("haas", {"SPINDLE_SPEED": {"Value": 8420, "Quality": "Bad"}})
    assert not out.get("_invariant_violations")
