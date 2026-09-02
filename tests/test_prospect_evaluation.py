"""The first thing an engineer at each target company does after `docker pull`.

WHY THIS FILE EXISTS
--------------------
Every defect that reached an image this month was invisible to the unit suite
and obvious to anyone who sent a real payload: tcp_speed multiplied by 60,
`battery_pct` resolving to nothing, a refusal note that printed `None`. The
unit tests all passed. Nobody had run the sandbox as a prospect.

Each test below is one company's evaluation, with the tags that company
actually ships. A failure names the prospect who would have seen the bug.

The three shared assertions are the point. They are not decoration:

    no_wrong_domain_mappings   nothing resolved into another industry's field
    no_invariant_violations    the relief valve stayed shut
    no_unit_corruption         every conversion was dimensionally legal

Run standalone, or via ./pre-push-gate.sh, which refuses to push without it.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app import field_registry, unit_converter
from app.main import app


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


def normalize(client, payload):
    resp = client.post("/v1/normalize", json=payload)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:400]}"
    return resp.json()


# ── shared safety assertions ────────────────────────────────────────────────
# Vertical -> canonical-field prefixes/substrings that vertical must never
# produce. Written as the failure we actually shipped or nearly shipped: a
# robot motor temperature landing in `spindle_temperature`, a conveyor belt
# speed landing in `spindle_speed_rpm`.
_FORBIDDEN = {
    "robotics": ("spindle_", "cnc.", "hotend", "filament", "heated_bed",
                 "inverter_", "pv_", "dc_bus"),
    "amr": ("spindle_", "cnc.", "hotend", "filament", "heated_bed",
            "inverter_", "pv_"),
    "logistics": ("spindle_", "cnc.", "hotend", "filament", "robot.joint."),
    "cnc": ("hotend", "filament", "heated_bed", "amr.", "inverter_", "pv_"),
    "energy": ("spindle_", "hotend", "filament", "robot.joint.", "amr."),
    "additive": ("spindle_", "amr.", "robot.joint.", "inverter_"),
}


def no_wrong_domain_mappings(result):
    """No tag resolved into a field belonging to another industry.

    Also refuses any resolution to a name outside the canonical registry: an
    invented field is a lie regardless of which domain it belongs to.
    """
    vertical = result.get("vertical")
    forbidden = _FORBIDDEN.get(vertical, ())
    bad = []
    for tag, info in (result.get("field_mappings") or {}).items():
        canon = info.get("canonical_field")
        if not canon:
            continue
        if not field_registry.is_canonical(canon):
            bad.append(f"{tag} -> {canon} (not in the canonical registry)")
            continue
        for needle in forbidden:
            if canon.startswith(needle) or needle in canon:
                bad.append(f"{tag} -> {canon} (a {vertical} reading in a "
                           f"{needle!r} field)")
    assert not bad, "cross-domain resolution:\n  " + "\n  ".join(bad)
    return True


def no_invariant_violations(result):
    """The relief valve must show zero violations."""
    n = result.get("_invariant_violations", 0)
    assert n == 0, f"{n} output-invariant violations"
    return True


def no_unit_corruption(result):
    """Every recorded conversion was between units of one physical quantity.

    This is the generalised form of the tcp_speed defect: the conversion there
    was dimensionally legal (mm/s -> mm/min) but landed in a unit no robot
    vendor reports. The dimensional half is checked here; the convention half
    is checked once, globally, by
    test_no_field_declares_a_unit_its_sources_never_send.
    """
    bad = []
    for conv in (result.get("unit_conversions") or []):
        src, dst = conv.get("from"), conv.get("to")
        qs = unit_converter.QUANTITY.get(src)
        qd = unit_converter.QUANTITY.get(dst)
        if qs and qd:
            ds = unit_converter.DIMENSION.get(qs, qs)
            dd = unit_converter.DIMENSION.get(qd, qd)
            if ds != dd:
                bad.append(f"{conv.get('raw_field')}: {src} -> {dst} "
                           f"({ds} into {dd})")
        val = conv.get("converted_value")
        if isinstance(val, float) and (val != val or val in (float("inf"),
                                                            float("-inf"))):
            bad.append(f"{conv.get('raw_field')}: non-finite {val}")
    assert not bad, "unit corruption:\n  " + "\n  ".join(bad)
    return True


def clean(result):
    no_wrong_domain_mappings(result)
    no_invariant_violations(result)
    no_unit_corruption(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# TIER 1 — companies with active conversations. A failure here kills a live
# deal, because these engineers can pull the image today.
# ═══════════════════════════════════════════════════════════════════════════

def test_ignition_evaluator(client):
    """Ignition partner team, Sparkplug B style CNC tags."""
    r = normalize(client, {"oem": "haas", "data": {
        "S1Temp": 79.2, "SP_SPEED": 5204, "S1Load": 67, "COOL_TEMP": 94.6,
    }})
    clean(r)
    assert r["coverage_pct"] >= 75
    assert r["normalized"]["spindle_speed_rpm"] == 5204
    assert r["normalized"]["spindle_load_pct"] == 67


def test_svt_robotics_evaluator(client):
    """SVT Robotics orchestration layer, Locus AMR telemetry.

    `battery_pct` is Locus's real production tag and MUST resolve. It went
    unresolved in the 2026-09-02 robotics sweep while `battery_soc` resolved
    fine, purely because the classifier keyed on the literal token "soc".
    """
    r = normalize(client, {"oem": "locus", "data": {
        "battery_pct": 68,
        "battery_voltage_v": 48.2,
        "drive_motor_temp_c": 38.4,
        "lift_motor_temp_c": 41.2,
        "picks_completed": 847,
        "current_speed_mps": 1.2,
        "payload_kg": 8.6,
    }})
    clean(r)
    assert r["oem_recognized"] is True
    assert r["normalized"]["battery_soc_pct"] == 68
    assert r["normalized"]["payload_kg"] == 8.6
    # The two motors must stay distinguishable. Collapsing them into
    # motor_temperature / motor_temperature_2 loses which motor is hot.
    assert r["normalized"]["motors.drive.temperature"] == 38.4
    assert r["normalized"]["motors.lift.temperature"] == 41.2
    assert r["coverage_pct"] >= 85


def test_svt_amr_vendor_spellings_all_resolve(client):
    """Locus, Fetch and 6 River spell state-of-charge four different ways.

    One pack serves all of SVT's AMR partners, so every spelling has to land
    on the same canonical or the fleet view fragments by vendor.
    """
    for oem in ("locus", "fetch", "6river", "otto", "vecna"):
        for tag in ("battery_pct", "BatteryLevel", "battery_soc",
                    "battery_percentage"):
            r = normalize(client, {"oem": oem, "data": {tag: 68}})
            assert r["normalized"].get("battery_soc_pct") == 68, \
                f"{oem}/{tag} did not resolve to battery_soc_pct"
            clean(r)


def test_svt_kuka_evaluator(client):
    """SVT also orchestrates KUKA arms. Guards the tcp_speed P0."""
    r = normalize(client, {"oem": "kuka", "data": {
        "TCP_Speed_mm_s": 250.0,
        "Axis1_Torque": 45.2,
        "RunHours": 14203,
        "DriveTemp_C": 52.3,
    }})
    clean(r)
    tcp = r["normalized"].get("sensor_readings.tcp_speed")
    assert tcp == 250.0, (
        f"TCP_Speed corrupted: {tcp}. A robot tool speed is reported in mm/s; "
        f"250 mm/s became 15000 when the registry declared mm/min.")
    assert r["normalized"]["robot.joint.0.torque"] == 45.2
    assert r["normalized"]["operating_hours"] == 14203


def test_svt_kuka_cycle_time_stays_in_seconds(client):
    """A 32.4 s robot cycle must not be reported as 0.009."""
    r = normalize(client, {"oem": "kuka", "data": {"CycleTime_s": 32.4}})
    clean(r)
    assert r["normalized"]["sensor_readings.cycle_time"] == 32.4


def test_svt_universal_robots_evaluator(client):
    """SVT orchestrates UR cobots. RTDE names, published and version-stable."""
    r = normalize(client, {"oem": "universal_robots", "data": {
        "actual_q_0": 1.57,
        "actual_current_0": 1.24,
        "joint_temperatures_0": 32.4,
        "robot_mode": 7,
        "payload_mass": 3.2,
    }})
    clean(r)
    assert r["coverage_pct"] >= 60
    # RTDE ships radians; the canonical is degrees, and the conversion has to
    # be recorded rather than silently reinterpreted.
    assert r["normalized"]["robot.joint.0.position"] == pytest.approx(89.95,
                                                                     abs=0.01)
    assert any(c["raw_field"] == "actual_q_0" and c["from"] == "rad"
               for c in r["unit_conversions"])
    assert r["normalized"]["robot.joint.0.current"] == 1.24
    assert r["normalized"]["robot.joint.0.temperature"] == 32.4
    assert r["normalized"]["payload_kg"] == 3.2


def test_svt_six_axis_arm_keeps_every_joint_distinct(client):
    """Six axes must produce six fields, not one field and five collisions."""
    data = {f"Axis{i}_Torque": float(i) for i in range(1, 7)}
    r = normalize(client, {"oem": "kuka", "data": data})
    clean(r)
    for i in range(6):
        assert r["normalized"][f"robot.joint.{i}.torque"] == float(i + 1)


def test_svt_amr_keeps_wheel_position(client):
    """A hot motor is only actionable if you know which wheel it is on."""
    r = normalize(client, {"oem": "locus", "data": {
        "MotorTemp_FL": 42.3, "MotorTemp_FR": 43.1,
        "MotorTemp_RL": 41.0, "MotorTemp_RR": 44.7,
    }})
    clean(r)
    assert r["normalized"]["motors.fl.temperature"] == 42.3
    assert r["normalized"]["motors.fr.temperature"] == 43.1
    assert r["normalized"]["motors.rl.temperature"] == 41.0
    assert r["normalized"]["motors.rr.temperature"] == 44.7
    assert r.get("collisions") in (None, {}), "wheel identity was collapsed"


def test_litmus_evaluator(client):
    """Litmus Edge, German-language Siemens tags."""
    r = normalize(client, {"oem": "siemens", "data": {
        "SPINDEL_AUSLASTUNG": 59.3,
        "STUECKZAHL": 4200,
        "energieverbrauch(Wh)": 4500000,
    }})
    clean(r)
    assert r["normalized"]["spindle_load_pct"] == 59.3
    assert r["normalized"]["energy_kwh"] == 4500.0
    assert r["coverage_pct"] == 100


def test_litmus_mixed_protocol(client):
    """Litmus connects 250 protocols; a motor payload with an F temperature."""
    r = normalize(client, {"oem": "rockwell", "data": {
        "Motor_Speed_RPM": 1750,
        "Motor_Current_A": 28.4,
        "Motor_Temp_F": 142.7,
        "VFD_Frequency_Hz": 58.5,
    }})
    clean(r)
    temp = r["normalized"].get("motor_temperature")
    assert temp is not None, "Motor_Temp_F did not resolve"
    assert 50 < temp < 70, f"142.7F should be ~61.5C, got {temp}"


def test_highbyte_uns_evaluator(client):
    """HighByte UNS-style payload."""
    r = normalize(client, {"oem": "haas", "data": {
        "S1Temp": 72.1, "SP_SPEED": 8500, "S1Load": 45,
    }})
    clean(r)
    assert r["coverage_pct"] >= 75


def test_machinemetrics_evaluator(client):
    """Two vendors, one canonical. This is the entire pitch.

    If Haas and FANUC do not land on the same field names, the demo fails.
    """
    haas = normalize(client, {"oem": "haas",
                              "data": {"S1Load": 67, "SP_SPEED": 5204}})
    fanuc = normalize(client, {"oem": "fanuc",
                               "data": {"SPINDLE_LOAD": 67,
                                        "SPINDLE_SPEED_ACT": 5204}})
    clean(haas)
    clean(fanuc)
    for r in (haas, fanuc):
        assert "spindle_load_pct" in r["normalized"]
        assert "spindle_speed_rpm" in r["normalized"]
    assert haas["normalized"]["spindle_speed_rpm"] == \
        fanuc["normalized"]["spindle_speed_rpm"]


def test_tulip_evaluator(client):
    """Tulip connector team, generic manufacturing tags."""
    r = normalize(client, {"oem": "generic", "data": {
        "MotorCurrent_Amps": 28.4,
        "MotorTemp_C": 61.5,
        "PartCount": 4200,
        "CycleTime_sec": 32.4,
    }})
    clean(r)


# ═══════════════════════════════════════════════════════════════════════════
# TIER 2 — emails out. These engineers could pull any day this week.
# ═══════════════════════════════════════════════════════════════════════════

def test_power_factors_evaluator(client):
    """310 GW managed. SunSpec model 103 with scale factors."""
    r = normalize(client, {"oem": "fronius", "sunspec_model": 103, "data": {
        "A": 5.1, "AphA": 1.7, "AphB": 1.7, "AphC": 1.7,
        "PPVphAB": 480.1, "PPVphBC": 479.8, "PPVphCA": 480.3,
        "W": 14389, "Hz": 59.98,
        "PF": 973, "PF_SF": -3,
        "WH": 91240000, "WH_SF": 0,
        "A_SF": -1, "V_SF": -1, "W_SF": 0,
    }})
    clean(r)
    assert r["coverage_pct"] >= 90
    assert r["normalized"]["power_factor"] == pytest.approx(0.973, abs=0.001)


def test_copa_data_evaluator(client):
    """COPA-DATA zenon, German decimal commas."""
    r = normalize(client, {"oem": "siemens", "locale": "de_DE", "data": {
        "SPINDEL_AUSLASTUNG": "59,3",
        "STUECKZAHL": "4200",
    }})
    clean(r)
    assert r["normalized"]["spindle_load_pct"] == 59.3
    assert float(r["normalized"]["part_count"]) == 4200.0


def test_n3uron_evaluator(client):
    """N3uron MQTT / OPC UA tags."""
    r = normalize(client, {"oem": "generic", "data": {
        "Temperature_C": 72.1,
        "Pressure_kPa": 450.2,
        "Flow_Rate_LPM": 28.4,
    }})
    clean(r)


def test_stem_also_energy_evaluator(client):
    """53 GW solar, SolarEdge register names."""
    r = normalize(client, {"oem": "solaredge", "data": {
        "I_AC_Power": 48700,
        "I_AC_Energy_WH": 91240000,
        "I_AC_Frequency": 59.98,
        "I_DC_Voltage": 780,
    }})
    clean(r)
    power = (r["normalized"].get("inverter_output_kw")
             or r["normalized"].get("active_power_kw")
             or r["normalized"].get("ac_power_kw"))
    assert power is not None, f"no AC power field: {sorted(r['normalized'])}"
    assert power == pytest.approx(48.7, abs=0.1)


def test_uptake_bosch_evaluator(client):
    """Industrial AI, generic condition-monitoring tags."""
    r = normalize(client, {"oem": "generic", "data": {
        "vibration_mm_s": 4.2,
        "bearing_temp_c": 78.4,
        "motor_current_a": 28.4,
        "operating_hours": 14203,
    }})
    clean(r)
    assert r["coverage_pct"] >= 50


def test_geotab_fleet_evaluator(client):
    """4.3M connected vehicles, CAN bus data."""
    r = normalize(client, {"oem": "generic", "data": {
        "EngineRPM": 2400,
        "EngineTemp_C": 92,
        "FuelLevel_Pct": 68,
        "Odometer_km": 142800,
        "BatteryVoltage": 13.8,
    }})
    clean(r)


def test_fiix_rockwell_evaluator(client):
    """CMMS platform, maintenance-focused tags."""
    r = normalize(client, {"oem": "rockwell", "data": {
        "Motor_Runtime_Hours": 8420,
        "Motor_Starts": 12400,
        "Bearing_Temp_F": 165.2,
        "Vibration_in_s": 0.15,
    }})
    clean(r)
    temp = r["normalized"].get("bearing_temperature")
    if temp is not None:
        assert 60 < temp < 80, f"165.2F should be ~73.9C, got {temp}"


# ═══════════════════════════════════════════════════════════════════════════
# TIER 3 — queued targets.
# ═══════════════════════════════════════════════════════════════════════════

def test_equipmentshare_evaluator(client):
    """Mixed construction fleet."""
    r = normalize(client, {"oem": "generic", "data": {
        "EngineHours": 4280,
        "FuelRate_GPH": 8.4,
        "HydraulicTemp_F": 185,
        "HydraulicPressure_PSI": 3200,
        "BoomAngle_deg": 45.2,
    }})
    clean(r)
    temp = r["normalized"].get("hydraulic_temperature")
    if temp is not None:
        assert 80 < temp < 90, f"185F should be ~85C, got {temp}"


# ═══════════════════════════════════════════════════════════════════════════
# WHOLE-IMAGE INVARIANTS — not tied to one prospect.
# ═══════════════════════════════════════════════════════════════════════════

# Fields whose declared unit is deliberately one no source sends, because the
# declared unit is the reporting convention or the SI target. Anything NOT on
# this list that declares a unit its own sources never send is the tcp_speed
# defect wearing a different field name.
_UNIT_CONVENTION_ALLOWLIST = {
    "cnc.path.feedrate",                  # feed rate is conventionally mm/min
    "metadata.pump_leakage",              # L/min is the datasheet convention
    "vehicle.ambient.barometric_pressure",
    "vehicle.engine.fuel_rail_pressure",
    "vehicle.engine.oil_pressure",
    "vehicle.tire_pressure_fl", "vehicle.tire_pressure_fr",
    "vehicle.tire_pressure_rl", "vehicle.tire_pressure_rr",
    "workholding_pressure",               # bar is the SI pressure target
}


def test_no_field_declares_a_unit_its_sources_never_send():
    """Generalises the tcp_speed defect.

    tcp_speed declared mm/min while every observed source sent mm/s, in/s or
    m/s -- dimensionally legal, so no converter test caught it, and a 250 mm/s
    robot read 15000. The signature is: a declared unit of the right dimension
    that no source in the corpus actually uses. Two more fields carried it --
    cycle_time in hours and altitude in millimetres.
    """
    fields = field_registry.fields()
    offenders = []
    for name, spec in sorted(fields.items()):
        if name in _UNIT_CONVENTION_ALLOWLIST:
            continue
        unit = spec.get("unit")
        observed = spec.get("observed_input_units") or []
        if not unit or not observed or unit in observed:
            continue
        q = unit_converter.QUANTITY.get(unit)
        if q and all(unit_converter.QUANTITY.get(o) == q for o in observed):
            offenders.append(f"{name}: declares {unit}, sources send {observed}")
    assert not offenders, (
        "field declares a unit no source sends:\n  " + "\n  ".join(offenders))


def test_every_pack_mapping_targets_a_real_canonical():
    """A mapping to a name outside the registry silently resolves to nothing."""
    pack_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "packs")
    bad = []
    for fn in sorted(os.listdir(pack_dir)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        pack = json.load(open(os.path.join(pack_dir, fn)))
        for tag, canon in pack["mappings"].items():
            if not field_registry.is_canonical(canon):
                bad.append(f"{fn}: {tag} -> {canon}")
    assert not bad, "pack maps to unknown canonicals:\n  " + "\n  ".join(bad)


def test_every_robotics_vendor_svt_names_is_recognized(client):
    """SVT's confirmed partner list. An unrecognized OEM refuses cross-pack
    matches and reports oem_recognized: false, which reads as "we don't
    support your robot"."""
    vendors = [
        "locus", "6river", "fetch", "zebra", "mir", "omron_amr", "otto",
        "kuka", "universal_robots", "hai_robotics", "addverb", "vecna",
        "modula", "autostore", "abb_robot", "fanuc", "yaskawa",
    ]
    from app import corpus
    unplaced = [v for v in vendors if corpus.domain_from_oem(v) is None]
    assert not unplaced, f"no domain for: {unplaced}"


