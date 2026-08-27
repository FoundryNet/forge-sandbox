"""Deterministic tag -> canonical-field resolution for the Forge sandbox.

Production Forge resolves a tag through five layers: corpus lookup, embedding
similarity, LLM research, physics validation, signal classification. This
sandbox ships three of them, and only the ones that need no model weights, no
network, and no proprietary data:

  layer 1  exact       raw tag is present verbatim in a pack
  layer 1b normalized  raw tag matches a pack tag once case, punctuation, and
                       unit suffixes are folded away ("SP_SPEED" -> "SP_SPEED [RPM]")
  layer 2  identity    the raw tag already IS a canonical field name
  layer 3  signal      a deterministic subject+quantity classifier, scored, and
                       accepted only against canonical names that really exist

Anything that survives all four is reported honestly as match_type "unknown"
with confidence 0.0 and its value passed through untouched. The sandbox never
invents a canonical name -- every name it emits comes out of the shipped
dictionary.
"""

import json
import math
import os
import re
from functools import lru_cache
from app.unit_converter import (convert_value as _convert_value,
                                declared_unit as _declared_unit)
from app.value_coercion import coerce_value as _coerce_value
from app.quantity_classifier import (UNIT_TO_QUANTITY as _UNIT_QTY,
                                     quantity_from_tag as _qty_tag,
                                     quantity_from_field as _qty_field,
                                     compatible as _qty_compatible)
from app.value_validator import (validate_sentinel as _validate_sentinel,
                                 is_sentinel_string,
                                 validate_bounds as _validate_bounds,
                                 normalize_enum_value as _normalize_enum)

PACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")

# Unit suffixes vendors staple onto tag names. Folding these is what lets one
# corpus row cover "F Rate (mm/min)", "F Rate_ipm", and "F_RATE (ipr)".
# Unit suffixes vendors staple onto tag names. Folding these is what lets one
# corpus row cover "F Rate (mm/min)", "F Rate_ipm", and "F_RATE (ipr)".
#
# The unit must be BRACKETED or DELIMITED. Allowing a bare trailing token meant
# single-letter units ate the end of real words: "program" folded to "progr"
# (m = metres, then a = amps), "alarms" to "alar" (ms), and
# "SPINDEL_AUSLASTUNG" to "spindelauslastun" (g = grams). A unit is only a unit
# when something separates it from the name.
_UNITS = (
    r"c|f|k|r|celsius|fahrenheit|kelvin|rankine|°c|°f|°k|°r|degc|degf|"
    r"mm|cm|m|in|inch|inches|ft|mil|um|µm|"
    r"rpm|1/min|min-1|rev/min|hz|"
    r"mm/min|mm/rev|mm/s|m/min|m/s|in/s|ips|ipm|ipr|fpm|mph|kph|"
    r"%|pct|percent|"
    r"kw|w|kwh|wh|mwh|j|kj|v|vac|vdc|kv|a|ma|"
    r"nm|n-m|n_m|knm|ft-lb|ftlb|lb|lbs|kg|g|oz|tonne|tons|"
    r"bar|psi|psia|psig|kpa|mpa|pa|mbar|inhg|mmhg|"
    r"l/min|lpm|ml/min|gpm|gal/min|l/s|m3/h|lbm/s|kg/s|"
    r"s|sec|seconds|ms|min|minutes|h|hr|hrs|hours|days|"
    r"pcs|parts|cycles|ppm|db|dbm"
)
_UNIT_SUFFIX = re.compile(
    r"(?:"
    r"[\s_]*[\(\[\{]\s*(?:deg\s*)?(?:" + _UNITS + r")\s*[\)\]\}]"
    r"|"
    r"[\s_]+(?:deg\s*)?(?:" + _UNITS + r")"
    r")\s*$",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^a-z0-9]+")


_VALUE_TAIL = re.compile(r":\s*-?\d+(?:\.\d+)?\s*$")


def _fold(tag: str) -> str:
    """Collapse a raw tag to its comparison key: lowercase, unit-stripped,
    punctuation-free. "SP LOAD PCT (%)" and "sp_load_pct" both fold to
    "sploadpct"."""
    s = str(tag).strip()
    # A naive split of a Marlin M105 report ("ok T:215.0 B:60.1") yields keys with
    # the VALUE baked into the tag: {"T:215.0": 215.0}. Strip a trailing ":<number>"
    # so the tag folds to "t" and resolves like the well-formed "T". Deliberately
    # narrow — only a numeric tail is removed, so a namespaced tag such as
    # "ns:temp" is untouched.
    s = _VALUE_TAIL.sub("", s).strip() or s
    # Strip repeatedly: "TOTAL_HOURS [minutes]" -> "TOTAL_HOURS" needs one pass,
    # but "X_Motor_Temp(degC)" style stacking can need two.
    for _ in range(3):
        stripped = _UNIT_SUFFIX.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    return _PUNCT.sub("", s.lower())


def _coerce_numeric(value):
    """A JSON string that holds a number is still a number.

    Many OPC UA -> JSON and Modbus -> MQTT bridges quote every value, so a plant
    that sends {"S1Temp": "9999"} instead of {"S1Temp": 9999} used to skip the
    sentinel gate, the physics bounds AND unit conversion in one step, while
    coverage_pct still read 100%. Every guarantee this engine makes was silently
    disabled by a gateway setting. Coercing here puts quoted numbers back on the
    same path as native ones.

    String SENTINELS are checked first and left alone -- "NaN", "N/A" and
    "UNAVAILABLE" must reach the string-sentinel gate as strings. A genuine
    non-numeric string ("RUNNING") is returned untouched for enum handling.

    Returns (value, record | None); the record is reported so a coercion is
    never invisible.
    """
    if not isinstance(value, str):
        return value, None
    if is_sentinel_string(value) is not None:
        return value, None
    s = value.strip()
    if not s:
        return value, None
    try:
        num = float(s)
    except (TypeError, ValueError):
        return value, None
    if not math.isfinite(num):
        # "1e309" parses to inf. Hand it back as a token the string-sentinel
        # gate already knows, so it nulls with a reason instead of flowing on.
        return ("INF" if num > 0 else "-INF"), {
            "raw_value": value, "coerced_to": None,
            "note": "non-finite numeric string treated as a sentinel",
        }
    # Keep integers integral so 65535 still matches the uint16 sentinel exactly
    # and does not become 65535.0 in the output.
    if "." not in s and "e" not in s.lower() and float(num).is_integer():
        num = int(num)
    return num, {"raw_value": value, "coerced_to": num,
                 "note": "numeric string coerced to a number before validation"}


# Unit families, so a query tagged `_h` still matches a corpus row tagged
# `(hours)`. Only used to disambiguate a fold collision -- never to convert.
_UNIT_FAMILY = {
    "h": "time", "hr": "time", "hrs": "time", "hour": "time", "hours": "time",
    "min": "time", "mins": "time", "s": "time", "sec": "time", "secs": "time",
    "w": "power", "kw": "power", "mw": "power", "watt": "power", "watts": "power",
    "wh": "energy", "kwh": "energy", "mwh": "energy", "j": "energy",
    "v": "voltage", "kv": "voltage", "mv": "voltage",
    "a": "current", "ma": "current", "amp": "current", "amps": "current",
    "c": "temperature", "degc": "temperature", "f": "temperature",
    "degf": "temperature", "k": "temperature",
    "rpm": "rotational", "hz": "frequency", "khz": "frequency",
    "pct": "ratio", "percent": "ratio", "ratio": "ratio",
    "bar": "pressure", "psi": "pressure", "kpa": "pressure", "mbar": "pressure",
    "pa": "pressure",
    "cycles": "count", "count": "count", "parts": "count",
    # power / energy beyond the SI core -- a nameplate in hp or VA is still a
    # power reading, and confusing it with an energy total is the exact failure
    # this table exists to prevent.
    "hp": "power", "va": "power", "kva": "power", "mva": "power",
    "mwh": "energy", "kj": "energy", "btu": "energy", "therm": "energy",
    # volumetric flow -- the units a pump is actually tagged with
    "gpm": "flow", "lpm": "flow", "lps": "flow", "m3h": "flow",
    "cfm": "flow", "mgd": "flow", "bopd": "flow", "mcfd": "flow",
    "ntu": "turbidity", "ph": "ph", "us": "conductivity", "uscm": "conductivity",
    "nm": "torque", "ftlb": "torque",
    "mbar": "pressure", "inhg": "pressure", "kg": "mass", "tonnes": "mass",
    "t": "mass", "lb": "mass", "lbs": "mass",
}


