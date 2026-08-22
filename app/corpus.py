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
import os
import re
from functools import lru_cache
from app.unit_converter import (convert_value as _convert_value,
                                declared_unit as _declared_unit)
from app.value_validator import (validate_sentinel as _validate_sentinel,
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


def _fold(tag: str) -> str:
    """Collapse a raw tag to its comparison key: lowercase, unit-stripped,
    punctuation-free. "SP LOAD PCT (%)" and "sp_load_pct" both fold to
    "sploadpct"."""
    s = str(tag).strip()
    # Strip repeatedly: "TOTAL_HOURS [minutes]" -> "TOTAL_HOURS" needs one pass,
    # but "X_Motor_Temp(degC)" style stacking can need two.
    for _ in range(3):
        stripped = _UNIT_SUFFIX.sub("", s).strip()
        if stripped == s or not stripped:
            break
        s = stripped
    return _PUNCT.sub("", s.lower())


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
        # Folded index. Vendors ship the same concept under many unit-suffixed
        # spellings; if two of them disagree on the canonical, the first wins
        # deterministically (sorted) rather than by dict insertion order.
        self.folded = {}
        for tag in sorted(self.mappings):
            self.folded.setdefault(_fold(tag), (tag, self.mappings[tag]))


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

    for subj in subjects:
        for qty in quantities:
            hit = _SIGNAL_MAP.get((subj, qty))
            if hit and hit in dictionary:
                # Two named halves is a strong signal; still below an exact
                # corpus row, which is why it tops out at 0.72.
                return hit, 0.72, f"signal:{subj}+{qty}"

    for qty in quantities:
        hit = _BARE_QUANTITY.get(qty)
        if hit and hit in dictionary:
            return hit, 0.55, f"signal:{qty}"
    return None


def resolve_field(tag: str, pack, dictionary: dict) -> dict:
    """Resolve one raw tag. Always returns a mapping record -- never raises,
    never invents a name."""
    fields = dictionary["fields"]

    # Layer 1: exact corpus row.
    if pack and tag in pack.mappings:
        return {"canonical_field": pack.mappings[tag], "confidence": 1.0,
                "match_type": "corpus", "layer": 1, "source": f"pack:{pack.oem}",
                "matched_tag": tag}

    # Layer 1b: same row, once units and punctuation are folded away.
    if pack:
        hit = pack.folded.get(_fold(tag))
        if hit:
            return {"canonical_field": hit[1], "confidence": 0.95,
                    "match_type": "corpus_normalized", "layer": 1,
                    "source": f"pack:{pack.oem}", "matched_tag": hit[0]}

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
    for other in sorted(packs.values(), key=lambda p: p.oem):
        if pack and other.oem == pack.oem:
            continue
        hit = other.folded.get(folded)
        if hit:
            return {"canonical_field": hit[1], "confidence": 0.60,
                    "match_type": "cross_oem", "layer": 1,
                    "source": f"pack:{other.oem}", "matched_tag": hit[0],
                    "note": f"resolved from the {other.oem} pack, not {pack.oem if pack else 'any given oem'}"}

    # Layer 3: signal classifier.
    guess = _signal_guess(tag, fields)
    if guess:
        canonical, conf, why = guess
        return {"canonical_field": canonical, "confidence": conf,
                "match_type": "signal", "layer": 3, "source": why,
                "matched_tag": None}

    # Honest miss.
    return {"canonical_field": None, "confidence": 0.0, "match_type": "unknown",
            "layer": None, "source": None, "matched_tag": None}


def normalize_row(data: dict, oem=None):
    """Normalize one flat {raw_tag: value} reading.

    Returns (normalized, field_mappings, stats, unit_conversions, collisions).
    """
    _, _, dictionary = load()
    fields = dictionary["fields"]
    pack = get_pack(oem)

    normalized, field_mappings, unit_conversions = {}, {}, []
    null_states, enum_states = {}, {}
    collisions = {}
    seen_canonical = {}

    for tag, value in data.items():
        rec = resolve_field(tag, pack, dictionary)
        canonical = resolve_canonical(rec["canonical_field"])
        rec["canonical_field"] = canonical

        if canonical is None:
            # Lossless: an unresolved tag keeps its raw name and value so no
            # data is silently dropped. It just does not count as coverage.
            normalized[tag] = value
            field_mappings[tag] = rec
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
            null_states[canonical] = {
                "null_state": True, "null_reason": _sr["null_reason"],
                "raw_value": _sr["raw_value"], "raw_field": tag,
                "stage": "pre_conversion",
            }
            normalized[canonical] = None
            field_mappings[tag] = rec
            seen_canonical[canonical] = (rec.get("confidence") or 0.0, tag)
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
        _src_unit = None
        if pack and _declared_unit(tag) is None:
            _src_unit = (pack.tag_units.get(tag)
                         or pack.tag_units_folded.get(_fold(tag)))
        out_value, _urec = _convert_value(tag, value, canonical, source_unit=_src_unit)
        if _urec is not None:
            unit_conversions.append(_urec)

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
            keep_new = rec["confidence"] > prior_conf
            collisions.setdefault(canonical, []).append({
                "raw_field": tag, "confidence": rec["confidence"],
                "kept": keep_new,
            })
            if not keep_new:
                rec["superseded_by"] = prior_tag
                field_mappings[tag] = rec
                continue
            collisions[canonical].append({
                "raw_field": prior_tag, "confidence": prior_conf, "kept": False,
            })
            field_mappings[prior_tag]["superseded_by"] = tag

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
    return (normalized, field_mappings, stats, unit_conversions, collisions,
            null_states, enum_states)
