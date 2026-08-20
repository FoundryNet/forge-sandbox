#!/usr/bin/env python3
"""Build the sandbox mapping packs from the PUBLIC sources.

Sources, and why each one is safe to ship in an open sandbox image:

  1. github.com/FoundryNet/canonical-schema (MIT, published 2026-08-10)
     schema/oem-mappings/{haas,fanuc,siemens,octoprint}.json
     Already open source. These are the real vendor tag -> canonical field
     mappings, so the sandbox resolves real tags to real canonical names.

  2. forge-prod verticals/building_bacnet.json
     The shipped BACnet/IP vertical pack. Object names are BACnet-standard
     (SupplyTemp, ChilledWaterTemp, ...), not proprietary corpus rows.

  3. forge-prod forge_core/canonicals.py -- the additive/3D-printing canonical
     names, paired with the raw tag names that prusa-telemetry-bridge.py
     actually emits off Marlin M105/M114.

What is deliberately NOT here: the production Supabase corpus (16,908 curated
mappings with confidence scores and provenance), the embedding index, the LLM
resolution path, the physics validators, and the vertical gate. Those stay
proprietary and server-side.

Run:  python3 tools/build_packs.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "app", "packs")

# Both source trees are supplied by the caller. canonical-schema is public and
# clonable; forge-prod is the private kernel repo, so only someone who already
# has it can regenerate the carrier and prusa packs. The generated packs are
# committed, so a clone of this repo needs neither in order to run the sandbox.
#
#   python3 tools/build_packs.py [canonical-schema-path] [forge-prod-path]
SCHEMA = os.path.expanduser(
    sys.argv[1] if len(sys.argv) > 1
    else os.environ.get("CANONICAL_SCHEMA", "../canonical-schema"))
FORGE_PROD = os.path.expanduser(
    sys.argv[2] if len(sys.argv) > 2
    else os.environ.get("FORGE_PROD", "../forge-prod"))


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _write(name, obj):
    path = os.path.join(OUT, name + ".json")
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")
    n = len(obj.get("mappings", {}))
    print(f"  {name:10s} {n:5d} mappings  ->  app/packs/{name}.json")


def from_canonical_schema(oem, *, vertical, display, protocol, aliases=()):
    """Lift an OEM's public mapping table straight out of the MIT schema repo."""
    src = _load(os.path.join(SCHEMA, "schema", "oem-mappings", f"{oem}.json"))
    return {
        "oem": oem,
        "display_name": display,
        "vertical": vertical,
        "protocol": protocol,
        "aliases": list(aliases),
        "source": "FoundryNet/canonical-schema (MIT) schema/oem-mappings/%s.json" % oem,
        "canonical_fields": src["canonical_fields"],
        "mappings": src["mappings"],
    }