def _fold_parts(tag: str):
    """`_fold`, but it also hands back the unit tokens it stripped.

    The unit suffix is not noise -- it is often the ONLY thing separating two
    different measurements that share a name. `TOTAL_HOURS (h)` and `Total_kW`
    both fold to "total"; throwing the suffix away is what let a power meter's
    Total resolve to operating_hours. Keeping it lets the collision be settled
    on evidence instead of on the alphabet.
    """
    s = str(tag).strip()
    s = _VALUE_TAIL.sub("", s).strip() or s
    units = []
    for _ in range(3):
        m = _UNIT_SUFFIX.search(s)
        stripped = _UNIT_SUFFIX.sub("", s).strip()
        if stripped == s or not stripped:
            break
        if m:
            tok = _PUNCT.sub("", m.group(0).lower())
            if tok.startswith("deg"):
                tok = tok[3:] or tok
            if tok:
                units.append(tok)
        s = stripped
    return _PUNCT.sub("", s.lower()), units


def _unit_keys(units):
    """Comparable forms of a tag's unit tokens: the token and its family."""
    keys = set()
    for u in units or []:
        u = u.lower()
        keys.add(u)
        fam = _UNIT_FAMILY.get(u)
        if fam:
            keys.add("fam:" + fam)
    return keys


def _pick_by_unit(tag, candidates, require_unit_match=False):
    """Settle a fold collision using the unit token.

    `candidates` is [(raw_tag, canonical, units)]. Returns
    (chosen | None, reason). A tie that the units cannot break returns None --
    answering at random is what the caller must never do.
    """
    distinct = {c[1] for c in candidates}
    want = _unit_keys(_fold_parts(tag)[1])
    if require_unit_match:
        # Used where the ONLY warrant for the match is the unit -- an
        # unrecognized oem, where no domain could be checked. A lone candidate
        # is not evidence, so the single-candidate shortcut below must not
        # apply: without this, `Flow_GPM` from an unknown vendor matched the
        # Prusa pack's unitless `flow` row and came back filament_flow_rate,
        # which is the precise failure the domain guard exists to stop.
        hits = [c for c in candidates if _unit_keys(c[2]) & want] if want else []
        if len({h[1] for h in hits}) == 1:
            return hits[0], None
        return None, ("no unit-compatible row for '%s' among %s"
                      % (tag, sorted(distinct)))
    if len(distinct) == 1:
        return candidates[0], None
    if want:
        hits = [c for c in candidates if _unit_keys(c[2]) & want]
        if len(hits) == 1:
            return hits[0], None
        if len({h[1] for h in hits}) == 1 and hits:
            return hits[0], None
    return None, ("ambiguous_fold: '%s' matches %s and the tag carries %s"
                  % (tag, sorted(distinct),
                     "no unit to disambiguate them" if not want
                     else "units %s, which fit more than one" % sorted(want)))


# Units that are real, unmistakable, and carry a large scale factor, for which
# no converter exists. Naming them explicitly keeps the fail-closed guard from
# firing on tokenizer artifacts.
_UNCONVERTIBLE_UNITS = {
    "mgd",      # million US gallons per day  (~2,629 L/min)
    "bopd",     # barrels of oil per day
    "bpd",
    "mcfd",     # thousand cubic feet per day
    "mmcfd",
    "scfm",     # standard cubic feet per minute
    "acfm",
    "gpd",      # gallons per day
    "bbl",      # barrels
    "boe",
}


def _normalize_unit_token(tok):
    """A unit as a human wrote it -> the symbol the converter knows.

    "72.1 degF" carries its unit in the value, but the converter is built around
    the symbols the corpus uses. Without this the extracted hint is passed
    through as "degF", matches nothing, and the reading silently stays in
    Fahrenheit under a Celsius field.
    """
    if not tok:
        return None
    t = str(tok).strip()
    key = _PUNCT.sub("", t.lower())
    if key.startswith("deg") and len(key) > 3:
        key = key[3:]
    return {
        "f": "F", "fahrenheit": "F", "c": "C", "celsius": "C", "k": "K",
        "psi": "psi", "bar": "bar", "kpa": "kPa", "mbar": "mbar", "pa": "Pa",
        "rpm": "rpm", "hz": "Hz", "khz": "kHz",
        "w": "W", "kw": "kW", "mw": "MW", "hp": "hp", "va": "VA",
        "wh": "Wh", "kwh": "kWh", "mwh": "MWh", "j": "J", "kj": "kJ",
        "v": "V", "kv": "kV", "mv": "mV",
        "a": "A", "ma": "mA", "amp": "A", "amps": "A",
        "gpm": "gal/min", "lpm": "L/min", "lps": "L/s", "cfm": "ft3/min",
        "m3h": "m3/h", "mph": "mph", "ms": "m/s", "kmh": "km/h",
        "in": "in", "inch": "in", "mm": "mm", "m": "m", "ft": "ft",
        "pct": "%", "percent": "%",
        "h": "h", "hr": "h", "hrs": "h", "hours": "h",
        "min": "min", "s": "s", "sec": "s",
        "nm": "Nm", "ntu": "NTU", "ppm": "ppm",
    }.get(key)


def _tokens(tag: str) -> list:
    """Split a tag into lowercase word tokens, breaking camelCase and digits.
    "S1Temp" -> ["s", "1", "temp"];  "SupplyFanSpeed" -> ["supply","fan","speed"]
    """
    s = _UNIT_SUFFIX.sub("", str(tag).strip())
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Za-z])([0-9])", r"\1 \2", s)
    s = re.sub(r"([0-9])([A-Za-z])", r"\1 \2", s)
    return [t for t in _PUNCT.split(s.lower()) if t]


# ── Layer 3: the signal classifier ───────────────────────────────────────────
# A tag names a SUBJECT (what part) and a QUANTITY (what is measured about it).
# Both halves are matched from fixed vocabularies, then the pair is looked up
# against canonical names that actually exist. No free-form invention.

_SUBJECT = {
    "spindle": "spindle", "spdl": "spindle", "sp": "spindle", "s": "spindle",
    "motor": "motor", "mtr": "motor", "servo": "motor", "drive": "drive",
    "coolant": "coolant", "cool": "coolant", "cutting": "coolant",
    "oil": "oil", "hydraulic": "hydraulic", "hyd": "hydraulic",
    "bearing": "bearing", "brg": "bearing", "gearbox": "gearbox",
    "ambient": "ambient", "room": "ambient", "cabinet": "ambient",
    "winding": "winding", "stator": "winding",
    "hotend": "hotend", "nozzle": "hotend", "extruder": "hotend",
    "bed": "heated_bed", "heatbed": "heated_bed", "heated": "heated_bed",
    "chamber": "chamber", "enclosure": "chamber",
    "supply": "supply_air", "discharge": "supply_air",
    "return": "return_air", "space": "space", "zone": "space",
    "chilled": "chilled_water", "chw": "chilled_water",
    "condenser": "condenser", "cond": "condenser",
    "evaporator": "evaporator", "evap": "evaporator",
    "feed": "feed", "rapid": "rapid", "tool": "tool", "part": "part",
    "axis": "axis", "tcp": "tcp", "joint": "joint", "payload": "payload",
    "battery": "battery", "bus": "bus", "fan": "fan", "damper": "damper",
    "engine": "engine", "fuel": "fuel", "filament": "filament",
    "pump": "pump", "tank": "tank", "vessel": "tank", "header": "pump",
}

