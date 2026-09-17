"""generic_water pack in the sandbox — resolution, units, bounds.

Trimmed port of forge-prod tests/test_water_vertical.py. The sandbox carries its
OWN copy of unit_converter/value_validator, so the three suffix collisions the
prod pack hit exist here independently and have to be asserted here too:

    influent_flow_m3_h  ended `_h`  -> HOURS      conductivity_us_cm -> CENTIMETRES
    orp_mv              ended `_v`  -> VOLTS (1000x)
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app import corpus                        # noqa: E402
from app import unit_converter as uc          # noqa: E402
from app import value_validator as vv         # noqa: E402

ALIASES = ["generic_water", "water_treatment", "wastewater",
           "municipal_water", "wtp", "wwtp", "water"]


@pytest.fixture(scope="module")
def pack():
    with open(os.path.join(_ROOT, "app", "packs", "generic_water.json")) as f:
        return json.load(f)


def test_pack_loads_and_every_alias_resolves():
    packs, by_alias, _d = corpus.load()
    assert "generic_water" in packs
    for a in ALIASES:
        assert by_alias.get(a) == "generic_water", f"alias {a} does not resolve"


def test_pack_is_in_the_public_index(pack):
    with open(os.path.join(_ROOT, "app", "packs", "_index.json")) as f:
        idx = json.load(f)["packs"]
    assert idx["generic_water"]["mapping_count"] == len(pack["mappings"])
    assert idx["generic_water"]["vertical"] == "water"


def test_every_canonical_target_is_in_the_public_dictionary(pack):
    _p, _a, d = corpus.load()
    missing = sorted(c for c in set(pack["mappings"].values()) if c not in d["fields"])
    assert missing == [], f"pack targets absent from the dictionary: {missing}"


@pytest.mark.parametrize("field,unit,quantity", [
    ("influent_flow_m3_h",    "m3/h",  "flow"),          # NOT hours
    ("conductivity_us_cm",    "uS/cm", "conductivity"),  # NOT centimetres
    ("orp_mv",                "mV",    "voltage"),       # NOT volts
    ("basin_level_m",         "m",     "length"),
    ("total_flow_m3",         "m3",    "volume"),
    ("dissolved_oxygen_mg_l", "mg/L",  "concentration"),
    ("turbidity_ntu",         "NTU",   "turbidity"),
    ("system_pressure_kpa",   "kPa",   "pressure"),
])
def test_canonical_name_declares_the_right_unit(field, unit, quantity):
    assert uc.field_declared_unit(field) == unit
    assert uc.quantity_of(unit) == quantity


def test_global_si_targets_unchanged():
    assert uc.TARGET_UNIT["flow"] == "L/min"
    assert uc.TARGET_UNIT["pressure"] == "bar"
    assert uc.TARGET_UNIT["length"] == "mm"


def test_every_whitelisted_suffix_token_resolves():
    unresolved = sorted(t for t in uc._UNAMBIGUOUS_SUFFIX
                        if uc.UNIT_ALIASES.get(t) is None)
    assert unresolved == []


@pytest.mark.parametrize("tag,raw,field,expected", [
    ("Influent_Flow_MGD",       10.0,        "influent_flow_m3_h",      1577.25),
    ("Effluent_Flow_GPM",       1000.0,      "effluent_flow_m3_h",      227.125),
    ("Flow_Rate_CFS",           10.0,        "flow_rate_m3_h",          1019.41),
    ("Blower_Airflow_SCFM",     500.0,       "blower_airflow_m3_h",     849.505),
    ("Totalizer_gal",           1_000_000.0, "total_flow_m3",           3785.41),
    ("Totalizer_MG",            1.0,         "total_flow_m3",           3785.41),
    ("Basin_Level_ft",          12.0,        "basin_level_m",           3.6576),
    ("System_Pressure_PSI",     60.0,        "system_pressure_kpa",     413.685),
    ("Blower_Discharge_Temp_F", 212.0,       "blower_discharge_temp_c", 100.0),
    ("Conductivity_mS_cm",      1.5,         "conductivity_us_cm",      1500.0),
])
def test_wire_units_convert(tag, raw, field, expected):
    p = corpus.get_pack("generic_water")
    canon = p.mappings[tag]
    assert canon == field
    out, rec = uc.convert_value(tag, raw, canon, source_unit=p.tag_units.get(tag))
    assert out == pytest.approx(expected, rel=1e-4)
    if raw != expected:
        assert rec and rec.get("converted") is True


def test_million_gallons_is_declared_not_guessed(pack):
    """`_MG` is Million Gallons on a totalizer and milligrams on every chemistry
    tag on the same plant."""
    assert pack["tag_units"]["Totalizer_MG"] == "MG"
    assert "mg" not in uc._UNAMBIGUOUS_SUFFIX


@pytest.mark.parametrize("field", [
    "turbidity_ntu", "dissolved_oxygen_mg_l", "orp_mv", "digester_temp_c",
    "basin_level_m", "svi_ml_g", "system_pressure_kpa", "influent_flow_m3_h",
])
def test_water_fields_have_read_time_bounds(field):
    assert vv.physics_bounds_for(field) is not None


def test_orp_bounds_are_signed():
    lo, hi = vv.physics_bounds_for("orp_mv")
    assert lo < 0 < hi


def test_a_300_psi_booster_survives():
    """The `pressure` quantity default is (0, 1500) in BAR; a kPa field
    inheriting it would null 2068 kPa as a physics violation."""
    out, _ = uc.convert_value("System_Pressure_PSI", 300.0, "system_pressure_kpa")
    assert vv.validate_bounds("system_pressure_kpa", out).get("value") is not None


@pytest.mark.parametrize("field,bad", [
    ("ph", 9999.0), ("dissolved_oxygen_mg_l", 850.0), ("digester_temp_c", 250.0),
])
def test_out_of_range_is_nulled(field, bad):
    assert vv.validate_bounds(field, bad).get("value") is None


# ── coverage honesty (FIX 5) ────────────────────────────────────────────────
# `coverage_pct` counts anything that resolved at all, so a moderate-evidence
# guess at 0.65 counts the same as a curated 1.0 corpus hit. The honest number
# counts only >= 0.78, the same threshold production uses. A prospect quotes
# whichever number the engine hands them, so both are reported.

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_both_coverage_numbers_are_reported():
    r = _client().post("/v1/normalize",
                       json={"oem": "haas", "data": {"S1Temp": 72.1, "SP_SPEED": 5204}}).json()
    assert r["coverage_pct"] == 100.0
    assert r["coverage_honest_pct"] == 100.0
    assert r["coverage_honest_min_confidence"] == 0.78
    assert r["fields_low_confidence"] == 0


def test_honest_coverage_excludes_moderate_evidence():
    """The evidence gate resolves at 0.65 ('moderate'), which the naive number
    counts and the honest one does not. If these two can never diverge the
    honest number is decoration."""
    data = {f"TAG_{i}": float(i) + 0.5 for i in range(6)}
    data.update({"S1Temp": 72.1, "Spindle Load %": 62, "coolant temp": 31.2, "AxisPos": 12.4})
    r = _client().post("/v1/normalize", json={"oem": "", "data": data}).json()
    assert r["coverage_honest_pct"] < r["coverage_pct"], \
        "honest coverage never diverges from naive — the threshold is doing nothing"
    assert r["fields_low_confidence"] > 0


def test_honest_coverage_is_never_above_naive():
    for oem, data in [("haas", {"S1Temp": 72.1}),
                      ("wwtp", {"pH": 7.2, "MLSS": 3250, "Influent_Flow_MGD": 12.4}),
                      ("", {"AX7": 41.2, "ZZ_9": 880.0})]:
        r = _client().post("/v1/normalize", json={"oem": oem, "data": data}).json()
        assert r["coverage_honest_pct"] <= r["coverage_pct"]