def build_prusa():
    """Prusa MK3S over Marlin, plus the OctoPrint REST shape in front of it.

    The MIT schema repo ships `octoprint` (17 mappings) for the additive
    vertical but has no `prusa` family, because the corpus reached printers
    through OctoPrint. The bare-Marlin tags below are the exact keys
    prusa-telemetry-bridge.py emits from M105/M114; the canonical names they
    resolve to are the additive canonicals declared in forge_core/canonicals.py.
    """
    octo = _load(os.path.join(SCHEMA, "schema", "oem-mappings", "octoprint.json"))
    mappings = dict(octo["mappings"])

    # Bare Marlin M105 / M114, as read off the serial port.
    marlin = {
        "hotend_temp":        "hotend_temperature_c",
        "hotend_target":      "hotend_target_temperature_c",
        "heater_power":       "hotend_heater_pwm_output",
        "bed_temp":           "heated_bed_temperature_c",
        "bed_target":         "heated_bed_target_temperature_c",
        "bed_power":          "heated_bed_heater_pwm_output",
        "pinda_temp":         "pinda_probe_temperature_c",
        "ambient_temp":       "ambient_temperature",
        "position_x":         "axes.x_position_actual",
        "position_y":         "axes.y_position_actual",
        "position_z":         "axes.z_position_actual",
        "extruder_position":  "extruder_position_mm",
        # PrusaLink / PrusaConnect spellings of the same quantities.
        "temp_nozzle":        "hotend_temperature_c",
        "target_nozzle":      "hotend_target_temperature_c",
        "temp_bed":           "heated_bed_temperature_c",
        "target_bed":         "heated_bed_target_temperature_c",
        "speed":              "print_speed_mm_s",
        "flow":               "filament_flow_rate",
        "progress":           "print_progress_pct",
        "job_state":          "machine_state",
        "printer_state":      "machine_state",
        "material":           "program_id",
        "nozzle_diameter":    "nozzle_diameter_mm",
        "layer_height":       "layer_height_mm",
    }
    mappings.update(marlin)

    return {
        "oem": "prusa",
        "display_name": "Prusa MK3S/MK4 (Marlin, PrusaLink, OctoPrint)",
        "vertical": "additive",
        "protocol": "serial_gcode",
        "aliases": ["octoprint", "marlin", "prusalink"],
        "source": ("FoundryNet/canonical-schema (MIT) octoprint.json + additive "
                   "canonicals from forge_core/canonicals.py; raw tags as emitted "
                   "by prusa-telemetry-bridge.py"),
        "canonical_fields": sorted(set(mappings.values())),
        "mappings": mappings,
        # The corpus emits unit:null for every hotend PWM mapping, which hides
        # that Marlin's @: is a 0-127 duty byte, not a percentage. An agent that
        # reads 95 as "95% of max" is wrong by a third. The sandbox declares the
        # scale so integrations built here handle it correctly.
        "units": {
            "hotend_heater_pwm_output":     "pwm_0_127",
            "heated_bed_heater_pwm_output": "pwm_0_127",
        },
    }


def build_carrier():
    """Carrier rooftop / chiller over BACnet/IP.

    There is no `carrier` OEM family in the corpus and none in the MIT schema
    repo -- HVAC reaches Forge through the BACnet adapter, not a vendor pack.
    So this pack IS forge-prod's shipped building_bacnet vertical: standard
    BACnet object names, real canonical field names, plus the Carrier-specific
    object spellings you meet on an i-Vu / CCN gateway.
    """
    src = _load(os.path.join(FORGE_PROD, "verticals", "building_bacnet.json"))
    mappings = {}
    units = {}
    for row in src["mappings"]:
        mappings[row["raw_tag"]] = row["canonical_field"]
        if row.get("unit"):
            units[row["canonical_field"]] = row["unit"]

    # Carrier i-Vu / CCN object names for the same points.
    carrier_objects = {
        "SAT":                  "supply_air_temperature_c",
        "SA_TEMP":              "supply_air_temperature_c",
        "SupplyAirTemp":        "supply_air_temperature_c",
        "RAT":                  "return_air_temperature_c",
        "RA_TEMP":              "return_air_temperature_c",
        "ReturnAirTemp":        "return_air_temperature_c",
        "ZoneTemp":             "space_temperature_c",
        "SpaceTempSensor":      "space_temperature_c",
        "SFS":                  "fan_speed_pct",
        "SupplyFanVFD":         "fan_speed_pct",
        "OA_DAMPER":            "damper_position_pct",
        "OADamperPos":          "damper_position_pct",
        "CHWST":                "chilled_water_temperature_c",
        "ChwSupplyTemp":        "chilled_water_temperature_c",
        "CondPress":            "condenser_pressure_bar",
        "DischargePressure":    "condenser_pressure_bar",
        "kW":                   "power_consumption_kw",
        "TotalPower":           "power_consumption_kw",
        "kWh":                  "energy_kwh",
        "CO2_PPM":              "co2_ppm",
        "SpaceRH":              "relative_humidity_pct",
        "Occupancy":            "occupancy_state",
        "AlarmCode":            "alarm_code",
    }
    mappings.update(carrier_objects)

    return {
        "oem": "carrier",
        "display_name": "Carrier rooftop / chiller (BACnet/IP, i-Vu)",
        "vertical": "building_automation",
        "protocol": "bacnet_ip",
        "aliases": ["bacnet", "bacnet_ip", "trane", "jci", "honeywell"],
        "source": "forge-prod verticals/building_bacnet.json + Carrier i-Vu object names",
        "canonical_fields": sorted(set(mappings.values())),
        "mappings": mappings,
        "units": units,
    }