_QUANTITY = {
    "temp": "temperature", "temperature": "temperature", "tmp": "temperature",
    "therm": "temperature",
    "load": "load", "util": "load", "utilization": "load",
    "speed": "speed", "rpm": "speed", "vel": "speed", "velocity": "speed",
    "rate": "rate", "feedrate": "rate", "flow": "flow",
    "press": "pressure", "pressure": "pressure",
    "torque": "torque", "trq": "torque",
    "power": "power", "kw": "power", "watts": "power",
    "energy": "energy", "kwh": "energy",
    "current": "current", "amp": "current", "amps": "current",
    "volt": "voltage", "voltage": "voltage", "volts": "voltage",
    "vibration": "vibration", "vib": "vibration",
    "position": "position", "pos": "position",
    "count": "count", "cnt": "count", "qty": "count",
    "level": "level",
    "hours": "hours", "hrs": "hours", "runtime": "hours",
    "override": "override", "ovr": "override",
    "humidity": "humidity", "rh": "humidity",
    "progress": "progress", "completion": "progress",
    "pwm": "pwm", "duty": "pwm",
    # NB: "sp" is deliberately absent here. On a CNC tag "SP" means spindle
    # (SP_SPEED, SP_LOAD_PCT), not setpoint, and claiming both makes
    # "SP_SPEED" resolve to a setpoint instead of a spindle speed.
    "target": "target", "setpoint": "target", "cmd": "target",
    "commanded": "target",
    "state": "state", "status": "state", "mode": "state",
    "alarm": "alarm", "fault": "alarm", "error": "alarm", "err": "alarm",
}

# subject+quantity -> the canonical name to try. Every value here is checked
# against the shipped dictionary at load time, so a typo fails loudly.
_SIGNAL_MAP = {
    ("spindle", "temperature"): "spindle_temperature",
    ("spindle", "load"):        "spindle_load_pct",
    ("spindle", "speed"):       "spindle_speed_rpm",
    ("spindle", "torque"):      "sensor_readings.torque",
    ("spindle", "override"):    "spindle_override_pct",
    ("motor", "temperature"):   "motor_temperature",
    ("motor", "torque"):        "sensor_readings.torque",
    ("motor", "power"):         "power_consumption_kw",
    ("drive", "temperature"):   "sensor_readings.drive_temp",
    ("coolant", "temperature"): "sensor_readings.coolant_temp",
    ("coolant", "pressure"):    "sensor_readings.coolant_pressure",
    ("pump", "flow"):           "flow_rate_lpm",
    ("pump", "pressure"):       "pressure_kpa",
    ("tank", "level"):          "tank_level_pct",
    ("oil", "temperature"):     "oil_temperature",
    ("hydraulic", "temperature"): "hydraulic_temperature",
    ("bearing", "temperature"): "bearing_temperature",
    ("gearbox", "temperature"): "gearbox_temperature",
    ("ambient", "temperature"): "ambient_temperature",
    ("winding", "temperature"): "winding_temperature",
    ("hotend", "temperature"):  "hotend_temperature_c",
    ("hotend", "target"):       "hotend_target_temperature_c",
    ("hotend", "pwm"):          "hotend_heater_pwm_output",
    ("heated_bed", "temperature"): "heated_bed_temperature_c",
    ("heated_bed", "target"):   "heated_bed_target_temperature_c",
    ("heated_bed", "pwm"):      "heated_bed_heater_pwm_output",
    ("chamber", "temperature"): "chamber_temperature_c",
    ("supply_air", "temperature"): "supply_air_temperature_c",
    ("return_air", "temperature"): "return_air_temperature_c",
    ("space", "temperature"):   "space_temperature_c",
    ("space", "humidity"):      "relative_humidity_pct",
    ("chilled_water", "temperature"): "chilled_water_temperature_c",
    ("condenser", "pressure"):  "condenser_pressure_bar",
    ("fan", "speed"):           "fan_speed_pct",
    ("damper", "position"):     "damper_position_pct",
    ("feed", "rate"):           "feed_rate",
    ("feed", "override"):       "feed_override_pct",
    ("rapid", "override"):      "rapid_override_pct",
    ("tool", "count"):          "tool_id",
    ("part", "count"):          "part_count",
    ("tcp", "speed"):           "sensor_readings.tcp_speed",
    ("payload", "load"):        "payload_kg",
    ("battery", "voltage"):     "battery_voltage",
    ("bus", "voltage"):         "dc_bus_voltage",
    ("engine", "speed"):        "engine_speed_rpm",
    ("engine", "load"):         "engine_load_pct",
    ("filament", "flow"):       "filament_flow_rate",
}

# Quantity alone is enough when no subject is named.
_BARE_QUANTITY = {
    "energy":    "energy_kwh",
    "power":     "power_consumption_kw",
    "vibration": "vibration_rms",
    "humidity":  "relative_humidity_pct",
    "alarm":     "alarm_code",
    "state":     "execution_state",
    "hours":     "operating_hours",
    "progress":  "print_progress_pct",
    # A pump, a main or a header tagged only "Flow" is the commonest industrial
    # reading there is. Without a universal home it either went unresolved or --
    # worse -- borrowed `filament_flow_rate` from the 3D-printer pack.
    "flow":      "flow_rate_lpm",
}

# Canonical names that ended up with two spellings. Mirrors the production
# CANONICAL_ALIASES table so a sandbox integration sees the same primaries.
CANONICAL_ALIASES = {
    "spindle_speed_rpm":    ["cnc.spindle.rotary_velocity", "spindle.speed",
                             "spindle_rotary_velocity", "rotary_velocity"],
    "vibration_rms":        ["vibration.rms", "vibration_rms_mm_s", "vib_rms_mm_s",
                             "vibration_mm_s", "vibration_velocity_mm_s"],
    "spindle_temperature":  ["spindle_temperature_c", "spindle_temp",
                             "spindle_temp_c", "spindle.temperature"],
    "motor_temperature":    ["motor_temperature_c", "motor_temp", "motor.temperature"],
    "spindle_load_pct":     ["spindle.load", "spindle_load_percent"],
    "power_consumption_kw": ["motor_power_kw", "power_kw", "electrical_power_kw"],
    "feed_rate_mm_min":     ["feedrate_mm_min", "feed.rate"],
}

# Axis fields arrived in TWO competing shapes because the canonical schema
# carries both: an INDEXED form (axes.0.position_actual, 431 corpus mappings) and
# a LETTERED form (axes.x_position_actual, 34). Vendors picked whichever the
# corpus offered, so Haas emitted axes.0.* while Siemens and Prusa emitted
# axes.x_* — for the SAME physical axis. A fleet query for X position silently
# missed whichever machines used the other shape. Worse, it was inconsistent
# WITHIN a vendor: Siemens "ActPos_X" -> axes.x_position_actual but
# "$AAIM[X] (m)" -> axes.0.position_actual.
#
# One shape wins: the lettered form. These aliases fold the indexed form into it
# so both spellings resolve to one primary, without rewriting 431 pack entries.
_AXIS_INDEX_TO_LETTER = {"0": "x", "1": "y", "2": "z", "3": "a", "4": "b", "5": "c"}
_AXIS_SUFFIX_CANON = {
    "position_actual": "position_actual",
    "position_commanded": "position_commanded",
    "temperature_c": "temperature",
    "temperature": "temperature",
    "load_percent": "load_pct",
    "load_pct": "load_pct",
    "following_error": "following_error",
}
for _i, _l in _AXIS_INDEX_TO_LETTER.items():
    for _raw, _canon in _AXIS_SUFFIX_CANON.items():
        _primary = f"axes.{_l}_{_canon}"
        CANONICAL_ALIASES.setdefault(_primary, [])
        _indexed = f"axes.{_i}.{_raw}"
        if _indexed not in CANONICAL_ALIASES[_primary]:
            CANONICAL_ALIASES[_primary].append(_indexed)

ALIAS_TO_PRIMARY = {}
for _p, _as in CANONICAL_ALIASES.items():
    ALIAS_TO_PRIMARY[_p] = _p
    for _a in _as:
        ALIAS_TO_PRIMARY[_a] = _p