def test_every_recognized_unit_token_is_convertible():
    """A unit the tokenizer recognises but the converter cannot convert is
    worse than one it never heard of.

    An unrecognised unit leaves the tag unresolved. A RECOGNISED but
    unconvertible one still resolves, still lands in the canonical field, and
    keeps the source unit's magnitude -- silently. `cfm` normalised to
    ft3/min, ft3/min was absent from QUANTITY, and a 450 CFM VAV box was
    stored as 450 in an L/min field: 28x low, no conversion recorded, nothing
    flagged. Same shape as the tcp_speed defect with none of the evidence.
    """
    from app import corpus
    unconvertible = []
    for token in sorted(set(corpus._UNITS.split("|"))):
        token = token.strip()
        if not token or "|" in token:
            continue
        norm = corpus._normalize_unit_token(token)
        if norm and norm not in unit_converter.QUANTITY:
            unconvertible.append(f"{token} -> {norm}")
    assert not unconvertible, (
        "tag tokenizer recognises units the converter cannot convert:\n  "
        + "\n  ".join(unconvertible))


def test_cfm_air_flow_converts(client):
    """HVAC/BMS bread and butter: a VAV box reports CFM, the field is L/min."""
    r = normalize(client, {"oem": "carrier", "data": {"VAV_Flow_CFM": 450}})
    clean(r)
    assert r["normalized"]["flow_rate_lpm"] == pytest.approx(12742.58, abs=0.1)