def build_canonical_fields(packs):
    """The canonical dictionary: name -> type, unit, description, vertical.

    Base is the 366 published fields (MIT). Fields that the packs resolve to but
    which the corpus never had mappings for -- the additive names and the
    building_automation names -- are declared here so the sandbox can still
    report a unit and a vertical for them instead of a null.
    """
    src = _load(os.path.join(SCHEMA, "schema", "fields.json"))
    out = {}
    for f in src["fields"]:
        out[f["field"]] = {
            "type": f.get("type"),
            "unit": f.get("unit"),
            "description": f.get("description"),
            "vertical": f.get("vertical"),
            "example_value": f.get("example_value"),
        }

    # Suffix -> (type, unit). The published corpus declares a unit for only 58
    # of its 366 fields, because the corpus had no unit column -- a null unit is
    # how "95" on a Marlin PWM byte got read as "95 percent". Anywhere the field
    # NAME states the unit, the sandbox fills it in rather than shipping a null.
    suffix_unit = [
        ("_pwm_output", "integer", "pwm_0_127"),
        ("_temperature_c", "float", "C"), ("_temp_c", "float", "C"),
        ("temperature_c", "float", "C"), ("temp_c", "float", "C"),
        ("_temperature", "float", "C"), ("_temp", "float", "C"),
        ("_pressure_bar", "float", "bar"), ("_bar", "float", "bar"),
        ("_pct", "float", "%"), ("_percent", "float", "%"),
        ("_kwh", "float", "kWh"), ("_kw", "float", "kW"),
        ("_ppm", "float", "ppm"), ("_dbm", "float", "dBm"),
        ("_mm_s", "float", "mm/s"), ("_mm_min", "float", "mm/min"),
        ("_rms", "float", "mm/s"), ("_mm", "float", "mm"),
        ("_rpm", "integer", "rpm"), ("_kg", "float", "kg"),
        ("_nm", "float", "Nm"), ("_volts", "float", "V"),
        ("_voltage", "float", "V"), ("_hours", "float", "h"),
        ("_seconds", "float", "s"), ("_ms", "float", "ms"),
        ("_liters", "float", "L"), ("_lpm", "float", "L/min"),
        ("_count", "integer", None), ("_score", "float", None),
    ]

    def infer(cf):
        """(type, unit) implied by the field name, or (None, None)."""
        for suf, t, u in suffix_unit:
            if cf.endswith(suf):
                return t, u
        if cf.endswith(("_state", "_status", "_code", "_name", "_id",
                        "_description", "_mode")):
            return "string", None
        return None, None

    # The "emitted-by-normalizer telemetry canonicals" block in
    # forge_core/canonicals.py: names production accepts as prediction fields
    # but which carry no OEM-attributed mappings, so fields.json omits them.
    emitted = [
        "spindle_temperature", "motor_temperature", "coolant_temperature",
        "oil_temperature", "bearing_temperature", "hydraulic_temperature",
        "ambient_temperature", "winding_temperature", "gearbox_temperature",
        "vibration_rms", "temperature_c", "power_consumption_kw",
        "feed_rate_mm_min",
    ]
    additive_extra = [
        "hotend_target_temperature_c", "hotend_power_pct", "bed_temperature_c",
        "bed_target_temperature_c", "bed_power_pct", "chamber_temperature_c",
        "pinda_probe_temperature_c", "extruder_position_mm",
        "nozzle_diameter_mm", "filament_flow_rate", "filament_diameter_mm",
        "layer_height_mm", "print_speed_mm_s", "bed_adhesion_score",
        "print_progress_pct",
    ]

    declared_verticals = {}
    declared_units = {}
    for name in emitted:
        declared_verticals.setdefault(name, "universal")
    for name in additive_extra:
        declared_verticals.setdefault(name, "additive")
    for pack in packs:
        for cf in pack["canonical_fields"]:
            declared_verticals.setdefault(cf, pack["vertical"])
        for cf, unit in (pack.get("units") or {}).items():
            declared_units[cf] = unit

    for cf, vertical in declared_verticals.items():
        if cf in out:
            # A pack that declares an explicit unit wins over an upstream null.
            if declared_units.get(cf) and not out[cf].get("unit"):
                out[cf]["unit"] = declared_units[cf]
                out[cf]["unit_source"] = "sandbox_pack_declared"
            continue
        itype, iunit = infer(cf)
        out[cf] = {
            "type": itype or "float",
            "unit": declared_units.get(cf) or iunit,
            "description": None,
            "vertical": vertical,
            "example_value": None,
            "unit_source": "sandbox_declared",
        }

    # Backfill nulls the published corpus could not fill.
    filled_unit = filled_type = 0
    for cf, entry in out.items():
        itype, iunit = infer(cf)
        if entry.get("unit") is None and iunit:
            entry["unit"] = iunit
            entry["unit_source"] = "sandbox_inferred_from_name"
            filled_unit += 1
        if entry.get("type") is None and itype:
            entry["type"] = itype
            filled_type += 1
    print(f"  backfilled {filled_unit} null units, {filled_type} null types "
          f"from field names")

    return {
        "name": "FoundryNet Canonical Schema (sandbox subset)",
        "base_version": src["version"],
        "license": "MIT",
        "upstream": "https://github.com/FoundryNet/canonical-schema",
        "field_count": len(out),
        "fields": out,
    }