def resolve_canonical(name):
    """Resolve any canonical name (including aliases) to its primary."""
    return ALIAS_TO_PRIMARY.get(name, name) if name else name


# ── Unit conversion ──────────────────────────────────────────────────────────
# The tag carries the unit; the canonical field declares one. When they differ,
# convert and say so. Production does this too, and reports it as
# `unit_conversions` in the response.

_UNIT_IN_TAG = re.compile(r"[\(\[_]\s*(°?[A-Za-z/\-]+)\s*[\)\]]?\s*$")

_UNIT_CANON = {
    "°c": "c", "degc": "c", "celsius": "c", "c": "c",
    "°f": "f", "degf": "f", "fahrenheit": "f", "f": "f",
    "in": "in", "inch": "in", "inches": "in", "mm": "mm", "m": "m", "cm": "cm",
    "lb": "lb", "lbs": "lbs", "kg": "kg", "g": "g",
    "w": "w", "kw": "kw", "kwh": "kwh",
    "psi": "psi", "bar": "bar", "kpa": "kpa", "mbar": "mbar",
    "min": "min", "minutes": "minutes", "h": "h", "hr": "h", "hrs": "h",
    "hours": "h", "ipm": "ipm", "mm/min": "mm/min", "rpm": "rpm",
}


def _tag_unit(tag: str):
    """Pull the declared unit out of a raw tag name, if it has one."""
    m = _UNIT_IN_TAG.search(str(tag).strip())
    if not m:
        return None
    return _UNIT_CANON.get(m.group(1).strip().lower())


def _target_unit(canonical: str, dictionary: dict):
    entry = dictionary.get(canonical) or {}
    unit = (entry.get("unit") or "").strip().lower()
    return _UNIT_CANON.get(unit, unit or None)


# ── Pack loading ─────────────────────────────────────────────────────────────

class Pack:
    def __init__(self, raw: dict):
        self.oem = raw["oem"]
        self.display_name = raw["display_name"]
        self.vertical = raw["vertical"]
        self.protocol = raw["protocol"]
        self.aliases = raw.get("aliases", [])
        self.source = raw.get("source")
        self.canonical_fields = raw["canonical_fields"]
        self.mappings = raw["mappings"]
        self.units = raw.get("units", {})
        # Source unit per RAW TAG, as the OEM puts it on the wire. Distinct from
        # `units` above, which describes the canonical field's own unit. Needed
        # wherever a register is named for its quantity with no suffix to read
        # (SunSpec `W`, `WH`, `Hz`) — see convert_value(source_unit=...).
        self.tag_units = raw.get("tag_units", {})
        self.tag_units_folded = {_fold(t): u for t, u in sorted(self.tag_units.items())}
        # Default SunSpec model for devices this pack covers, used only when the
        # reading carries no ID register and the caller named none.
        self.sunspec_model = raw.get("sunspec_model")
        # {point: sf_point} for vendor SunSpec implementations whose scale
        # factors are shared or renamed, so neither the model definition nor
        # the X/X_SF convention can pair them.
        self.sunspec_sf_map = raw.get("sunspec_sf_map")
        # Folded index. Vendors ship the same concept under many unit-suffixed
        # spellings; if two of them disagree on the canonical, the first wins
        # deterministically (sorted) rather than by dict insertion order.
        self.folded = {}
        # Every row that folds to the same key, so a collision can be settled on
        # the unit token instead of silently taking whichever sorted first.
        self.folded_all = {}
        for tag in sorted(self.mappings):
            key, units = _fold_parts(tag)
            self.folded.setdefault(key, (tag, self.mappings[tag]))
            self.folded_all.setdefault(key, []).append(
                (tag, self.mappings[tag], units))