def test_a_canonical_name_resolves_in_any_case(client):
    """`Operating_Hours` is the same field as `operating_hours`.

    Layer 2 was a literal `in` test, so the lowercase form matched at
    confidence 1.0 and the capitalised form -- much the commoner spelling on a
    real historian -- fell through to UNRESOLVED. It could not be rescued
    later either: the unit stripper reads the trailing "Hours" as a time unit
    and folds the tag to "operating".
    """
    for spelling in ("operating_hours", "Operating_Hours", "OPERATING_HOURS",
                     "Operating Hours", "operating-hours"):
        r = normalize(client, {"oem": "generic", "data": {spelling: 14203}})
        assert r["normalized"].get("operating_hours") == 14203, \
            f"{spelling!r} did not resolve to operating_hours"


def test_ambiguous_bare_tags_are_not_answered_confidently(client):
    """Test F. Maximally ambiguous tags must not resolve at high confidence.

    `Temperature`, `Pressure`, `Speed`, `Level` could belong to any vertical.
    A confident answer here means the evidence gate has gone loose.
    """
    r = normalize(client, {"oem": "generic", "data": {
        "Temperature": 72, "Pressure": 450, "Current": 28.4, "Speed": 1200,
        "Load": 78, "Power": 412, "Voltage": 480, "Frequency": 60,
        "Level": 68, "Flow": 28.4, "Position": 45.2, "Status": "RUNNING",
        "Mode": "AUTO", "Count": 4200, "Hours": 14203,
    }})
    clean(r)
    overconfident = [
        (t, m["canonical_field"], m["confidence"])
        for t, m in r["field_mappings"].items()
        if m.get("canonical_field") and (m.get("confidence") or 0) > 0.7
    ]
    assert not overconfident, (
        f"ambiguous bare tags answered confidently: {overconfident}")


