"""Energy vertical + the F1/F2 fixes from the 2026-08-22 microgrid simulation.

F1 and F2 are both ORDERING/CONTEXT bugs, not arithmetic bugs. Every one of
these must exercise the real normalization pipeline: `validate_value` in
isolation always caught the F1 sentinel, which is exactly why the bug survived.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.corpus import normalize_row
from app.unit_converter import declared_unit, convert_value
from app.value_validator import validate_sentinel, validate_bounds, ALWAYS_SENTINEL_NUMBERS


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


# ── F1: sentinel before conversion ──────────────────────────────────────────

def test_f1_uint16_sentinel_caught_before_conversion():
    """65535 Wh must be nulled as a wire sentinel, NOT converted to 65.535 kWh."""
    norm, _, _, convs, _, nulls, _ = normalize_row(
        {"Pack_Energy_Wh": 65535}, oem="tesla")
    assert norm["battery_capacity_kwh"] is None
    assert nulls["battery_capacity_kwh"]["null_state"] is True
    assert "65535" in nulls["battery_capacity_kwh"]["null_reason"]
    assert nulls["battery_capacity_kwh"]["stage"] == "pre_conversion"
    # and the conversion must never have run
    assert not [c for c in convs if c.get("converted")]


def test_f1_sentinel_would_survive_conversion_if_order_were_wrong():
    """Pins the reason the order matters: post-conversion the value is clean."""
    assert 65535 in ALWAYS_SENTINEL_NUMBERS
    assert validate_sentinel("battery_capacity_kwh", 65535)["null_state"] is True
    converted, _ = convert_value("Pack_Energy_Wh", 65535, "battery_capacity_kwh")
    assert converted == 65.535
    assert validate_sentinel("battery_capacity_kwh", converted)["null_state"] is False
    assert validate_bounds("battery_capacity_kwh", converted)["null_state"] is False


@pytest.mark.parametrize("sentinel", sorted(ALWAYS_SENTINEL_NUMBERS))
def test_f1_every_tier1_sentinel_survives_a_converting_field(sentinel):
    norm, _, _, _, _, nulls, _ = normalize_row(
        {"Pack_Energy_Wh": sentinel}, oem="tesla")
    assert norm["battery_capacity_kwh"] is None, f"{sentinel} leaked through"
    assert nulls["battery_capacity_kwh"]["stage"] == "pre_conversion"


def test_f1_bounds_still_run_after_conversion():
    """A real (non-sentinel) value out of physical range is still caught, and
    the bounds are applied to the CONVERTED value in canonical units."""
    norm, _, _, _, _, nulls, _ = normalize_row(
        {"Cell_Temp_Max_F": 9999.0}, oem="tesla")
    assert norm["battery_cell_temp_max_c"] is None
    assert nulls["battery_cell_temp_max_c"]["stage"] == "post_conversion"
    assert "physics_violation" in nulls["battery_cell_temp_max_c"]["null_reason"]


def test_f1_valid_fahrenheit_not_rejected_by_celsius_bounds():
    """The discriminating case for WHY bounds run last.

    battery_cell_temp_max_c is bounded [-40, 100] in CELSIUS. A thermal-runaway
    reading of 200 F is 93.3 C — a real, in-range value. Checked before
    conversion, the raw 200 would blow the Celsius ceiling and a genuine reading
    would be nulled. That is why stage 1 is sentinels only.
    """
    norm, _, _, _, _, nulls, _ = normalize_row({"Cell_Temp_Max_F": 200.0}, oem="tesla")
    assert norm["battery_cell_temp_max_c"] == 93.333333
    assert "battery_cell_temp_max_c" not in nulls
    # sanity: the same number IS out of range when it really is Celsius
    assert validate_bounds("battery_cell_temp_max_c", 200.0)["null_state"] is True


# ── F2: identical detection across tag formats ──────────────────────────────

F_FORMS = ["Cell_Temp_Max_F", "Cell_Temp_Max (degF)", "Cell_Temp_Max (°F)",
           "Cell_Temp_Max_degF", "Cell_Temp_Max [F]"]


@pytest.mark.parametrize("tag", F_FORMS)
def test_f2_all_fahrenheit_spellings_detect_the_same_unit(tag):
    assert declared_unit(tag) == "F", f"{tag} did not read as Fahrenheit"


def test_f2_all_fahrenheit_spellings_normalize_identically():
    outs = set()
    for tag in F_FORMS:
        norm, _, _, _, _, _, _ = normalize_row({tag: 91.4}, oem="tesla")
        outs.add(norm.get("battery_cell_temp_max_c"))
    assert outs == {33.0}, f"tag format changed the stored value: {outs}"


def test_f2_the_original_60_degree_divergence_is_gone():
    """The exact F2 reproduction: bare _f vs (degF) on the weather station."""
    a, _, _, _, _, _, _ = normalize_row({"ambient_temp_f": 94.6}, oem="generic_iot")
    b, _, _, _, _, _, _ = normalize_row({"ambient_temp (degF)": 94.6}, oem="generic_iot")
    assert a["ambient_temperature_c"] == b["ambient_temperature_c"] == 34.777778


@pytest.mark.parametrize("tag", ["axis_pos_c", "status_f", "part_count_a",
                                 "flag_v", "job_c", "spindle_load"])
def test_f2_bare_suffix_without_context_stays_unresolved(tag):
    """The context gate must not turn every trailing letter into a unit."""
    assert declared_unit(tag) is None


def test_f2_explicit_tag_unit_beats_the_pack_default():
    """generic_iot declares panel_temp as F. A tag saying degC must still win."""
    norm, _, _, _, _, _, _ = normalize_row({"panel_temp (degC)": 61.6}, oem="generic_iot")
    assert norm["panel_temperature_c"] == 61.6          # unchanged, not F->C'd


def test_f2_pack_declared_unit_fills_a_silent_tag():
    """SunSpec `W` has no suffix to read; the pack says watts."""
    norm, _, _, convs, _, _, _ = normalize_row({"W": 48700}, oem="fronius")
    assert norm["inverter_output_kw"] == 48.7
    assert [c for c in convs if c["raw_field"] == "W"][0]["unit_source"] == "pack"


# ── energy vertical coverage ────────────────────────────────────────────────

DEVICES = {
    "tesla": {"SOC_pct": 73.2, "SOH_pct": 96.1, "DC_Bus_V": 814.3,
              "Chrg_Rate_kW": 0.0, "Dischrg_Rate_kW": 247.8,
              "Cell_Temp_Max_F": 91.4, "Cycles": 1847, "Pack_Energy_Wh": 3200000},
    "fronius": {"W": 48700, "WH": 12847500, "DCA": 62.3, "DCV": 782.1,
                "Hz": 60.01, "TmpCab": 47.2, "St": 4, "Evt1": 0},
    "schneider": {"kW_Total": 312.7, "kVAR_Total": -42.1, "PF_Avg": 0.991,
                  "V_LL_Avg": 481.2, "I_Avg_A": 376.4, "Freq_Hz": 60.01,
                  "kWh_Del": 4287650, "kWh_Rec": 187420, "Demand_kW_Peak": 487.3},
    "generic_iot": {"irradiance_w_m2": 847.3, "ambient_temp_f": 94.6,
                    "wind_speed_mph": 7.2, "humidity_pct": 23.1, "panel_temp_f": 142.8},
}


@pytest.mark.parametrize("oem,data", sorted(DEVICES.items()))
def test_energy_device_fully_maps(oem, data):
    _, mappings, _, _, _, _, _ = normalize_row(data, oem=oem)
    unmapped = [t for t, m in mappings.items() if not m.get("canonical_field")]
    assert unmapped == [], f"{oem} left {unmapped} unresolved"


def test_dc_current_and_voltage_no_longer_collide():
    """F6 from the simulation: DCA and DCV both landed on digital_input and the
    voltage was dropped."""
    norm, _, _, _, collisions, _, _ = normalize_row(
        {"DCA": 62.3, "DCV": 782.1}, oem="fronius")
    assert norm["dc_current_a"] == 62.3
    assert norm["dc_voltage_v"] == 782.1
    assert not collisions


def test_inverter_state_enum_converges():
    _, _, _, _, _, _, enums = normalize_row({"St": 4}, oem="fronius")
    assert enums["inverter_state"]["value"] == "mppt"
    assert enums["inverter_state"]["matched"] is True


def test_wind_speed_converts_mph_to_m_s():
    norm, _, _, _, _, _, _ = normalize_row({"wind_speed_mph": 7.2}, oem="generic_iot")
    assert norm["wind_speed_m_s"] == 3.218688


def test_energy_fields_are_in_both_field_lists():
    """The registry and the corpus dictionary must agree, or predict_breach
    warns about a field that normalize happily emits."""
    import json, os
    from app import corpus, field_registry
    _, _, dictionary = corpus.load()
    dic = dictionary["fields"]
    reg = field_registry.fields()
    for pack_name in ("tesla", "fronius", "schneider", "generic_iot"):
        pack = corpus.get_pack(pack_name)
        for cf in pack.canonical_fields:
            assert cf in dic, f"{pack_name}: {cf} missing from corpus dictionary"
            assert cf in reg, f"{pack_name}: {cf} missing from field registry"


def test_energy_machines_are_simulatable(client):
    for m in ("tesla", "fronius", "schneider", "generic_iot"):
        r = client.get(f"/v1/simulate/{m}")
        assert r.status_code == 200, m
        assert r.json()["data"], m


def test_end_to_end_energy_normalize_is_full_coverage(client):
    for oem, data in DEVICES.items():
        r = client.post("/v1/normalize", json={"oem": oem, "data": data})
        assert r.status_code == 200
        body = r.json()
        assert body["coverage_pct"] == 100.0, f"{oem}: {body['unresolved_tags']}"


# ── irradiance is not power ─────────────────────────────────────────────────

def test_irradiance_is_not_treated_as_power():
    """W/m² shares a symbol prefix with W but is a different quantity. Without
    its own token, a tag declaring "(W)" against solar_irradiance_w_m2 resolved
    as watts, found the SI power target kW, and divided the reading by 1000."""
    from app.unit_converter import target_unit, quantity_of
    assert quantity_of("W/m2") == "irradiance"
    assert target_unit("solar_irradiance_w_m2", "W/m2") == "W/m2"
    value, rec = convert_value("Irradiance (W)", 847.3, "solar_irradiance_w_m2")
    assert value == 847.3, "a power-labelled tag must not be scaled onto an irradiance field"
    assert rec["converted"] is False
    assert rec["flag"] == "unit_quantity_mismatch"


def test_irradiance_converts_within_its_own_quantity():
    value, rec = convert_value("Irradiance (kW/m2)", 0.8473, "solar_irradiance_w_m2")
    assert value == 847.3
    assert rec["conversion"] == "kw_m2_to_w_m2"


def test_weather_station_irradiance_passes_through_unscaled():
    norm, _, _, _, _, _, _ = normalize_row({"irradiance_w_m2": 847.3}, oem="generic_iot")
    assert norm["solar_irradiance_w_m2"] == 847.3


# ── non-finite input must never reach the response encoder ──────────────────
# Found by the 2026-08-23 break test against the live sandbox: `1e309` parses to
# inf, travelled through normalization untouched (an unresolved tag keeps its
# raw value by design), and then broke starlette's JSON encoder —
# `ValueError: Out of range float values are not JSON compliant: inf`, i.e. an
# unauthenticated HTTP 500 from a 38-byte body. The CSV path had the same hole
# via _coerce turning the cell "inf" into a float.

@pytest.mark.parametrize("literal", ["1e309", "-1e309", "Infinity", "-Infinity", "NaN"])
def test_non_finite_json_is_rejected_not_a_500(client, literal):
    resp = client.post("/v1/normalize",
                       content='{"oem":"haas","data":{"S1Temp":%s}}' % literal,
                       headers={"content-type": "application/json"})
    assert resp.status_code == 422, resp.text[:200]
    assert "finite" in resp.text


def test_non_finite_inside_a_list_is_rejected(client):
    resp = client.post("/v1/normalize",
                       content='{"oem":"haas","data":{"t":[1.0, 1e309]}}',
                       headers={"content-type": "application/json"})
    assert resp.status_code == 422


@pytest.mark.parametrize("cell", ["inf", "-inf", "nan", "Infinity", "NaN"])
def test_non_finite_csv_cells_null_out_rather_than_crashing(client, cell):
    resp = client.post("/v1/normalize",
                       content=f"S1Temp,SP_SPEED\n{cell},8500",
                       headers={"content-type": "text/csv", "x-oem": "haas"})
    assert resp.status_code == 200, resp.text[:200]
    row = resp.json()["normalized"][0]
    assert row.get("spindle_temperature") is None, row


# ── recognized-but-unconvertible units ───────────────────────────────────────

def test_kilovolts_are_converted_not_passed_through(client):
    """A unit we can NAME but not CONVERT is worse than one we never knew.

    `kV` and `mA` were already recognized as voltage and current, so the tag
    resolved cleanly and the reading was kept in its original unit -- 0.2771 kV
    landing in a volts field as 0.2771, off by 1000x behind a clean 100%
    coverage number. The fail-closed guard is a deliberately narrow list of flow
    units and never covered these.
    """
    out = client.post("/v1/normalize", json={
        "oem": "sunspec_meter", "data": {"PhVphA (kV)": 0.2771}}).json()
    assert out["normalized"]["ac_voltage_phase_a"] == pytest.approx(277.1)


def test_milliamps_are_converted(client):
    out = client.post("/v1/normalize", json={
        "oem": "sunspec_meter", "data": {"A (mA)": 182400}}).json()
    assert out["normalized"]["line_current_a"] == pytest.approx(182.4)


def test_hz_to_rpm_is_refused_rather_than_guessed():
    """Not a unit conversion: the factor depends on the machine's pole count.

    60 Hz is 3600 rpm on a 2-pole machine and 1800 on a 4-pole, and Pint reads
    the pair as 9.549 by treating Hz as rad/s. Three defensible answers means
    this must never be applied silently.
    """
    from app.unit_converter import CONVERSIONS
    assert ("Hz", "rpm") not in CONVERSIONS
    assert ("rpm", "Hz") not in CONVERSIONS


def test_no_recognized_unit_lacks_a_converter_to_its_target():
    """The gap class itself, not just the four instances found."""
    from app.unit_converter import CONVERSIONS, QUANTITY, TARGET_UNIT

    gaps = []
    for unit, quantity in QUANTITY.items():
        target = TARGET_UNIT.get(quantity)
        if not target or unit == target:
            continue
        if (unit, target) not in CONVERSIONS:
            gaps.append(f"{unit} -> {target}")
    # Hz->rpm is the one deliberate omission and is asserted above.
    assert [g for g in gaps if g != "Hz -> rpm"] == []


# ── phase designators are not units ──────────────────────────────────────────

def test_a_trailing_phase_letter_is_not_read_as_a_unit():
    """`ac_voltage_phase_a` holds volts, not amperes.

    The `_a` and `_c` on the phase fields are phase designators. Reading them
    as a unit made a voltage field claim to hold current and a current field
    claim to hold degrees Celsius, so every conversion into them was refused as
    a cross-quantity mismatch and the value passed through in whatever unit it
    arrived in.
    """
    from app.unit_converter import target_unit
    assert target_unit("ac_voltage_phase_a", "V") == "V"
    assert target_unit("ac_voltage_phase_c", "V") == "V"
    assert target_unit("ac_current_phase_c", "A") == "A"
    assert target_unit("ac_current_phase_a", "A") == "A"


def test_a_name_declared_unit_still_wins_when_the_registry_agrees_in_dimension():
    """The `..._psi` rule the name precedence exists for must not regress."""
    from app.unit_converter import target_unit
    assert target_unit("feed_rate_mm_min", "in/min") == "mm/min"
    assert target_unit("print_speed_mm_s", "in/s") == "mm/s"