def main():
    for path, label in ((SCHEMA, "canonical-schema"), (FORGE_PROD, "forge-prod")):
        if not os.path.isdir(path):
            sys.exit(f"missing source: {label} at {path}")

    os.makedirs(OUT, exist_ok=True)
    print("building sandbox packs from public sources:")

    packs = [
        from_canonical_schema("haas", vertical="cnc", display="Haas VF-series CNC",
                              protocol="mtconnect", aliases=["haas_automation"]),
        from_canonical_schema("fanuc", vertical="robotics",
                              display="FANUC R-30iB robot / Series 30i CNC",
                              protocol="focas", aliases=["fanuc_robotics", "focas"]),
        from_canonical_schema("siemens", vertical="cnc",
                              display="Siemens S7-1500 PLC / SINUMERIK 840D",
                              protocol="profinet_s7", aliases=["sinumerik", "s7", "simatic"]),
        build_prusa(),
        build_carrier(),
    ]

    index = {}
    for pack in packs:
        _write(pack["oem"], pack)
        index[pack["oem"]] = {
            "display_name": pack["display_name"],
            "vertical": pack["vertical"],
            "protocol": pack["protocol"],
            "aliases": pack["aliases"],
            "mapping_count": len(pack["mappings"]),
            "canonical_field_count": len(pack["canonical_fields"]),
        }

    meta = {
        "name": "Forge sandbox mapping packs",
        "generated_from": {
            "canonical_schema": _load(
                os.path.join(SCHEMA, "schema", "fields.json"))["version"],
            "license": "MIT (FoundryNet/canonical-schema)",
        },
        "note": ("Deterministic subset only. The production corpus, embedding "
                 "index, LLM resolution, and physics validators are not in this "
                 "image."),
        "packs": index,
    }
    _write("_index", meta)

    fields = build_canonical_fields(packs)
    with open(os.path.join(OUT, "_canonical_fields.json"), "w") as fh:
        json.dump(fields, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"  {'canonical':10s} {fields['field_count']:5d} fields    ->  "
          f"app/packs/_canonical_fields.json")

    total = sum(v["mapping_count"] for v in index.values())
    print(f"\n{len(packs)} packs, {total} mappings, {fields['field_count']} canonical fields")


if __name__ == "__main__":
    main()