# ═══════════════════════════════════════════════════════════════════════════
# FLEET / HEAVY VEHICLE — J1939 pack
# ═══════════════════════════════════════════════════════════════════════════

def test_geotab_motive_j1939_evaluator(client):
    """A telematics platform's first payload. Every tag is a documented SPN."""
    r = normalize(client, {"oem": "j1939", "data": {
        "EngineSpeed": 2400, "EngineLoad_Pct": 78,
        "EngineCoolantTemp_F": 195, "FuelRate_GPH": 8.4, "FuelLevel_Pct": 68,
        "Odometer_km": 142800, "TransmissionTemp_F": 180,
        "OilPressure_PSI": 42, "DEF_Level_Pct": 72, "DPF_Soot_Load_Pct": 34,
        "TurboBoost_PSI": 28.4, "ExhaustTemp_F": 842, "BatteryVoltage": 13.8,
        "VehicleSpeed_MPH": 62, "TotalFuelUsed_gal": 48200,
        "TotalIdleHours": 2400, "EngineHours": 14203, "AmbientTemp_F": 85,
        "Barometric_kPa": 101.3,
    }})
    clean(r)
    assert r["coverage_pct"] >= 85
    n = r["normalized"]
    # Idle hours and engine hours are DIFFERENT meters. They shared
    # operating_hours before this pack, idle won, and a 14,203-hour truck
    # reported 2,400 -- the number that drives every maintenance interval.
    assert n["engine_hours"] == 14203
    assert n["idle_hours"] == 2400
    # Conversions, each against the arithmetic rather than a golden file.
    assert n["vehicle_speed_km_h"] == pytest.approx(62 * 1.609344, abs=0.01)
    assert n["fuel_rate_lph"] == pytest.approx(8.4 * 3.785411784, abs=0.01)
    assert n["total_fuel_used_l"] == pytest.approx(48200 * 3.785411784, abs=1)
    assert n["engine_oil_pressure_kpa"] == pytest.approx(42 * 6.894757, abs=0.1)
    assert n["exhaust_temp_c"] == pytest.approx(450.0, abs=0.1)
    # A 142,800 km odometer is an ordinary truck, not a physics violation.
    assert n["odometer_km"] == 142800
    assert not r.get("null_states"), r.get("null_states")


