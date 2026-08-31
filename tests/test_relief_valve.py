"""Relief valve -- the output invariant checker.

A last gate on the FINISHED response, after the whole pipeline, on every field:
resolved, unresolved, coerced or passed through. It knows nothing about
resolution, channels, folds or packs. It knows only what must never be true of
an answer handed to a caller.

  1  No tier-1 wire sentinel survives (65535, 32767, -32768, +-2^31, 2^32-1)
  2  No string or boolean in a field the schema declares numeric
  3  No dict, list or other raw structure as a value
  4  No value outside its declared physics bounds
  5  No NaN, no Infinity
  6  fields_total == fields_mapped + fields_unknown
  7  No blank field name

The valve is a BACKSTOP. On a legitimate payload it must stay silent -- a
non-zero `_invariant_violations` means a gate above it failed and the valve had
to rescue the response. Corrections are reported, never silent, so a clean
answer stays distinguishable from a rescued one.

Reference: RELIEF_VALVE_TEST.md (2026-08-27).
"""

import json
import math
import os

import pytest

from conftest import FIXTURES

SENTINELS = {65535, 32767, -32768, 2147483647, -2147483648, 4294967295}


def _final_boss():
    with open(os.path.join(FIXTURES, "final_boss_49.json")) as fh:
        return json.load(fh)


# Legitimate payloads. Every one of these must come back with the valve silent.
CLEAN_PAYLOADS = [
    ("siemens", {"oem": "siemens", "data": {
        "SP_LOAD": 59.3, "PART_CNT": 842, "POWER_CONSUMPTION(kWh)": 4820500}}),
    ("tesla", {"oem": "tesla", "data": {
        "Pack_SOC": 73.2, "Pack_Energy_Wh": 3200000, "Cell_Temp_Max": 33.0}}),
    ("haas", {"oem": "haas", "data": {
        "SPINDLE_SPEED": 8420, "SPINDLE_LOAD": 59.3, "COOLANT_TEMP": 94.6}}),
    ("fronius", {"oem": "fronius", "data": {
        "PAC": 48700, "E_Total": 91240000, "F_AC": 59.98}}),
    ("schneider", {"oem": "schneider", "data": {
        "ActivePower": 412.6, "TotalEnergy": 88400, "VoltageLL": 480.2}}),
    ("solaredge", {"oem": "solaredge", "data": {
        "M_AC_Power": 412600, "M_AC_Freq": 60.01, "M_AC_Voltage_LL": 479.8}}),
    ("sentinel", {"oem": "tesla", "data": {"Pack_Energy_Wh": 65535}}),
    ("physics", {"oem": "haas", "data": {"COOLANT_TEMP": 9999}}),
    ("final-boss-49", _final_boss()),
]


@pytest.fixture(scope="module")
def responses(client):
    out = {}
    for name, body in CLEAN_PAYLOADS:
        r = client.post("/v1/normalize", json=body)
        assert r.status_code == 200, f"{name}: HTTP {r.status_code}"
        out[name] = r.json()
    return out


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_valve_stays_silent_on_a_legitimate_payload(responses, name):
    """The headline number: 0 violations across the whole set."""
    assert not responses[name].get("_invariant_violations")


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_invariant_1_no_sentinel_survives(responses, name):
    leaked = {k: v for k, v in (responses[name]["normalized"] or {}).items()
              if v in SENTINELS}
    assert not leaked, f"{name}: {leaked}"


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_invariant_3_no_raw_structure(responses, name):
    bad = {k: type(v).__name__ for k, v in (responses[name]["normalized"] or {}).items()
           if isinstance(v, (dict, list))}
    assert not bad, f"{name}: {bad}"


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_invariant_5_no_nan_or_infinity(responses, name):
    bad = [k for k, v in (responses[name]["normalized"] or {}).items()
           if isinstance(v, float) and (math.isnan(v) or math.isinf(v))]
    assert not bad, f"{name}: {bad}"


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_invariant_6_field_accounting(responses, name):
    out = responses[name]
    assert out["fields_total"] == out["fields_mapped"] + out["fields_unknown"]


@pytest.mark.parametrize("name", [n for n, _ in CLEAN_PAYLOADS])
def test_invariant_7_no_blank_field_name(responses, name):
    blank = [k for k in (responses[name]["normalized"] or {}) if not str(k).strip()]
    assert not blank, f"{name}: {blank}"


# -- the valve actually firing -----------------------------------------------

def test_valve_catches_a_deliberately_malformed_payload(client):
    """The other half of the guarantee. Given genuine garbage the valve must
    fire, REPORT that it fired, and give each rescued field a reason -- so a
    rescued response is never mistaken for a clean one."""
    r = client.post("/v1/normalize", json={"oem": "siemens", "data": {
        "": 1,
        "weird": {"a": 1},
        "nan_ish": "NaN",
        "big": 4294967295,
        "neg": -32768,
    }})
    assert r.status_code == 200
    out = r.json()

    # Invariant 7: the blank field name is removed and the removal is reported.
    assert not [k for k in (out["normalized"] or {}) if not str(k).strip()]
    assert out.get("_invariant_violations"), \
        "the valve rescued the response without saying so"

    # Every other garbage field is nulled WITH a reason, not silently dropped.
    ns = out.get("null_states") or {}
    for field, fragment in (("weird", "unprocessable_object"),
                            ("nan_ish", "sentinel"),
                            ("big", "sentinel"),
                            ("neg", "sentinel")):
        assert field in ns, f"{field} was not given a null_state"
        assert fragment in ns[field]["null_reason"], \
            f"{field}: {ns[field]['null_reason']}"


def test_a_rescued_response_is_distinguishable_from_a_clean_one(client, responses):
    """The whole point of reporting corrections."""
    dirty = client.post("/v1/normalize", json={
        "oem": "siemens", "data": {"": 1}}).json()
    assert dirty.get("_invariant_violations")
    assert not responses["haas"].get("_invariant_violations")
