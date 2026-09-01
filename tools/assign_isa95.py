#!/usr/bin/env python3
"""Assign an `isa95_category` to every canonical field. METADATA ONLY.

Categories follow ISA-95 Part 2 (equipment / material / personnel models) and
Part 4 (production operations), collapsed to the ten buckets a UNS or CDM
consumer actually filters on.

This is a SCRIPT, not a one-off hand edit, for two reasons:

  * There are two canonical-field files that disagree on their field list
    (the 464-field registry and the 467-field serving dictionary). Classifying
    by hand guarantees they drift apart. Running one classifier over the union
    guarantees they cannot.

  * The field list changes. When a pack adds a field, re-running this is a
    second, not an archaeology exercise in which category a sibling got.

Resolution order is strict and the FIRST match wins:

    1. SEEDS      -- every field the specification named explicitly, plus the
                     two stated tie-breaks (motor_temperature is condition,
                     power_factor is energy). These can never be overridden.
    2. PATTERNS   -- ordered regex rules. Order is the whole design here:
                     `spindle_temperature` must reach the temperature rule
                     before the `spindle_` rule, `battery_voltage_v` must
                     reach the storage rule before the voltage rule, and
                     `ambient_temperature_c` must reach environmental before
                     either.
    3. QUANTITY   -- the field's declared physical_quantity, when it settles it.
    4. "general"  -- and the field is printed for review. Never silently.

Usage:
    python3 tools/assign_isa95.py            # classify + report, writes nothing
    python3 tools/assign_isa95.py --write    # write both files in place
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

APP = pathlib.Path(__file__).resolve().parent.parent / "app"
TARGETS = (APP / "canonical_fields.json", APP / "packs" / "_canonical_fields.json")

CATEGORIES = (
    "equipment_performance", "equipment_state", "production_performance",
    "energy_consumption", "electrical_measurement", "equipment_condition",
    "environmental", "storage", "safety", "production_quality", "general",
)

# ── 1. seeds: every field the specification named, verbatim ────────────────
SEEDS: dict[str, str] = {}


def _seed(category: str, *names: str) -> None:
    for n in names:
        SEEDS[n] = category


_seed("equipment_performance",
      "spindle_speed_rpm", "spindle_load_pct", "feed_rate_mm_min",
      "servo_load_pct", "axis_position", "motor_current_a", "motor_speed_rpm",
      "tool_number", "program_number")
_seed("equipment_state",
      "execution_state", "operating_hours", "operating_mode", "alarm_code",
      "error_count", "availability", "power_on_time", "power_on_hours",
      "cutting_time_hours")
_seed("production_performance",
      "part_count", "parts_count", "cycle_count", "cycle_time_s",
      "reject_count", "good_count", "oee_pct", "throughput")
_seed("energy_consumption",
      "energy_kwh", "active_power_kw", "power_factor", "reactive_power_kvar",
      "apparent_power_va", "power_consumption_kw", "energy_delivered_kwh")
_seed("electrical_measurement",
      "ac_current_phase_a", "ac_current_phase_b", "ac_current_phase_c",
      "ac_voltage_ll_ab", "ac_voltage_ll_bc", "ac_voltage_ll_ca",
      "ac_voltage_phase_a", "ac_voltage_phase_b", "ac_voltage_phase_c",
      "dc_current_a", "dc_voltage_v", "line_current_a", "grid_frequency_hz")
_seed("equipment_condition",
      "spindle_temperature", "coolant_temp", "oil_temperature",
      "bearing_temperature_c", "vibration_mm_s", "inverter_heatsink_temp_c",
      # Stated tie-break: health, not output.
      "motor_temperature")
_seed("environmental",
      "ambient_temperature_c", "humidity_pct", "irradiance_w_m2",
      "wind_speed_m_s")
_seed("storage",
      "battery_soc_pct", "battery_soh_pct", "battery_current_a",
      "battery_voltage_v", "battery_capacity_kwh", "charge_state")
_seed("safety",
      "estop_state", "door_interlock", "guard_status", "alarm_active",
      "fault_code")
_seed("production_quality",
      "surface_finish", "tolerance_deviation", "dimensional_accuracy")

# ── 2. patterns, most specific first. ORDER IS THE DESIGN. ─────────────────
PATTERNS: list[tuple[str, str]] = [
    # Safety reads on the name, and must outrank every process rule: an
    # interlock is a safety field even when it is also a door position.
    (r"(^|_)(e_?stop|emergency_stop)", "safety"),
    (r"(^|_)(interlock|light_curtain|guard(_|$)|safety|door_open|door_closed)", "safety"),
    (r"(^|_)fault(_|$)|(^|_)fault_(code|active|state)", "safety"),

    # Environmental before condition: ambient temperature is weather, not wear.
    (r"(^|_)(ambient|outdoor|outside|weather|atmospheric)", "environmental"),
    (r"(^|_)(irradiance|insolation|wind_speed|wind_direction|dew_point|"
     r"precipitation|air_quality|barometric)", "environmental"),
    (r"(^|_)humidity", "environmental"),
    (r"(^|_)(co2|ppm|o2|voc)(_|$)", "environmental"),

    # Condition before every subsystem rule: a temperature is a health signal
    # regardless of which subsystem it is measured on. This is the generalised
    # form of the stated motor_temperature tie-break.
    (r"(temperature|_temp(_|$)|_temp$|thermal|heatsink)", "equipment_condition"),
    (r"(^|_)(vibration|bearing|wear|lubric|oil_|grease|runout|imbalance|"
     r"degradation|health|soh)", "equipment_condition"),
    (r"(^|_)coolant", "equipment_condition"),
    (r"_condition$|(^|_)(leakage|machine_failure|machine_age|maintenance)", "equipment_condition"),

    # Storage before electrical: battery_voltage_v is a storage field that
    # happens to be measured in volts.
    (r"(^|_)(battery|bess|cell_|soc(_|$)|state_of_charge|charge_state|"
     r"charging|discharg|charge_cycles)", "storage"),

    (r"(^|_)(ac_current|ac_voltage|dc_current|dc_voltage|line_current|"
     r"line_voltage|phase_current|phase_voltage|neutral_current)", "electrical_measurement"),
    (r"(^|_)(frequency_hz|grid_frequency|insulation_resistance|thd|"
     r"harmonic|phase_angle)", "electrical_measurement"),
    (r"_(v|a|hz)$", "electrical_measurement"),
    # A bare voltage is a raw electrical reading whatever subsystem carries it.
    (r"(^|_)(voltage|volt)(_|$)|_voltage$|_volt$", "electrical_measurement"),

    (r"(^|_)power_factor", "energy_consumption"),
    (r"(^|_)(energy|kwh|mwh|wh)(_|$)|_kwh$|_mwh$|_wh$", "energy_consumption"),
    (r"(active_power|reactive_power|apparent_power|power_consumption|"
     r"power_demand|power_output|cooling_power|dc_power|_kw$|_kvar$|_kva$|"
     r"_va$|_mw$|_w$)", "energy_consumption"),
    # Fuel burn is energy consumption; what is LEFT in the tank is state.
    (r"(^|_)(fuel_rate|fuel_consumed|fuel_used|fuel_flow)", "energy_consumption"),

    (r"(^|_)(execution_state|operating_mode|machine_mode|mode)(_|$)", "equipment_state"),
    (r"(^|_)(alarm|warning_code|status|state)(_|$)|_state$|_status$", "equipment_state"),
    (r"(^|_)(operating_hours|runtime|uptime|downtime|power_on|availability|"
     r"enabled|running|idle_time|hours)(_|$)", "equipment_state"),
    (r"_(count|errors)$|(^|_)error_", "equipment_state"),
    (r"(^|_)(event_flags|dtc_code)|event_flags", "equipment_state"),
    (r"(^|_)idle_|_remaining_pct$|_remaining$", "equipment_state"),

    (r"(^|_)(surface_finish|roughness|tolerance|dimensional|accuracy|defect|"
     r"quality|inspection|conformance|adhesion)", "production_quality"),
    (r"(^|_)(part_count|parts|cycle_time|cycle_count|reject|scrap|good_count|"
     r"oee|throughput|yield|produced|production|progress|print_time|"
     r"filament_used)", "production_performance"),

    # Driver/operator events are safety signals, not motion telemetry.
    (r"(^|_)(harsh_\w+|intervention)(_|$)|_event$", "safety"),
    # Telematics location and distance describe operational state.
    (r"(^|_)(gps|latitude|longitude|heading|odometer|trip_distance|geofence)", "equipment_state"),
    # Sensor and link health, not process output.
    (r"(^|_)(wifi_signal|signal_strength|rssi|localization_confidence|"
     r"lidar_point_density|camera_fps|imu_drift)", "equipment_condition"),
    # Utilisation and task completion are production measures.
    (r"(^|_)(payload|task_completion|utilization|efficiency|performance|"
     r"quality_rate)", "production_performance"),

    (r"(^|_)(spindle|feed_rate|feedrate|servo|axis|tool_|program_|override|"
     r"rapid|jog|torque|load_pct|motor_)", "equipment_performance"),
    # Motion, actuation and commanded output: what the machine is DOING.
    (r"(^|_)(axes|axis|velocity|acceleration|gyro|following_error|joint|tcp|"
     r"end_effector|steering|throttle|transmission|boom|bucket)", "equipment_performance"),
    (r"(pwm_output|_output_pct$|load_percent|engine_load|(^|_)(hotend|bed_power|"
     r"vfd|nozzle))", "equipment_performance"),
    (r"(^|_)(speed|position|rpm|pressure|flow|level|setpoint|target)", "equipment_performance"),
]
# Identity and transport metadata. These are not measurements and ISA-95 has
# no category for them; they resolve to "general" DELIBERATELY, and are
# reported separately from fields that merely went unmatched.
IDENTITY = re.compile(
    r"(^|_)(timestamp|serial_number|machine_id|device_id|sequence_number|"
    r"task_id|uuid|dtc_description|register_value|digital_input|digital_output|"
    r"model|vendor|firmware|protocol|max_axes)(_|$)|_id$|_name$|_desc$|_description$")

COMPILED = [(re.compile(p), c) for p, c in PATTERNS]

# ── 3. last resort: the declared physical quantity, when it settles it ─────
QUANTITY_HINTS = {
    "temperature": "equipment_condition",
    "vibration_velocity": "equipment_condition",
    "vibration_acceleration": "equipment_condition",
    "electric_current": "electrical_measurement",
    "voltage": "electrical_measurement",
    "frequency": "electrical_measurement",
    "power": "energy_consumption",
    "energy": "energy_consumption",
    "rotational_speed": "equipment_performance",
    "linear_speed": "equipment_performance",
    "length": "equipment_performance",
    "pressure": "equipment_performance",
    "volumetric_flow": "equipment_performance",
    "mass_flow": "equipment_performance",
    "time": "equipment_state",
    "irradiance": "environmental",
    "relative_humidity": "environmental",
}


def classify(name: str, spec: dict) -> tuple[str, str]:
    """Returns (category, why). `why` makes every assignment auditable."""
    if name in SEEDS:
        return SEEDS[name], "seed"
    # `vehicle.gps_latitude` and `sensor_readings.vibration` are namespaced.
    # Without this the (^|_) boundaries never match past the dot and a third of
    # the schema falls through to "general" for a punctuation reason.
    low = name.lower().replace(".", "_")
    if IDENTITY.search(low):
        return "general", "identity"
    for rx, cat in COMPILED:
        if rx.search(low):
            return cat, f"pattern:{rx.pattern[:40]}"
    q = (spec or {}).get("physical_quantity")
    if q and q in QUANTITY_HINTS:
        return QUANTITY_HINTS[q], f"quantity:{q}"
    return "general", "unmatched"


def load(path: pathlib.Path) -> tuple[dict, dict]:
    doc = json.loads(path.read_text())
    return doc, doc.get("fields", doc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write isa95_category into both files in place")
    args = ap.parse_args()

    docs = {}
    union: dict[str, dict] = {}
    for p in TARGETS:
        if not p.is_file():
            print(f"MISSING: {p}", file=sys.stderr)
            return 2
        doc, fields = load(p)
        docs[p] = (doc, fields)
        for name, spec in fields.items():
            # The serving dictionary carries physical_quantity; the registry
            # may not. Merge so the quantity hint is available either way.
            union.setdefault(name, {}).update(spec if isinstance(spec, dict) else {})

    assigned = {name: classify(name, spec) for name, spec in union.items()}

    dist = collections.Counter(cat for cat, _ in assigned.values())
    by_reason = collections.Counter(why.split(":", 1)[0] for _, why in assigned.values())

    print(f"canonical fields classified: {len(assigned)} "
          f"(union of {len(docs[TARGETS[0]][1])} registry + "
          f"{len(docs[TARGETS[1]][1])} serving)\n")
    print(f"{'category':26} {'fields':>7}")
    for cat in CATEGORIES:
        if dist.get(cat):
            print(f"  {cat:24} {dist[cat]:>7}")
    print(f"\nresolved by: {dict(by_reason)}")

    identity = sorted(n for n, (c, w) in assigned.items()
                      if c == "general" and w == "identity")
    flagged = sorted(n for n, (c, w) in assigned.items()
                     if c == "general" and w != "identity")
    if identity:
        print(f"\n{len(identity)} identity/metadata field(s) -> 'general' by "
              f"design (not measurements; ISA-95 has no category for them):")
        print("    " + ", ".join(identity))
    if flagged:
        print(f"\n*** {len(flagged)} field(s) FLAGGED FOR REVIEW — unmatched:")
        for n in flagged:
            print(f"    {n}")
    else:
        print("\nno field went unmatched.")

    if not args.write:
        print("\n(dry run — pass --write to persist)")
        return 0

    # Each file keeps the exact JSON style it already has. A metadata-only
    # change that reformats 180KB of JSON is unreviewable, and an unreviewable
    # diff is how a "metadata only" edit smuggles in a real one.
    STYLE = {
        "canonical_fields.json": dict(indent=1, sort_keys=False,
                                      ensure_ascii=False, trailing_nl=True),
        "_canonical_fields.json": dict(indent=2, sort_keys=False,
                                       ensure_ascii=True, trailing_nl=False),
    }
    for p, (doc, fields) in docs.items():
        n = 0
        for name, spec in fields.items():
            if isinstance(spec, dict):
                spec["isa95_category"] = assigned[name][0]
                n += 1
        doc["isa95_categories"] = sorted(set(c for c, _ in assigned.values()))
        style = dict(STYLE[p.name])
        nl = style.pop("trailing_nl")
        p.write_text(json.dumps(doc, **style) + ("\n" if nl else ""))
        print(f"wrote {n} categories -> {p.relative_to(APP.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