def test_equipmentshare_mixed_fleet_evaluator(client):
    """T3 prefixes every tag with the machine's make. Six vendors, one
    canonical -- the same claim the CNC demo makes, in a second vertical."""
    r = normalize(client, {"oem": "equipmentshare", "data": {
        "CAT_EngineRPM": 1800, "CAT_CoolantTemp": 195, "CAT_FuelRate": 6.2,
        "Deere_EngHrs": 8420, "Deere_HydTemp": 185, "Deere_HydPSI": 3200,
        "JLG_BoomAngle": 45, "JLG_PlatformLoad": 680,
        "Genie_LiftHeight": 60, "Genie_BatterySOC": 72,
        "Doosan_EngineLoad": 78, "Doosan_FuelLevel": 45,
    }})
    clean(r)
    assert r["coverage_pct"] >= 85
    n = r["normalized"]
    assert n["engine_speed_rpm"] == 1800
    assert n["engine_hours"] == 8420
    # 3200 psi is normal construction hydraulics, not an out-of-range reading.
    assert n["hydraulic_pressure_kpa"] == pytest.approx(3200 * 6.894757, abs=1)
    assert n["platform_load_kg"] == pytest.approx(680 * 0.45359237, abs=0.1)
    assert n["lift_height_m"] == pytest.approx(60 * 0.3048, abs=0.001)
    assert not r.get("null_states"), r.get("null_states")