@lru_cache(maxsize=1)
def load():
    """Load every pack plus the canonical dictionary. Cached for process life."""
    packs, by_alias = {}, {}
    for fn in sorted(os.listdir(PACK_DIR)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        with open(os.path.join(PACK_DIR, fn)) as fh:
            pack = Pack(json.load(fh))
        packs[pack.oem] = pack
        by_alias[pack.oem] = pack.oem
        for a in pack.aliases:
            by_alias.setdefault(a, pack.oem)

    with open(os.path.join(PACK_DIR, "_canonical_fields.json")) as fh:
        dictionary = json.load(fh)

    # Fail loudly if the signal map ever points at a name the dictionary lacks.
    known = dictionary["fields"]
    bad = sorted({c for c in list(_SIGNAL_MAP.values()) + list(_BARE_QUANTITY.values())
                  if c not in known})
    if bad:
        raise RuntimeError(f"signal classifier targets unknown canonicals: {bad}")

    return packs, by_alias, dictionary


def known_oems():
    packs, _, _ = load()
    return sorted(packs)


def get_pack(oem):
    """Resolve an oem hint (or alias) to a pack. None if unrecognized."""
    packs, by_alias, _ = load()
    if not oem:
        return None
    return packs.get(by_alias.get(str(oem).strip().lower()))


def _signal_guess(tag: str, dictionary: dict):
    """Layer 3. Returns (canonical, confidence, rationale) or None."""
    toks = _tokens(tag)
    if not toks:
        return None
    # A token claimed as the subject cannot also be counted as the quantity,
    # otherwise a tag like "temp_sensor_temp" double-counts and a token that
    # appears in both vocabularies resolves against itself.
    subjects, quantities, used = [], [], set()
    for i, t in enumerate(toks):
        if t in _SUBJECT:
            subjects.append(_SUBJECT[t])
            used.add(i)
    for i, t in enumerate(toks):
        if i not in used and t in _QUANTITY:
            quantities.append(_QUANTITY[t])

    # "hotend_target_temp" names both a setpoint and a temperature. The
    # setpoint is the more specific claim, so it wins.
    if "target" in quantities and len(quantities) > 1:
        quantities = ["target"]

    def _claims_modality_tag_lacks(canonical):
        """A canonical that names AC/DC must not be claimed from a tag that
        never said which. `Bus_Voltage_kV` on a 138 kV substation bus is AC;
        answering `dc_bus_voltage` invents the modality and the reading lands
        under a DC field. The tag has to say `dc` before we may."""
        for mode in ("dc", "ac"):
            if canonical.startswith(mode + "_") and mode not in toks:
                return True
        return False

    for subj in subjects:
        for qty in quantities:
            hit = _SIGNAL_MAP.get((subj, qty))
            if hit and _claims_modality_tag_lacks(hit):
                continue
            if hit and hit in dictionary:
                # Two named halves is a strong signal; still below an exact
                # corpus row, which is why it tops out at 0.72.
                return hit, 0.72, f"signal:{subj}+{qty}"

    for qty in quantities:
        hit = _BARE_QUANTITY.get(qty)
        if hit and _claims_modality_tag_lacks(hit):
            continue
        if hit and hit in dictionary:
            return hit, 0.55, f"signal:{qty}"
    return None


# ── Domain guard for cross-pack resolution ───────────────────────────────────
# A fold match out of ANOTHER vendor's pack is only credible when both vendors
# build the same KIND of machine. Without this guard `Flow_GPM` on an
# Allen-Bradley pump folds to `flow`, resolves out of the Prusa pack as
# `filament_flow_rate` -- a 3D-printer extruder field -- and is then unit
# converted to L/min, which lends a domain error the authority of a real
# measurement. Refusing the match is strictly better than answering confidently
# from the wrong industry.

_OEM_DOMAINS = {
    # CNC / machining
    "haas": "cnc", "fanuc": "cnc", "siemens": "cnc",
    "mazak": "cnc", "okuma": "cnc", "dmg_mori": "cnc",
    "doosan": "cnc", "makino": "cnc", "brother": "cnc",

    # Robotics
    "fanuc_robot": "robotics", "kuka": "robotics",
    "abb_robot": "robotics", "universal_robots": "robotics",

    # 3D printing
    "prusa": "3dp", "stratasys": "3dp", "markforged": "3dp",

    # Energy
    "tesla": "energy", "fronius": "energy",
    "schneider": "energy", "solaredge": "energy",
    "sma": "energy", "victron": "energy",
    "sunspec": "energy", "sungrow": "energy",
    "goodwe": "energy", "growatt": "energy",

    # HVAC / building
    "carrier": "hvac", "trane": "hvac",
    "johnson_controls": "hvac", "honeywell": "hvac",

    # Industrial general
    "rockwell": "industrial", "allen_bradley": "industrial",
    "emerson": "industrial", "yokogawa": "industrial",
    "mitsubishi": "industrial", "omron": "industrial",

    # Automotive / fleet
    "vehicle": "automotive", "j1939": "automotive",

    # Verticals a Kepware/Ignition partner evaluation reaches for that we ship
    # no pack for. Naming the domain is what turns a confident cross-vertical
    # guess into an honest refusal, so these earn their place even with no
    # mappings behind them.
    "hach": "water", "endress": "water", "ysi": "water",
    "krones": "packaging", "tetra_pak": "packaging", "sidel": "packaging",
    "sartorius": "pharma", "eppendorf": "pharma", "thermo_fisher": "pharma",
    "caterpillar": "heavy_equipment", "komatsu": "heavy_equipment",
    "liebherr": "heavy_equipment", "hitachi_ce": "heavy_equipment",
    "wartsila": "marine", "man_es": "marine", "cummins": "marine",
    "abb": "utility", "ge_grid": "utility", "sel": "utility",
    "siemens_energy": "utility",
}

# Packs label themselves with a `vertical` drawn from the canonical schema's
# vocabulary, which is not the same vocabulary as the OEM table above. Map both
# into one domain space so a pack and a caller's oem hint can be compared at all.
_VERTICAL_DOMAINS = {
    "cnc": "cnc",
    "robotics": "robotics",
    "additive": "3dp",
    "energy": "energy",
    "building_automation": "hvac",
    "vehicle": "automotive",
    "amr": "robotics",
    "industrial": "industrial",
    "water": "water",
}

# A vendor-neutral pack describes no particular industry, so it stays eligible
# to answer for any domain -- this is the pack that SHOULD win a generic tag.
_DOMAIN_NEUTRAL_PACKS = {"generic_iot", "generic"}

# When the caller names an OEM we cannot place, a cross-pack answer is a guess
# about an unknown machine. False keeps the documented behaviour (guess, label
# it cross_oem, disclose the note); True refuses instead. Measured both ways --
# see REPORT.
STRICT_UNKNOWN_OEM = True


def domain_from_oem(oem) -> "str | None":
    """The equipment domain an OEM / category hint implies, or None if the name
    is not one we can place. Longest key first, so `fanuc_robot` is not shadowed
    by `fanuc`."""
    o = str(oem or "").strip().lower()
    if not o:
        return None
    if o in _OEM_DOMAINS:
        return _OEM_DOMAINS[o]
    for key in sorted(_OEM_DOMAINS, key=len, reverse=True):
        if o.startswith(key):
            return _OEM_DOMAINS[key]
    return None


def domain_from_pack(pack) -> "str | None":
    """The domain a pack speaks for. The OEM table wins over the pack's declared
    vertical because a vendor can straddle two of them: the `fanuc` pack covers
    both the R-30iB robot and the Series 30i CNC, and gating it as robotics-only
    would refuse legitimate CNC exchanges (a Haas PART_COUNT resolving out of
    fanuc, a FANUC SPINDLE_SPEED_ACT resolving out of haas)."""
    if pack is None:
        return None
    return (domain_from_oem(getattr(pack, "oem", None))
            or _VERTICAL_DOMAINS.get(getattr(pack, "vertical", None)))


def _quantity_ok(tag, canonical, fields, tag_units=None) -> bool:
    """Does the tag measure the same PHYSICAL QUANTITY as the field?

    Independent of the domain guard and catches a different class of error. Two
    tags can fold together, belong to plausible domains, and still describe
    incomparable measurements -- `Total_kW` (power) against `operating_hours`
    (time), `Flow_GPM` (volume/time) against `filament_flow_rate` (mass/time).
    Unknown on either side allows the match: missing evidence is not contrary
    evidence.
    """
    if not canonical:
        return True
    return _qty_compatible(_qty_tag(tag, tag_units),
                           _qty_field(canonical, fields.get(canonical) or {}))


def _signal_target_allowed(source_domain, canonical, fields) -> bool:
    """Whether the signal classifier may answer `source_domain` with `canonical`.

    Layer 3 pairs a subject with a quantity, which is domain-blind: "feed rate"
    is a real reading on a CNC and on a bioreactor, but `feed_rate` is the CNC
    one. A canonical declared `universal` (163 of 435 fields -- alarms, states,
    operating hours, motor temperature) is safe for anyone. A canonical that
    declares a SPECIFIC vertical is only safe for that vertical.
    """
    vert = (fields.get(canonical) or {}).get("vertical")
    if vert in (None, "universal"):
        return True
    target = _VERTICAL_DOMAINS.get(vert)
    if target is None:
        return True
    if source_domain is None:
        return not STRICT_UNKNOWN_OEM
    return source_domain == target


def _cross_pack_allowed(source_domain, other_pack) -> bool:
    """Whether `other_pack` may answer for a reading from `source_domain`."""
    if getattr(other_pack, "oem", None) in _DOMAIN_NEUTRAL_PACKS:
        return True
    match_domain = domain_from_pack(other_pack)
    if source_domain is None:
        return not STRICT_UNKNOWN_OEM
    if match_domain is None:
        return True
    return source_domain == match_domain


def resolve_field(tag: str, pack, dictionary: dict, oem=None) -> dict:
    """Resolve one raw tag. Always returns a mapping record -- never raises,
    never invents a name."""
    fields = dictionary["fields"]

    # Layer 1: exact corpus row.
    if pack and tag in pack.mappings:
        return {"canonical_field": pack.mappings[tag], "confidence": 1.0,
                "match_type": "corpus", "layer": 1, "source": f"pack:{pack.oem}",
                "matched_tag": tag}

    # Layer 1b: same row, once units and punctuation are folded away.
    ambiguous = None
    if pack:
        cands = pack.folded_all.get(_fold(tag))
        if cands:
            _tu = _fold_parts(tag)[1]
            _kept = [c for c in cands if _quantity_ok(tag, c[1], fields, _tu)]
            # When the tag carries an EXPLICIT unit, a quantity clash is hard
            # evidence and the row is refused outright: `Flow_DegF` is a
            # temperature however much it folds onto a flow row. With no unit,
            # the clash was only inferred from a keyword, and the vendor's own
            # declared row outranks our inference -- that fallback is what keeps
            # oddly-named but correct corpus rows working.
            cands = _kept if (_tu or _kept) else cands
            chosen, why = _pick_by_unit(tag, cands)
            if chosen:
                return {"canonical_field": chosen[1], "confidence": 0.95,
                        "match_type": "corpus_normalized", "layer": 1,
                        "source": f"pack:{pack.oem}", "matched_tag": chosen[0]}
            ambiguous = why

    # Layer 2: the tag is already canonical.
    if tag in fields:
        return {"canonical_field": tag, "confidence": 1.0,
                "match_type": "identity", "layer": 2, "source": "canonical_schema",
                "matched_tag": tag}

    # Layer 1c: some other pack knows this tag. Lower confidence, because the
    # caller's oem hint said otherwise -- production calls this cross-vertical
    # and gates it; here it is reported, not hidden.
    packs, _, _ = load()
    folded = _fold(tag)
    # Vendor-neutral pack first, then alphabetical for determinism. Plain
    # alphabetical order meant `fanuc` answered before `generic_iot`, so a
    # weather-station tag (`ambient_temp_f`) resolved out of a CNC pack to
    # `sensor_readings.ambient_temp` at 0.60 -- while production, consulting
    # the same registry, returned `ambient_temperature_c` at 1.0. Which vendor
    # answers a vendor-neutral tag should not depend on the alphabet.
    _NEUTRAL_FIRST = ("generic_iot", "generic")
    def _pack_rank(p):
        try:
            return (_NEUTRAL_FIRST.index(p.oem), p.oem)
        except ValueError:
            return (len(_NEUTRAL_FIRST), p.oem)
    # Which industry this reading came from. The caller's raw oem string is used
    # in preference to the pack, because the readings most at risk are the ones
    # whose oem we have no pack for at all -- an Allen-Bradley line resolves to
    # pack None, and only the string "rockwell" says it is not a 3D printer.
    source_domain = domain_from_oem(oem) or domain_from_pack(pack)
    refused = []
    # Gathered across every eligible pack, then settled together: a collision
    # between two VENDORS is the same problem as one inside a single pack, and
    # picking the alphabetically-first vendor is not an answer.
    cross_cands = []
    unknown_cands = []
    quantity_refused = []
    for other in sorted(packs.values(), key=_pack_rank):
        if pack and other.oem == pack.oem:
            continue
        cands = other.folded_all.get(folded)
        if not cands:
            continue
        if not _cross_pack_allowed(source_domain, other):
            # Record and keep looking: a nearer pack in the right domain may
            # still know this tag.
            refused.append((other.oem, cands[0][1], domain_from_pack(other)))
            # An UNKNOWN caller domain is missing evidence, not conflicting
            # evidence. If the tag names its own unit, that unit is evidence
            # about the reading itself and can still settle the match. A domain
            # MISMATCH is never overridden this way -- a Rockwell pump does not
            # become a 3D printer because both spell the tag "Flow".
            if source_domain is None:
                unknown_cands.extend((other, c) for c in cands)
            continue
        cross_cands.extend((other, c) for c in cands)

    if cross_cands:
        _tu = _fold_parts(tag)[1]
        _q_bad = [(o, c) for o, c in cross_cands
                  if not _quantity_ok(tag, c[1], fields, _tu)]
        cross_cands = [(o, c) for o, c in cross_cands
                       if _quantity_ok(tag, c[1], fields, _tu)]
        for _o, _c in _q_bad:
            quantity_refused.append((_o.oem, _c[1],
                                     _qty_field(_c[1], fields.get(_c[1]) or {})))
        chosen, why = _pick_by_unit(tag, [c for _, c in cross_cands])
        if chosen:
            owner = next(o for o, c in cross_cands if c is chosen)
            return {"canonical_field": chosen[1], "confidence": 0.60,
                    "match_type": "cross_oem", "layer": 1,
                    "source": f"pack:{owner.oem}", "matched_tag": chosen[0],
                    "note": f"resolved from the {owner.oem} pack, not "
                            f"{pack.oem if pack else 'any given oem'}"}
        ambiguous = ambiguous or why

    # Layer 3: signal classifier.
    guess = _signal_guess(tag, fields)
    if guess:
        canonical, conf, why = guess
        if (_signal_target_allowed(source_domain, canonical, fields)
                and _quantity_ok(tag, canonical, fields, _fold_parts(tag)[1])):
            return {"canonical_field": canonical, "confidence": conf,
                    "match_type": "signal", "layer": 3, "source": why,
                    "matched_tag": None}
        refused.append(("signal_classifier", canonical,
                        _VERTICAL_DOMAINS.get((fields.get(canonical) or {})
                                              .get("vertical"))))

    # Unit evidence is the LAST resort for an unrecognized oem: it borrows a
    # vendor's row, so it must not outrank the signal classifier, which
    # borrows from nobody. Ordering these the other way let an unknown
    # vendor's `Flow_GPM` pull `flow_rate_lpm` out of the rockwell pack when
    # the domain-free signal layer could answer it unaided.
    if unknown_cands and not _fold_parts(tag)[1]:
        # No unit to go on. If the candidate packs disagree about the canonical,
        # the honest reason is that the tag is ambiguous -- not merely that the
        # oem was unrecognized.
        _distinct = {c[1] for _, c in unknown_cands}
        if len(_distinct) > 1:
            ambiguous = ambiguous or (
                "ambiguous_fold: '%s' matches %s and the tag carries no unit to "
                "disambiguate them" % (tag, sorted(_distinct)))
    if unknown_cands and _fold_parts(tag)[1]:
        # Only with a unit token present, and only when it picks exactly one
        # answer. Confidence stays below a domain-backed match because the
        # caller's equipment is still unidentified.
        chosen, why = _pick_by_unit(tag, [c for _, c in unknown_cands],
                                    require_unit_match=True)
        if chosen:
            owner = next(o for o, c in unknown_cands if c is chosen)
            refused[:] = [r for r in refused if r[0] != owner.oem]
            return {"canonical_field": chosen[1], "confidence": 0.55,
                    "match_type": "cross_oem_unit_evidence", "layer": 1,
                    "source": f"pack:{owner.oem}", "matched_tag": chosen[0],
                    "note": f"oem '{oem}' is unrecognized, so no domain could be "
                            f"checked; the unit on '{tag}' identifies the "
                            f"quantity and settles it to the {owner.oem} row"}
        ambiguous = ambiguous or why


    # Honest miss.
    rec = {"canonical_field": None, "confidence": 0.0, "match_type": "unknown",
           "layer": None, "source": None, "matched_tag": None}
    if quantity_refused and not refused:
        _o, _c, _fq = quantity_refused[0]
        rec["match_type"] = "quantity_mismatch"
        rec["note"] = (
            "%s measures %s; '%s' in the %s pack measures %s. Different "
            "physical quantities cannot be the same reading, so the match was "
            "refused." % (tag, _qty_tag(tag, _fold_parts(tag)[1]), _c, _o, _fq))
        rec["refused_matches"] = [
            {"pack": o, "canonical_field": c, "quantity": q}
            for o, c, q in quantity_refused]
        return rec
    if ambiguous:
        rec["match_type"] = "ambiguous_fold"
        rec["note"] = ambiguous
        return rec
    if refused:
        # Do not let a domain refusal look like plain ignorance. Say that a
        # match existed and why it was not taken, so an integrator can tell a
        # missing mapping apart from a rejected one.
        oem_, canon_, dom_ = refused[0]
        rec["match_type"] = "cross_domain_refused"
        rec["note"] = (
            f"{tag} matches '{canon_}' in the {oem_} pack ({dom_}), but this "
            f"reading is {source_domain}. Cross-domain match refused; a "
            f"{source_domain} mapping for this tag is missing."
        )
        rec["refused_matches"] = [
            {"pack": o, "canonical_field": c, "domain": d} for o, c, d in refused
        ]
    return rec


def _resolve_sunspec_model(data: dict, pack, explicit=None):
    """Which SunSpec model this reading claims to be, most specific first.

    1. what the caller said
    2. the ID register in the payload, which is where a real device says it
    3. the pack's declared default

    Returns (model_id, how). `how` goes into the response so an integrator can
    see WHY a particular model's scale-factor map was applied — silently picking
    the wrong model is how a three-phase plant gets read as single phase.
    """
    if explicit is not None:
        return explicit, "caller"
    raw_id = data.get("ID")
    if raw_id is not None:
        try:
            mid = int(raw_id)
            from app.sunspec import model_spec
            if model_spec(mid) is not None:
                return mid, "id_register"
        except (TypeError, ValueError):
            pass
    if pack is not None:
        declared = getattr(pack, "sunspec_model", None)
        if declared is not None:
            return declared, "pack_default"
    return None, None


def _looks_like_sunspec(data: dict) -> bool:
    """A reading carrying scale-factor registers is a SunSpec reading.

    Deliberately narrow. The scaling stage must never fire on a device that
    merely happens to have a point called `W`: applying a scale factor that is
    not there would be as wrong as omitting one that is.
    """
    return any(str(k).endswith("_SF") for k in data)


def _type_mismatch(canonical, value, fields):
    """Reason string if `value` cannot be what `canonical` declares, else None.

    Only numeric fields are policed. State and alarm fields declare `string` or
    `enum` and legitimately carry text, so they are left alone.
    """
    if value is None:
        return None
    # A recognized non-value token ("UNAVAILABLE", "N/A", "NaN") is NOT a type
    # error -- it is the device saying it has no reading. Leave it for the
    # sentinel gate, which nulls it with the far more useful reason.
    if is_sentinel_string(value) is not None:
        return None
    declared = (fields.get(canonical) or {}).get("type")
    if declared not in ("float", "integer"):
        return None
    if isinstance(value, bool):
        return (f"type_mismatch: {canonical} is declared {declared}, received a "
                f"boolean. A true/false cannot be a measurement.")
    if isinstance(value, (int, float)):
        return None
    shown = value if isinstance(value, str) else type(value).__name__
    return (f"type_mismatch: {canonical} is declared {declared}, received "
            f"{shown!r}, which is not a number and not a recognized non-value "
            f"token. Refused rather than stored unvalidated.")


def _overflow_slot(normalized, canonical):
    """Next free `<canonical>_N` slot for the loser of a collision.

    Two tags can legitimately carry the same quantity from different sensors --
    a vendor spelling and the canonical name, two thermocouples on one spindle.
    Keeping only the winner threw a real measurement away silently; parking it
    beside the winner keeps the reading auditable without letting it pretend to
    BE the canonical value.
    """
    n = 2
    while f"{canonical}_{n}" in normalized:
        n += 1
    return f"{canonical}_{n}"


def _unconvertible_unit(tag, canonical, fields, urec):
    """Reason string if the tag names a unit we can NAME but cannot CONVERT.

    Deliberately an explicit list rather than an inference. The risk is narrow
    and specific: industrial units that are unmistakably real, carry a large
    scale factor, and have no converter -- `Flow_MGD` (million gallons/day) was
    landing its raw 4.8 in a L/min field, off by ~2,600x and reported as a clean
    100% resolution. A unit we can name but not convert is worse than one we
    never recognized, because it looks handled.

    Inferring the set instead of listing it was tried and misfired: the
    tokenizer splits `kVAR_Total` into ["k","var","total"], so a bare "var" read
    as a different unit from "kVAR" and nulled a perfectly good power reading.
    """
    if urec is not None and urec.get("converted"):
        return None
    target = (fields.get(canonical) or {}).get("unit")
    if not target:
        return None
    toks = {t for t in _tokens(tag) if len(t) > 1}
    hit = toks & _UNCONVERTIBLE_UNITS
    if not hit:
        return None
    tok = sorted(hit)[0]
    if tok == str(target).lower().replace("/", "").replace(" ", ""):
        return None
    return (f"unconvertible_unit: '{tag}' is in {tok}, {canonical} is in "
            f"{target}, and no converter exists between them. Passing the "
            f"number through unchanged would misstate it by an unknown factor.")


def normalize_row(data: dict, oem=None, sunspec_model=None):
    """Normalize one flat {raw_tag: value} reading.

    Returns (normalized, field_mappings, stats, unit_conversions, collisions,
             null_states, enum_states). SunSpec scale-factor detail rides in
             stats["sunspec"] rather than an eighth tuple slot, so every
             existing caller keeps working unchanged.
    """
    _, _, dictionary = load()
    fields = dictionary["fields"]
    pack = get_pack(oem)

    # ── STAGE 0: SunSpec scale factors ──────────────────────────────────────
    # Runs before everything, because a SunSpec register is not a measurement
    # until its scale factor is applied — `W = 5938` is 59.38 kW at W_SF=1 and
    # 5.938 kW at W_SF=0, and no amount of downstream validation can recover
    # which one was meant.
    #
    # It runs before the sentinel gate too, and that ordering is load-bearing in
    # the opposite direction from every other stage: `apply_scale_factors` does
    # its OWN type-directed not-implemented check on the raw register first, so
    # a 0xFFFF is nulled while it is still 65535 and never becomes 655.35. Doing
    # the sentinel check out here instead would not work, because whether 65535
    # is a sentinel depends on the register's declared TYPE — it is uint16 max
    # for a uint16 point and an ordinary reading for an acc32 one — and only the
    # model spec knows which. Same lesson as finding F1, one level deeper: the
    # gate has to sit where the type information is.
    sunspec_info = None
    if _looks_like_sunspec(data) or sunspec_model is not None:
        try:
            from app.sunspec import apply_scale_factors
            model_id, how = _resolve_sunspec_model(data, pack, sunspec_model)
            # The pack may know scale-factor groupings the model definition
            # cannot express for a vendor-renamed implementation.
            data, sf_records, sf_diags = apply_scale_factors(
                data, model_id,
                sf_map=getattr(pack, "sunspec_sf_map", None) if pack else None)
            sunspec_info = {
                "detected": True,
                "model_id": model_id,
                "model_source": how,
                "scale_factors_applied": sf_records,
                "diagnostics": sf_diags,
                "errors": sum(1 for d in sf_diags if d.get("severity") == "error"),
            }
        except Exception as e:                      # never take down ingest
            sunspec_info = {"detected": True, "error": f"{type(e).__name__}: {e}"}

    normalized, field_mappings, unit_conversions = {}, {}, []
    value_coercions = []
    null_states, enum_states = {}, {}
    collisions = {}
    seen_canonical = {}

    for tag, value in data.items():
        rec = resolve_field(tag, pack, dictionary, oem=oem)
        canonical = resolve_canonical(rec["canonical_field"])
        rec["canonical_field"] = canonical

        if canonical is None:
            # Lossless: an unresolved tag keeps its raw name and value so no
            # data is silently dropped. It just does not count as coverage.
            normalized[tag] = value
            field_mappings[tag] = rec
            continue

        # ── STAGE 0: repair the TYPE before anything inspects the value ────
        # Every gate below this line only understands numbers. A quoted value, a
        # decimal comma, a hex register or an OPC quality wrapper would skip the
        # sentinel check, the physics bounds AND the unit conversion, and still
        # land under a canonical name — 100% coverage over unvalidated data.
        value, _applied, _origtype = _coerce_value(value, field_hint=canonical)
        _coerced_unit = None
        if _applied:
            if _applied.startswith("extracted_unit:"):
                # The value named its own unit. That describes THIS reading, so
                # it outranks the pack's general default.
                _coerced_unit = _applied.split(":", 1)[1]
            rec["value_coercion"] = {"applied": _applied,
                                     "original_type": _origtype}
            value_coercions.append({"raw_field": tag,
                                    "canonical_field": canonical,
                                    "applied": _applied,
                                    "original_type": _origtype})

        # ── STAGE 0b: the value must be the TYPE the field declares ────────
        # Coercion repairs what it can; whatever is left has to be refused. A
        # canonical declared `float` that receives "~31" or `true` cannot be
        # bounds-checked, converted or compared, and storing it anyway hands a
        # downstream consumer a value its own schema says is impossible.
        _tm = _type_mismatch(canonical, value, fields)
        if _tm:
            null_states[canonical] = {
                "null_state": True, "null_reason": _tm,
                "raw_value": data[tag], "raw_field": tag,
                "stage": "pre_conversion",
            }
            normalized[canonical] = None
            field_mappings[tag] = rec
            seen_canonical[canonical] = (tag, rec.get("confidence") or 0.0)
            continue

        # ── STAGE 1: sentinel gate, on the RAW value, BEFORE conversion ────
        # Order is load-bearing. A sentinel is a property of the number as the
        # device put it on the wire: 65535 is uint16 max whether the register
        # holds watt-hours or millibars. Converting first LAUNDERS it —
        # 65535 Wh becomes 65.535 kWh, which no longer matches any sentinel and
        # passes every downstream check as a plausible reading. That was finding
        # F1 (2026-08-22): the validator was correct in isolation and caught the
        # value every time it was called first; only the call order defeated it.
        _sr = _validate_sentinel(canonical, value)
        if _sr["null_state"]:
            # A sentinel arriving AFTER a real reading of the same canonical
            # must not erase it. Without this the answer depended on dict
            # order: {"W": 65535, "AC_Power": 48700} resolved to 48.7 kW and
            # {"AC_Power": 48700, "W": 65535} — the same reading, keys in the
            # other order — resolved to null. Two orderings of one message
            # cannot disagree about what the machine is doing.
            if normalized.get(canonical) is not None:
                collisions.setdefault(canonical, []).append({
                    "raw_field": tag, "confidence": rec["confidence"],
                    "kept": False,
                    "superseded_because": f"null ({_sr['null_reason']})",
                })
                rec["superseded_by"] = seen_canonical[canonical][0]
                field_mappings[tag] = rec
                continue
            null_states[canonical] = {
                "null_state": True, "null_reason": _sr["null_reason"],
                "raw_value": _sr["raw_value"], "raw_field": tag,
                "stage": "pre_conversion",
            }
            normalized[canonical] = None
            field_mappings[tag] = rec
            # (tag, confidence) — the SAME order the collision reader below
            # unpacks. This slot used to store (confidence, tag), so any reading
            # where a sentinel-nulled field was followed by a second tag on the
            # same canonical unpacked prior_conf as a STRING and crashed
            # normalize with `'>' not supported between float and str`. Very
            # reachable on SunSpec: unimplemented points come across as 0xFFFF
            # and the same quantity routinely arrives under two spellings.
            seen_canonical[canonical] = (tag, rec.get("confidence") or 0.0)
            continue

        # ── STAGE 2: unit conversion ───────────────────────────────────────
        # Via the SHARED engine (app/unit_converter.py), the same module the
        # production kernel uses, so the sandbox cannot drift from prod on unit
        # behaviour. A refusal is recorded too — never silent.
        # Pack-declared wire units are a DEFAULT, consulted only when the tag
        # itself declares nothing. A tag that says `(degC)` outright is the most
        # specific evidence available — it describes THIS message, where the
        # pack describes the model in general — so an explicit tag unit always
        # wins over the pack's default.
        # Precedence: what the TAG declares, then what the VALUE carried, then
        # the pack's general default. The tag and the value are equally specific
        # about this reading, and the tag is the form the converter is built
        # around, so it stays first.
        _src_unit = None
        if _declared_unit(tag) is None:
            _src_unit = _normalize_unit_token(_coerced_unit)
        if _src_unit is None and pack and _declared_unit(tag) is None:
            _src_unit = (pack.tag_units.get(tag)
                         or pack.tag_units_folded.get(_fold(tag)))
        out_value, _urec = _convert_value(tag, value, canonical, source_unit=_src_unit)
        if _urec is not None:
            unit_conversions.append(_urec)

        # Fail CLOSED on a unit we can recognize but cannot convert. `Flow_MGD`
        # (million gallons/day) is a real flow unit the classifier knows, but no
        # converter exists for it, so the raw 4.8 was landing in a L/min field
        # untouched -- off by a factor of ~2,600 and reported as a clean 100%
        # resolution. A unit we can NAME but not CONVERT is worse than one we
        # never recognized, because it looks handled.
        _unconv = _unconvertible_unit(tag, canonical, fields, _urec)
        if _unconv and out_value is not None:
            null_states[canonical] = {
                "null_state": True, "null_reason": _unconv,
                "raw_value": value, "raw_field": tag,
                "stage": "post_conversion",
            }
            out_value = None

        # ── STAGE 3: status convergence, then physics bounds ───────────────
        # Bounds run LAST, on a value now expressed in the canonical field's own
        # unit — the only unit they are meaningful in. Checking 91.4 (degF)
        # against a Celsius field's [-40, 85] would reject a correct reading.
        _ev = _normalize_enum(canonical, out_value)
        if _ev is not None:
            enum_states[canonical] = _ev
            out_value = _ev["value"]
        _vr = _validate_bounds(canonical, out_value)
        if _vr["null_state"]:
            # Same ordering argument as the sentinel gate above: an
            # out-of-bounds register must not overwrite a good one that already
            # landed on this canonical.
            if normalized.get(canonical) is not None:
                collisions.setdefault(canonical, []).append({
                    "raw_field": tag, "confidence": rec["confidence"],
                    "kept": False,
                    "superseded_because": f"null ({_vr['null_reason']})",
                })
                rec["superseded_by"] = seen_canonical[canonical][0]
                field_mappings[tag] = rec
                continue
            null_states[canonical] = {
                "null_state": True, "null_reason": _vr["null_reason"],
                "raw_value": _vr["raw_value"], "raw_field": tag,
                "stage": "post_conversion",
            }
            out_value = None

        # Two tags landing on one canonical is a real event, not an error. Keep
        # the higher-confidence one and record what happened.
        prior = seen_canonical.get(canonical)
        if prior is not None:
            prior_tag, prior_conf = prior
            # A prior that was nulled is not evidence. It means the device said
            # "not implemented" (or shipped an out-of-range number) on THAT
            # register — it says nothing about this one. At equal confidence the
            # plain rule kept the null and discarded a perfectly good reading,
            # which on SunSpec is the common case rather than the exotic one:
            # devices answer 0xFFFF on unimplemented points while reporting the
            # same quantity for real under another spelling.
            prior_was_null = (canonical in null_states
                              and normalized.get(canonical) is None)
            keep_new = (rec["confidence"] > prior_conf
                        or (prior_was_null and out_value is not None))
            collisions.setdefault(canonical, []).append({
                "raw_field": tag, "confidence": rec["confidence"],
                "raw_value": value, "kept": keep_new,
            })
            if not keep_new:
                rec["superseded_by"] = prior_tag
                # Park the loser rather than dropping it, but only when it is a
                # real reading: a nulled sentinel is an absence, not a second
                # opinion, and giving it a slot would invent data.
                if out_value is not None:
                    slot = _overflow_slot(normalized, canonical)
                    normalized[slot] = out_value
                    rec["stored_as"] = slot
                    collisions[canonical][-1]["stored_as"] = slot
                field_mappings[tag] = rec
                continue
            collisions[canonical].append({
                "raw_field": prior_tag, "confidence": prior_conf, "kept": False,
                "raw_value": normalized.get(canonical),
                "superseded_because": ("prior reading was null" if prior_was_null
                                       else "lower confidence"),
            })
            if not prior_was_null and normalized.get(canonical) is not None:
                slot = _overflow_slot(normalized, canonical)
                normalized[slot] = normalized[canonical]
                field_mappings[prior_tag]["stored_as"] = slot
                collisions[canonical][-1]["stored_as"] = slot
            field_mappings[prior_tag]["superseded_by"] = tag
            # The null belonged to the register we just dropped, so it must not
            # keep flying the flag for a canonical that now holds a real value.
            if prior_was_null and out_value is not None:
                null_states.pop(canonical, None)

        seen_canonical[canonical] = (tag, rec["confidence"])
        normalized[canonical] = out_value
        entry = fields.get(canonical) or {}
        rec["unit"] = entry.get("unit")
        rec["vertical"] = entry.get("vertical")
        field_mappings[tag] = rec

    fields_total = len(data)
    resolved = [r for r in field_mappings.values()
                if r["match_type"] != "unknown" and r["confidence"] > 0]
    distinct = len({r["canonical_field"] for r in resolved})
    # Distinct canonicals over total tags -- the same definition production
    # uses. Counting per-tag instead would let ten spellings of one quantity
    # report as ten covered fields.
    coverage = round((distinct / fields_total) * 100, 2) if fields_total else 0.0

    stats = {
        "fields_total": fields_total,
        "fields_mapped": len(resolved),
        "fields_unknown": fields_total - len(resolved),
        "fields_distinct_canonical": distinct,
        "coverage_pct": coverage,
        "layer1_deterministic": sum(1 for r in resolved if r.get("layer") == 1),
        "layer2_identity": sum(1 for r in resolved if r.get("layer") == 2),
        "layer3_signal": sum(1 for r in resolved if r.get("layer") == 3),
    }
    if sunspec_info is not None:
        stats["sunspec"] = sunspec_info
    if value_coercions:
        stats["value_coercions"] = value_coercions
    return (normalized, field_mappings, stats, unit_conversions, collisions,
            null_states, enum_states)
