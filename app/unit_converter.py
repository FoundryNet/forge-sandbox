"""Unit normalization for the Forge kernel — Option A (converge on SI).

WHY THIS EXISTS
---------------
Before this module, unit handling lived in `universal_normalize.detect_and_convert_units`
and keyed off substring tests against the canonical field name ("pressure" in cf,
cf.endswith("_kw")). That had two failure modes, both confirmed against production
on 2026-08-21:

  1. SILENT CORRUPTION. A tag declaring a non-SI unit — `COOL_TEMP (F)`,
     `Abs X [inch]`, `Feedrate_Act (m/min)` — landed unconverted in a field whose
     name implies SI. 86 °F was served as 86 into a Celsius-convention field; an
     inch position was served as-is into a millimetre-convention field. The value
     is plausible, so nothing ever looks wrong. 32 corpus fields are affected.

  2. NAME/VALUE CONTRADICTION. The LLM mapper invents canonical names that carry
     the SOURCE unit as a suffix (`condenser_pressure_psi`, `infeed_pressure_psi`).
     The substring rule ("pressure" in cf) then converted psi→bar and stored a bar
     value under a `_psi` name. 98 of 154 cached LLM mappings carry such a suffix,
     so every converter added naively promotes a latent case into an active one.

The fix is a single explicit table plus a NAME-AWARE guard: the canonical field's
own name is authoritative about what unit it holds. We never write a value into a
field whose name declares a different unit — we convert, or we flag, never both.

THE CONVENTION (Option A)
-------------------------
Each physical quantity has exactly one target unit. A field name either declares
that target explicitly (`..._c`, `..._bar`, `..._mm`) or declares nothing and is
assumed to hold the target. Cross-vendor comparison is then always valid: the same
canonical field holds the same unit regardless of OEM.

    temperature  -> C          energy      -> kWh
    pressure     -> bar        power       -> kW
    length       -> mm         flow        -> L/min
    linear speed -> mm/min     mass        -> kg
    rotational   -> rpm        torque      -> Nm
    vibration    -> mm/s       time        -> h

Nothing here mutates field NAMES; renaming is a corpus migration (see
UNIT_CONVERSION_AUDIT.md). This module makes values honest under whatever name the
resolver produced, and reports every decision it makes.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

__all__ = [
    "convert_value", "declared_unit", "field_declared_unit", "target_unit",
    "quantity_of", "TARGET_UNIT", "CONVERSIONS", "DIMENSION",
]

# ── unit vocabulary ─────────────────────────────────────────────────────────
# Every spelling observed in the 16,908-row production corpus maps to one token.
UNIT_ALIASES = {
    # temperature
    "c": "C", "°c": "C", "degc": "C", "deg_c": "C", "celsius": "C",
    "f": "F", "°f": "F", "degf": "F", "deg_f": "F", "fahrenheit": "F",
    "k": "K", "°k": "K", "kelvin": "K",
    "r": "R", "°r": "R", "rankine": "R",
    # pressure
    "bar": "bar", "mbar": "mbar", "psi": "psi", "psig": "psi", "psia": "psi",
    "kpa": "kPa", "mpa": "MPa", "pa": "Pa", "atm": "atm", "inhg": "inHg",
    "mmhg": "mmHg", "torr": "mmHg", "mm_hg": "mmHg",
    # length
    "mm": "mm", "cm": "cm", "m": "m", "in": "in", "inch": "in", "inches": "in",
    "ft": "ft", "feet": "ft", "um": "um", "µm": "um", "micron": "um",
    "mil": "mil", "mils": "mil",
    # linear speed
    "mm/min": "mm/min", "m/min": "m/min", "in/min": "in/min", "ipm": "in/min",
    "mm/s": "mm/s", "m/s": "m/s", "in/s": "in/s", "ips": "in/s",
    "ft/min": "ft/min", "fpm": "ft/min", "ft/s": "ft/s", "fps": "ft/s",
    "mph": "mph", "kph": "kph", "km/h": "kph", "kmh": "kph",
    # rotational
    "rpm": "rpm", "rev/min": "rpm", "1/min": "rpm", "hz": "Hz",
    # energy / power
    "wh": "Wh", "kwh": "kWh", "mwh": "MWh", "j": "J", "kj": "kJ",
    "w": "W", "kw": "kW", "mw": "MW", "hp": "hp",
    # volumetric flow
    "l/min": "L/min", "lpm": "L/min", "ml/min": "mL/min",
    "gpm": "gal/min", "gal/min": "gal/min", "l/s": "L/s", "m3/h": "m3/h",
    # mass flow. NASA C-MAPSS documents bleed flow as "pps", meaning lbm/s —
    # but "pps" also means parts-per-second on a packaging line, so only the
    # explicit spellings are auto-recognised. An ambiguous "pps" tag is left
    # alone and flagged rather than guessed.
    "lbm/s": "lb/s", "lb/s": "lb/s", "kg/s": "kg/s", "kg/h": "kg/h",
    # mass
    "kg": "kg", "g": "g", "lb": "lb", "lbs": "lb", "oz": "oz",
    "tonne": "tonne", "tonnes": "tonne", "ton": "ton", "tons": "ton",
    # torque / force
    "nm": "Nm", "n-m": "Nm", "n_m": "Nm", "n": "N",
    "ftlb": "ftlb", "ft-lb": "ftlb", "lbft": "ftlb",
    # vibration / acceleration
    "m/s²": "m/s2", "m/s2": "m/s2", "mm/s²": "mm/s2", "g-force": "g-force",
    # time
    "s": "s", "sec": "s", "secs": "s", "seconds": "s",
    "min": "min", "mins": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hours": "h",
    "ms": "ms", "days": "days",
    # dimensionless / electrical
    "%": "%", "pct": "%", "percent": "%",
    "v": "V", "vac": "V", "vdc": "V", "kv": "kV", "a": "A", "ma": "mA",
}

QUANTITY = {
    "C": "temperature", "F": "temperature", "K": "temperature", "R": "temperature",
    "bar": "pressure", "mbar": "pressure", "psi": "pressure", "kPa": "pressure",
    "MPa": "pressure", "Pa": "pressure", "atm": "pressure", "inHg": "pressure",
    "mmHg": "pressure",
    "mm": "length", "cm": "length", "m": "length", "in": "length",
    "ft": "length", "um": "length", "mil": "length",
    "mm/min": "linear_speed", "m/min": "linear_speed", "in/min": "linear_speed",
    "mm/s": "linear_speed", "m/s": "linear_speed", "in/s": "linear_speed",
    "ft/min": "linear_speed", "ft/s": "linear_speed",
    "mph": "vehicle_speed", "kph": "vehicle_speed",
    "rpm": "rotational", "Hz": "rotational",
    "Wh": "energy", "kWh": "energy", "MWh": "energy", "J": "energy", "kJ": "energy",
    "W": "power", "kW": "power", "MW": "power", "hp": "power",
    "L/min": "flow", "mL/min": "flow", "gal/min": "flow", "L/s": "flow",
    "m3/h": "flow",
    "lb/s": "mass_flow", "kg/s": "mass_flow", "kg/h": "mass_flow",
    "kg": "mass", "g": "mass", "lb": "mass", "oz": "mass",
    "tonne": "mass", "ton": "mass",
    "Nm": "torque", "N": "force", "ftlb": "torque",
    "m/s2": "vibration_accel", "mm/s2": "vibration_accel", "g-force": "vibration_accel",
    "s": "time", "min": "time", "h": "time", "ms": "time", "days": "time",
    "%": "percent", "V": "voltage", "kV": "voltage", "A": "current", "mA": "current",
}

# Several "quantities" above are really reporting CONVENTIONS over one physical
# dimension: feed rate, vehicle speed and vibration velocity are all length/time.
# Conversions between them are legitimate; the cross-quantity guard must compare
# dimensions, not convention labels, or it blocks m/s -> kph as a "mismatch".
DIMENSION = {
    "linear_speed": "velocity",
    "vehicle_speed": "velocity",
    "vibration_velocity": "velocity",
    "vibration_accel": "acceleration",
}

# The single target unit per quantity — the whole point of Option A.
TARGET_UNIT = {
    "temperature": "C",
    "pressure": "bar",
    "length": "mm",
    "linear_speed": "mm/min",
    "vibration_velocity": "mm/s",
    "vehicle_speed": "kph",      # road speed stays kph; mm/min is meaningless here
    "rotational": "rpm",
    "energy": "kWh",
    "power": "kW",
    "flow": "L/min",
    "mass_flow": "kg/s",
    "mass": "kg",
    "torque": "Nm",
    "vibration_accel": "mm/s2",
    "time": "h",
    "percent": "%",
    "voltage": "V",
    "current": "A",
}

# ── conversions ─────────────────────────────────────────────────────────────
CONVERSIONS = {
    # temperature
    ("F", "C"): (lambda v: (v - 32.0) * 5.0 / 9.0, "fahrenheit_to_celsius"),
    ("K", "C"): (lambda v: v - 273.15, "kelvin_to_celsius"),
    ("R", "C"): (lambda v: (v - 491.67) * 5.0 / 9.0, "rankine_to_celsius"),
    # pressure
    ("psi", "bar"): (lambda v: v * 0.0689476, "psi_to_bar"),
    ("kPa", "bar"): (lambda v: v / 100.0, "kpa_to_bar"),
    ("MPa", "bar"): (lambda v: v * 10.0, "mpa_to_bar"),
    ("Pa", "bar"): (lambda v: v / 100_000.0, "pa_to_bar"),
    ("atm", "bar"): (lambda v: v * 1.01325, "atm_to_bar"),
    ("mbar", "bar"): (lambda v: v / 1000.0, "mbar_to_bar"),
    ("inHg", "bar"): (lambda v: v * 0.0338639, "inhg_to_bar"),
    ("mmHg", "bar"): (lambda v: v * 0.00133322, "mmhg_to_bar"),
    # length
    ("in", "mm"): (lambda v: v * 25.4, "inch_to_mm"),
    ("ft", "mm"): (lambda v: v * 304.8, "foot_to_mm"),
    ("m", "mm"): (lambda v: v * 1000.0, "meter_to_mm"),
    ("cm", "mm"): (lambda v: v * 10.0, "cm_to_mm"),
    ("um", "mm"): (lambda v: v / 1000.0, "micron_to_mm"),
    ("mil", "mm"): (lambda v: v * 0.0254, "mil_to_mm"),
    # linear speed
    ("m/min", "mm/min"): (lambda v: v * 1000.0, "m_min_to_mm_min"),
    ("in/min", "mm/min"): (lambda v: v * 25.4, "in_min_to_mm_min"),
    ("m/s", "mm/min"): (lambda v: v * 60_000.0, "m_s_to_mm_min"),
    ("mm/s", "mm/min"): (lambda v: v * 60.0, "mm_s_to_mm_min"),
    ("ft/min", "mm/min"): (lambda v: v * 304.8, "ft_min_to_mm_min"),
    ("ft/s", "mm/min"): (lambda v: v * 18_288.0, "ft_s_to_mm_min"),
    # vibration velocity is reported in mm/s, not mm/min
    ("in/s", "mm/s"): (lambda v: v * 25.4, "in_s_to_mm_s"),
    ("m/s", "mm/s"): (lambda v: v * 1000.0, "m_s_to_mm_s"),
    # vehicle speed
    ("mph", "kph"): (lambda v: v * 1.609344, "mph_to_kph"),
    ("m/s", "kph"): (lambda v: v * 3.6, "m_s_to_kph"),
    # energy
    ("Wh", "kWh"): (lambda v: v / 1000.0, "watthour_to_kwh"),
    ("MWh", "kWh"): (lambda v: v * 1000.0, "megawatthour_to_kwh"),
    ("J", "kWh"): (lambda v: v / 3_600_000.0, "joule_to_kwh"),
    ("kJ", "kWh"): (lambda v: v / 3600.0, "kilojoule_to_kwh"),
    # power
    ("W", "kW"): (lambda v: v / 1000.0, "watt_to_kw"),
    ("MW", "kW"): (lambda v: v * 1000.0, "megawatt_to_kw"),
    ("hp", "kW"): (lambda v: v * 0.7457, "hp_to_kw"),
    # flow
    ("gal/min", "L/min"): (lambda v: v * 3.785412, "gpm_to_l_min"),
    ("mL/min", "L/min"): (lambda v: v / 1000.0, "ml_min_to_l_min"),
    ("L/s", "L/min"): (lambda v: v * 60.0, "l_s_to_l_min"),
    ("m3/h", "L/min"): (lambda v: v * 16.6667, "m3_h_to_l_min"),
    # mass flow
    ("lb/s", "kg/s"): (lambda v: v * 0.4535924, "lbm_s_to_kg_s"),
    ("kg/h", "kg/s"): (lambda v: v / 3600.0, "kg_h_to_kg_s"),
    # mass
    ("lb", "kg"): (lambda v: v * 0.4535924, "pound_to_kg"),
    ("oz", "kg"): (lambda v: v * 0.0283495, "ounce_to_kg"),
    ("g", "kg"): (lambda v: v / 1000.0, "gram_to_kg"),
    ("tonne", "kg"): (lambda v: v * 1000.0, "tonne_to_kg"),
    ("ton", "kg"): (lambda v: v * 907.185, "shortton_to_kg"),
    # torque
    ("ftlb", "Nm"): (lambda v: v * 1.355818, "ftlb_to_nm"),
    # vibration acceleration
    ("m/s2", "mm/s2"): (lambda v: v * 1000.0, "m_s2_to_mm_s2"),
    ("g-force", "mm/s2"): (lambda v: v * 9806.65, "g_to_mm_s2"),
    # time
    ("min", "h"): (lambda v: v / 60.0, "minutes_to_hours"),
    ("s", "h"): (lambda v: v / 3600.0, "seconds_to_hours"),
    ("ms", "h"): (lambda v: v / 3_600_000.0, "ms_to_hours"),
    ("days", "h"): (lambda v: v * 24.0, "days_to_hours"),
}

# ── parsing a unit off a name ───────────────────────────────────────────────
_PARENTHESISED = re.compile(r"[\(\[\{]\s*([^)\]\}]{1,12})\s*[\)\]\}]\s*$")
_TRAILING = re.compile(r"[_\s]([A-Za-z°µ/%²0-9\-]{1,8})$")

# A bare trailing "_s" / "_in" / "_m" / "_g" is far more often a word fragment
# ("alarms", "digital_input", "program", "config") than a unit. Underscore
# suffixes are only trusted when the token is unambiguous standing alone;
# parenthesised units — "(F)", "(in)", "(s)" — are explicit and always trusted.
_UNAMBIGUOUS_SUFFIX = {
    "mm", "cm", "um", "µm", "inch", "inches", "feet", "mil", "mils",
    "psi", "psig", "psia", "kpa", "mpa", "mbar", "bar", "inhg",
    "rpm", "hz", "kwh", "wh", "kw", "mw", "hp",
    "nm", "n-m", "n_m", "ftlb", "ft-lb", "lbft",
    "kg", "lb", "lbs", "oz", "tonne", "tonnes",
    "gpm", "lpm", "l/min", "ml/min", "l/s", "m3/h", "lbm/s", "kg/s", "kg/h",
    "mmhg", "mm_hg", "torr", "inhg",
    "pct", "percent", "hrs", "hours", "minutes", "seconds",
    "mph", "kph", "km/h", "kmh", "ipm", "fpm",
    "mm/s", "mm/min", "m/min", "in/min", "m/s2", "m/s²",
    "degc", "degf", "celsius", "fahrenheit", "kelvin", "rankine",
    "°c", "°f", "°k", "°r",
}


def declared_unit(tag: str) -> Optional[str]:
    """The unit a raw source tag declares, or None.

    `COOL_TEMP (F)` -> "F";  `energieverbrauch(Wh)` -> "Wh";  `SPINDLE_LOAD` -> None.
    """
    t = (tag or "").strip()
    if not t:
        return None
    m = _PARENTHESISED.search(t)
    if m:
        u = UNIT_ALIASES.get(m.group(1).strip().lower())
        if u:
            return u
    # Try the trailing 1..3 underscore/space segments, longest first, so that
    # `Press_mm_hg` resolves as "mm_hg" (mmHg) rather than "hg" (unknown) and
    # `flow_rate_l_min` resolves as "l_min" rather than "min" (time!).
    parts = re.split(r"[_\s]+", t.lower())
    for n in (3, 2, 1):
        if len(parts) <= n:
            continue
        tok = "_".join(parts[-n:])
        if tok in _UNAMBIGUOUS_SUFFIX:
            return UNIT_ALIASES.get(tok)
        slashed = "/".join(parts[-n:])
        if slashed in _UNAMBIGUOUS_SUFFIX:
            return UNIT_ALIASES.get(slashed)
    return None


# Longest-first so `_mm_s` wins over `_s` and `_kwh` over `_h`.
_NAME_SUFFIXES = sorted(
    [
        ("_mm_s", "mm/s"), ("_mm_min", "mm/min"), ("_m_s", "m/s"),
        ("_m_s2", "m/s2"), ("_l_min", "L/min"), ("_ml_min", "mL/min"),
        ("_kwh", "kWh"), ("_wh", "Wh"), ("_kw", "kW"), ("_mw", "MW"),
        ("_bar", "bar"), ("_mbar", "mbar"), ("_psi", "psi"),
        ("_kpa", "kPa"), ("_mpa", "MPa"), ("_pa", "Pa"),
        ("_c", "C"), ("_f", "F"), ("_k", "K"), ("_r", "R"),
        ("_celsius", "C"), ("_fahrenheit", "F"),
        ("_mm", "mm"), ("_cm", "cm"), ("_um", "um"),
        ("_inch", "in"), ("_in", "in"), ("_ft", "ft"),
        ("_rpm", "rpm"), ("_hz", "Hz"),
        ("_kg", "kg"), ("_lb", "lb"), ("_nm", "Nm"),
        ("_kmh", "kph"), ("_kph", "kph"), ("_mph", "mph"),
        ("_gpm", "gal/min"), ("_kg_s", "kg/s"), ("_lb_s", "lb/s"),
        ("_mm_hg", "mmHg"), ("_mmhg", "mmHg"),
        ("_pct", "%"), ("_percent", "%"),
        ("_minutes", "min"), ("_min", "min"),
        ("_hours", "h"), ("_hrs", "h"), ("_h", "h"),
        ("_seconds", "s"), ("_sec", "s"), ("_ms", "ms"), ("_s", "s"),
        ("_days", "days"), ("_v", "V"), ("_a", "A"),
    ],
    key=lambda p: -len(p[0]),
)


def field_declared_unit(canonical: str) -> Optional[str]:
    """The unit a canonical field NAME promises, or None.

    `condenser_pressure_psi` -> "psi";  `axes.x_position_actual` -> None.
    This is authoritative: we never store a value that contradicts it.
    """
    c = (canonical or "").lower()
    if not c:
        return None
    for suf, unit in _NAME_SUFFIXES:
        if c.endswith(suf):
            return unit
    return None


def quantity_of(unit: Optional[str]) -> Optional[str]:
    return QUANTITY.get(unit) if unit else None


def _is_vibration_field(canonical: str) -> bool:
    c = (canonical or "").lower()
    return "vibration" in c or c.endswith("_vib") or ".vib" in c


# A velocity unit (m/s, in/s, mm/s, m/min…) means different things on different
# fields, and each domain has its own reporting convention. Same dimension, four
# legitimate targets — so the FIELD decides, not the unit.
_VELOCITY_CONTEXT = (
    # (substring match on the canonical field, target unit)
    ("vibration", "mm/s"),      # ISO 10816 reports vibration velocity in mm/s
    ("tcp_speed", "mm/s"),      # robot tool-centre-point speed
    ("tcp_velocity", "mm/s"),
    ("tool_speed", "mm/s"),
    ("travel_speed", "mm/s"),
    ("ground_speed", "kph"),    # vehicle road speed
    ("vehicle_speed", "kph"),
    ("road_speed", "kph"),
    ("wheel_speed", "kph"),
    ("feed", "mm/min"),         # machining feed rate
    ("jog", "mm/min"),
    ("rapid", "mm/min"),
)


def _velocity_target(canonical: str) -> str:
    """Which velocity convention this field reports in. Defaults to the feed-rate
    convention (mm/min) because that is what the machining corpus overwhelmingly
    means by a linear speed."""
    c = (canonical or "").lower()
    for needle, unit in _VELOCITY_CONTEXT:
        if needle in c:
            return unit
    return TARGET_UNIT["linear_speed"]


def target_unit(canonical: str, source_unit: Optional[str]) -> Optional[str]:
    """The unit this field should hold.

    The field's own NAME wins when it declares a unit — a field called
    `..._psi` holds psi, full stop, even though the SI target for pressure is
    bar. Renaming such fields is a corpus migration, not something a converter
    may decide at runtime. Otherwise the SI target for the source quantity.

    One quantity is field-dependent: velocity units (in/s, m/s, mm/s) mean
    *feed rate* on a machining field and *vibration velocity* on a vibration
    field, and ISO 10816 reports the latter in mm/s while feed is conventionally
    mm/min. Same dimension, two legitimate targets — so the field decides.
    """
    named = field_declared_unit(canonical)
    if named:
        return named
    q = quantity_of(source_unit)
    if q in ("linear_speed", "vehicle_speed"):
        return _velocity_target(canonical)
    return TARGET_UNIT.get(q) if q else None


def convert_value(tag: str, value: Any, canonical: str) -> Tuple[Any, Optional[dict]]:
    """Convert `value` into the unit `canonical` is supposed to hold.

    Returns (value, record). `record` is None when nothing needed doing;
    otherwise it is a dict describing what happened, suitable for the response's
    `unit_conversions` list. A record with "converted": False is a FLAG — the
    value was left alone because converting it would have contradicted the field
    name or because no converter exists. Nothing is ever silently passed through.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value, None

    src = declared_unit(tag)
    if not src:
        return value, None

    dst = target_unit(canonical, src)
    if not dst or src == dst:
        return value, None

    src_q, dst_q = quantity_of(src), quantity_of(dst)

    # The field name declares a unit of the same quantity but a DIFFERENT one
    # than the source. Converting is correct and safe: the name is the contract.
    # (e.g. tag "(psi)" -> field "..._bar": convert psi->bar.)
    #
    # The dangerous inverse — tag "(psi)" -> field "..._psi" — is already
    # short-circuited above by src == dst.
    src_dim = DIMENSION.get(src_q, src_q)
    dst_dim = DIMENSION.get(dst_q, dst_q)
    if src_dim and dst_dim and src_dim != dst_dim:
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "flag": "unit_quantity_mismatch",
            "detail": f"tag declares {src} ({src_q}) but field implies "
                      f"{dst} ({dst_q}); refusing to convert across quantities",
        }

    conv = CONVERSIONS.get((src, dst))
    if not conv:
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "flag": "unit_unconvertible",
            "detail": f"no converter for {src} -> {dst}; value left in {src}",
        }

    fn, label = conv
    try:
        out = round(float(fn(float(value))), 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "flag": "unit_conversion_failed", "detail": f"{label} raised on {value!r}",
        }

    return out, {
        "raw_field": tag, "canonical_field": canonical,
        "from": src, "to": dst, "converted": True,
        "conversion": label, "raw_value": value, "converted_value": out,
    }