def test_aemp_construction_evaluator(client):
    """Trackunit / AEMP 2.0 off-highway payload."""
    r = normalize(client, {"oem": "aemp", "data": {
        "Machine_Hours": 4280, "Fuel_Used_gal": 12400, "Fuel_Level_Pct": 45,
        "Idle_Hours": 1200, "Location_Lat": 36.1699, "Location_Lon": -115.1398,
        "DEF_Level_Pct": 68, "HydraulicTemp_F": 185,
        "HydraulicPressure_PSI": 3200, "Engine_Coolant_Temp_F": 195,
        "Engine_Oil_Pressure_PSI": 42, "Ground_Speed_MPH": 12,
        "BoomAngle_deg": 45.2, "BucketAngle_deg": 22.8, "SwingAngle_deg": 90,
        "TrackTension_PSI": 450,
    }})
    clean(r)
    assert r["coverage_pct"] >= 85
    n = r["normalized"]
    assert n["engine_hours"] == 4280
    assert n["idle_hours"] == 1200
    assert n["gps_latitude"] == pytest.approx(36.1699)
    assert n["gps_longitude"] == pytest.approx(-115.1398)
    assert not r.get("null_states"), r.get("null_states")


def test_doosan_still_means_machine_tools(client):
    """`doosan` is a CNC builder in the OEM domain table and must stay one.

    The J1939 pack answers for construction OEMs, but claiming the bare name
    would route a lathe's tags into a truck pack. The construction arm is
    reachable as `doosan_ce`.
    """
    from app import corpus
    assert corpus.domain_from_oem("doosan") == "cnc"
    assert corpus.domain_from_oem("doosan_ce") == "automotive"


def test_pack_index_matches_the_packs_on_disk(client):
    """`_index.json` is the manifest a prospect reads. It must not drift.

    It listed 16 of 26 packs -- every energy pack was missing -- and six of
    the entries it did carry under-reported their mapping counts (fanuc 759
    vs 780, haas 240 vs 258, schneider 41 vs 60). A prospect reading the
    manifest saw a smaller product than the one they had running, and the
    number quoted in an email would have been wrong in either direction
    depending on which artifact was consulted.
    """
    pack_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "packs")
    index = json.load(open(os.path.join(pack_dir, "_index.json")))["packs"]
    on_disk = {}
    for fn in sorted(os.listdir(pack_dir)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        pack = json.load(open(os.path.join(pack_dir, fn)))
        on_disk[fn[:-5]] = len(pack["mappings"])

    assert set(index) == set(on_disk), (
        f"index/disk mismatch: only on disk {sorted(set(on_disk) - set(index))}, "
        f"only in index {sorted(set(index) - set(on_disk))}")
    drift = {k: (index[k]["mapping_count"], v)
             for k, v in on_disk.items() if index[k]["mapping_count"] != v}
    assert not drift, f"mapping_count drift (index, disk): {drift}"

    # ...and the health endpoint, which is what anyone actually quotes.
    health = client.get("/health").json()
    assert set(health["packs"]) == set(on_disk)
    assert sum(health["packs"].values()) == sum(on_disk.values())
    assert health["canonical_fields"] == len(field_registry.fields())
