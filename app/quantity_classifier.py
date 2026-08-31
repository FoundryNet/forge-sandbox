"""Physical-quantity classifier.

Answers one question about a tag or a canonical field: WHAT PHYSICAL QUANTITY
does it describe? Two things that measure different quantities can never be the
same reading, whatever their names fold to.

This is the structural half of cross-vertical protection. The domain guard asks
"is this the same KIND OF MACHINE"; this asks "is this the same KIND OF
MEASUREMENT". They fail differently and catch different bugs:

    Flow_GPM  -> volumetric_flow_rate
    filament_flow_rate -> mass_flow_rate      => rejected on QUANTITY,
                                                 with or without a domain

    Total_kW  -> electric_power
    operating_hours -> time_duration          => rejected on QUANTITY,
                                                 even though both fold to "total"

Quantity is read from three signals, most specific first:
  1. the unit token stripped off the tag name        (GPM -> volumetric_flow_rate)
  2. keyword tokens in the name                      (Temp -> temperature)
  3. for a canonical field, what the schema declares

DELIBERATELY CONSERVATIVE: when either side cannot be classified the pair is
allowed through. An unknown quantity is missing evidence, not contrary evidence,
and blocking on ignorance would refuse most of the corpus -- 215 of 395 fields
declare no quantity at all.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["classify_quantity", "quantity_from_unit", "quantity_from_tag",
           "same_physical_quantity", "dimensional_conflict",
           "get_dimensionality", "tag_unit_to_pint", "verify_conversion", "quantity_from_field",
           "compatible", "resolve_fold_ambiguity",
           "UNIT_TO_QUANTITY", "KEYWORD_TO_QUANTITY"]


UNIT_TO_QUANTITY = {
    # volumetric flow
    "gpm": "volumetric_flow_rate", "lpm": "volumetric_flow_rate",
    "lps": "volumetric_flow_rate", "m3h": "volumetric_flow_rate",
    "m3/h": "volumetric_flow_rate", "cfm": "volumetric_flow_rate",
    "l/min": "volumetric_flow_rate", "gal/min": "volumetric_flow_rate",
    "mgd": "volumetric_flow_rate", "bopd": "volumetric_flow_rate",
    "mcfd": "volumetric_flow_rate",
    # mass flow
    "kg/s": "mass_flow_rate", "g/s": "mass_flow_rate", "lb/h": "mass_flow_rate",
    "kg/h": "mass_flow_rate", "g/min": "mass_flow_rate",
    # temperature
    "degf": "temperature", "degc": "temperature", "f": "temperature",
    "c": "temperature", "k": "temperature", "celsius": "temperature",
    "fahrenheit": "temperature",
    # pressure
    "psi": "pressure", "bar": "pressure", "kpa": "pressure",
    "mbar": "pressure", "mmhg": "pressure", "atm": "pressure",
    "pa": "pressure", "inhg": "pressure",
    # power
    "kw": "electric_power", "w": "electric_power", "mw": "electric_power",
    "hp": "electric_power",
    # Apparent power (VA) is not active power (W). Kept distinct so
    # `apparent_power_va` and `active_power_kw` cannot be confused for one
    # another, and parented to electric_power in _PARENT so nothing that
    # already declares electric_power stops accepting these units.
    "va": "apparent_power", "kva": "apparent_power", "mva": "apparent_power",
    "kvar": "electric_power",
    "mvar": "electric_power", "var": "electric_power", "mw": "electric_power",
    # energy
    "kwh": "electric_energy", "wh": "electric_energy", "mwh": "electric_energy",
    "j": "electric_energy", "kj": "electric_energy", "btu": "electric_energy",
    "therm": "electric_energy",
    # rotational speed. The per-minute spellings matter: a naive reading of
    # "ACT_SP_SPEED_rev/min" sees a trailing "min" and calls a spindle speed a
    # DURATION, which is exactly the kind of false positive that would make this
    # classifier refuse real corpus rows.
    "rpm": "rotational_speed", "revmin": "rotational_speed",
    "rev/min": "rotational_speed", "1/min": "rotational_speed",
    "1min": "rotational_speed", "min1": "rotational_speed",
    "min-1": "rotational_speed", "umin": "rotational_speed",
    "u/min": "rotational_speed", "rs": "rotational_speed",
    # current / voltage
    "a": "electric_current", "ma": "electric_current",
    "amp": "electric_current", "amps": "electric_current",
    "v": "electric_voltage", "kv": "electric_voltage", "mv": "electric_voltage",
    # frequency
    "hz": "frequency", "khz": "frequency",
    # concentration / water quality
    "ppm": "concentration", "ppb": "concentration",
    "mg/l": "concentration", "mg_l": "concentration", "ugl": "concentration",
    "ntu": "turbidity",
    "us": "conductivity", "uscm": "conductivity", "us/cm": "conductivity",
    # irradiance
    "w/m2": "irradiance", "w_m2": "irradiance", "wm2": "irradiance",
    # linear speed / distance / mass / torque / time / ratio
    "mph": "linear_speed", "m/s": "linear_speed", "mps": "linear_speed",
    "km/h": "linear_speed", "kmh": "linear_speed",
    "mm": "length", "m": "length", "in": "length", "ft": "length",
    "kg": "mass", "t": "mass", "tonnes": "mass", "lb": "mass", "lbs": "mass",
    "nm": "torque", "ftlb": "torque",
    "h": "time_duration", "hr": "time_duration", "hrs": "time_duration",
    "hours": "time_duration", "min": "time_duration", "s": "time_duration",
    "sec": "time_duration", "ms": "time_duration",
    "msec": "time_duration", "us": "time_duration",
    "pct": "ratio", "%": "ratio", "percent": "ratio",
}

KEYWORD_TO_QUANTITY = {
    "temp": "temperature", "temperature": "temperature", "tmp": "temperature",
    "flow": "flow_rate",            # generic: the unit decides volumetric vs mass
    "pressure": "pressure", "press": "pressure",
    "power": "electric_power", "kw": "electric_power",
    "energy": "electric_energy", "kwh": "electric_energy",
    "current": "electric_current", "amps": "electric_current",
    "voltage": "electric_voltage", "volt": "electric_voltage",
    "speed": None, "rpm": "rotational_speed",
    "torque": "torque",
    "level": "level",
    "humidity": "humidity",
    "hours": "time_duration", "runtime": "time_duration",
    "time": "time_duration", "duration": "time_duration",
    "elapsed": "time_duration",
    "turbidity": "turbidity", "conductivity": "conductivity",
    "frequency": "frequency", "freq": "frequency",
}

# The schema's own `quantity` vocabulary drives PHYSICS_BOUNDS_BY_QUANTITY and
# must not be renamed, so it is translated here instead.
_SCHEMA_QUANTITY = {
    "temperature": "temperature", "pressure": "pressure",
    "power": "electric_power", "energy": "electric_energy",
    "current": "electric_current", "voltage": "electric_voltage",
    "rotational": "rotational_speed", "time": "time_duration",
    "flow": "flow_rate", "mass_flow": "mass_flow_rate",
    "mass": "mass", "torque": "torque", "length": "length",
    "linear_speed": "linear_speed", "vehicle_speed": "linear_speed",
    "percent": "ratio",
}

# A generic parent is compatible with its specialisations: a tag that says only
# "Flow" has not claimed volumetric OR mass, so it must not be rejected against
# either. A tag that says GPM has.
_PARENT = {
    "apparent_power": "electric_power",
    "volumetric_flow_rate": "flow_rate",
    "mass_flow_rate": "flow_rate",
}

_PUNCT = re.compile(r"[^a-z0-9]+")


def _norm(tok: Optional[str]) -> Optional[str]:
    if not tok:
        return None
    t = str(tok).strip().lower()
    if t in UNIT_TO_QUANTITY:
        return t
    stripped = _PUNCT.sub("", t)
    if stripped.startswith("deg") and len(stripped) > 3:
        stripped = stripped[3:]
    return stripped or None


def quantity_from_unit(unit: Optional[str]) -> Optional[str]:
    """The quantity a unit implies, or None."""
    if not unit:
        return None
    raw = str(unit).strip().lower()
    if raw in UNIT_TO_QUANTITY:
        return UNIT_TO_QUANTITY[raw]
    return UNIT_TO_QUANTITY.get(_norm(raw))


def _tokens(name: str):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(name))
    return [t for t in _PUNCT.split(s.lower()) if t]


_PH_TAG = re.compile(r"^ph(value|_?val|reading)?$")


def quantity_from_tag(tag: str, units=None) -> Optional[str]:
    """Classify a RAW vendor tag.

    The unit wins over the keyword: `Flow_GPM` is volumetric because GPM says
    so, where `Flow` alone stays the generic `flow_rate`.
    """
    for u in (units or []):
        q = quantity_from_unit(u)
        if q:
            return q
    toks = _tokens(tag)
    # ONLY the final token may be read as a unit. A unit lives at the END of a
    # tag name; anywhere else it is part of the name. `InBatV` (internal battery
    # voltage) begins with "in", which is not inches, and `PhVphA` is a phase
    # voltage, not a pH reading.
    for t in toks[-1:]:
        # A ONE-LETTER token is not evidence of a unit. Vendor tags are full of
        # them as name fragments -- `Van`/`Vab` are phase voltages, not amps;
        # a trailing `F` is usually a phase or a field letter, not Fahrenheit.
        # The corpus folder learned the same lesson ("program" -> "progr"), and
        # reading them as units here produced most of this classifier's false
        # positives against real corpus rows.
        if len(t) < 2:
            continue
        q = UNIT_TO_QUANTITY.get(t)
        if q:
            return q
    # Keywords scanned from the END. English tag names put the head noun last:
    # "Power On Time" is a TIME, not a power, and reading left-to-right made the
    # leading "Power" win and mislabelled a machine's runtime counter.
    for t in reversed(toks):
        q = KEYWORD_TO_QUANTITY.get(t)
        if q:
            return q
    # pH is two letters that appear inside plenty of electrical tag names
    # (`PhV`, `PhVphA` are phase voltages), so it only counts when the whole tag
    # is the pH reading.
    if _PH_TAG.match(_PUNCT.sub("", str(tag).lower())):
        return "acidity"
    return None


def quantity_from_field(field: str, spec: Optional[dict] = None) -> Optional[str]:
    """Classify a CANONICAL field: what the schema declares, else its name/unit."""
    spec = spec or {}
    explicit = spec.get("physical_quantity")
    if explicit:
        return explicit
    mapped = _SCHEMA_QUANTITY.get(spec.get("quantity"))
    if mapped:
        # A declared `flow` plus a volumetric unit is volumetric, not generic.
        if mapped == "flow_rate":
            byunit = quantity_from_unit(spec.get("unit"))
            if byunit in ("volumetric_flow_rate", "mass_flow_rate"):
                return byunit
        return mapped
    byunit = quantity_from_unit(spec.get("unit"))
    if byunit:
        return byunit
    return quantity_from_tag(field)


# A percentage is dimensionless: fan speed, humidity, load and level are all
# legitimately expressed as one. So `ratio` is compatible with everything --
# refusing `Humidity -> relative_humidity_pct` would be the classifier being
# clever rather than correct.
_DIMENSIONLESS = {"ratio"}


def compatible(a: Optional[str], b: Optional[str]) -> bool:
    """Can a tag of quantity `a` legitimately be field of quantity `b`?

    True when either is unknown (missing evidence is not contrary evidence),
    when they are equal, or when one is the generic parent of the other.
    """
    if not a or not b:
        return True
    if a == b:
        return True
    if a in _DIMENSIONLESS or b in _DIMENSIONLESS:
        return True
    return _PARENT.get(a) == b or _PARENT.get(b) == a


def resolve_fold_ambiguity(raw_tag, candidates, tag_units=None, field_spec=None):
    """Pick the candidate whose quantity agrees with the tag's.

    `candidates` is [(raw, canonical, units)]. `field_spec` maps a canonical to
    its schema entry. Returns (candidate | None, reason | None).
    """
    tag_q = quantity_from_tag(raw_tag, tag_units)
    if not tag_q:
        return None, None                        # nothing to decide with
    spec_of = field_spec or (lambda f: {})
    keep, rejected = [], []
    for c in candidates:
        fq = quantity_from_field(c[1], spec_of(c[1]))
        (keep if compatible(tag_q, fq) else rejected).append((c, fq))
    if len({c[1] for c, _ in keep}) == 1:
        return keep[0][0], None
    if not keep and rejected:
        return None, ("quantity_mismatch: '%s' measures %s; %s"
                      % (raw_tag, tag_q,
                         ", ".join(f"{c[1]} measures {fq}" for c, fq in rejected)))
    return None, None


def classify_quantity(raw_tag: str, units=None) -> Optional[str]:
    """Public entry point: the physical quantity a raw vendor tag describes."""
    return quantity_from_tag(raw_tag, units)


# ── Dimensional analysis via Pint ────────────────────────────────────────────
# Pint proves dimensional equality mathematically instead of by lookup, and it
# catches unit pairs no hand-written table would think to list.
#
# It is NOT sufficient on its own, and that matters. Dimensionality cannot see
# semantics:
#
#     rpm      vs Hz     -> both 1/[time]                    dimensionally EQUAL
#     N*m      vs J      -> both [mass][length]^2/[time]^2    dimensionally EQUAL
#     ppm      vs percent-> both dimensionless                dimensionally EQUAL
#
# A spindle speed is not a grid frequency, a torque is not an energy, and
# reading ppm as percent is a 10,000x error. So Pint is wired as an ADDITIONAL
# VETO on top of the semantic map, never as a replacement: a pair must satisfy
# BOTH to resolve. Pint contributes proof the map cannot; the map contributes
# meaning Pint cannot.
try:
    import pint as _pint
    _UREG = _pint.UnitRegistry()
except Exception:                                # pragma: no cover
    _pint = None
    _UREG = None

# Our tag tokens are not Pint's spellings. Thin translation layer.
TAG_UNIT_TO_PINT = {
    "degf": "degF", "degc": "degC", "f": "degF", "c": "degC", "k": "kelvin",
    "r": "degR", "celsius": "degC", "fahrenheit": "degF",
    "psi": "psi", "bar": "bar", "kpa": "kPa", "mpa": "MPa", "pa": "Pa",
    "mbar": "mbar", "mmhg": "mmHg", "atm": "atm", "inhg": "inHg",
    "kw": "kilowatt", "w": "watt", "mw": "megawatt", "hp": "horsepower",
    "va": "volt_ampere", "kva": "kilovolt_ampere", "mva": "megavolt_ampere",
    "var": "volt_ampere", "kvar": "kilovolt_ampere", "mvar": "megavolt_ampere",
    "kwh": "kilowatt_hour", "wh": "watt_hour", "mwh": "megawatt_hour",
    "j": "joule", "kj": "kilojoule", "btu": "BTU",
    "gpm": "gallon / minute", "lpm": "liter / minute", "l/min": "liter / minute",
    "lps": "liter / second", "l/s": "liter / second",
    "m3/h": "meter ** 3 / hour", "m3h": "meter ** 3 / hour",
    "cfm": "foot ** 3 / minute", "ml/min": "milliliter / minute",
    "gal/min": "gallon / minute",
    "kg/s": "kilogram / second", "kg/h": "kilogram / hour",
    "lb/s": "pound / second", "g/s": "gram / second",
    "rpm": "revolutions_per_minute", "hz": "hertz", "khz": "kilohertz",
    "mph": "mph", "kph": "kph", "m/s": "meter / second",
    "mm/s": "mm / second", "mm/min": "mm / minute", "ft/min": "foot / minute",
    "a": "ampere", "amp": "ampere", "amps": "ampere", "ma": "milliampere",
    "v": "volt", "kv": "kilovolt", "mv": "millivolt",
    "mm": "mm", "cm": "cm", "m": "meter", "in": "inch", "inch": "inch",
    "ft": "foot", "um": "micrometer", "mil": "thou",
    "kg": "kilogram", "g": "gram", "lb": "pound", "lbs": "pound",
    "t": "metric_ton", "tonne": "metric_ton", "ton": "short_ton", "oz": "ounce",
    "nm": "newton * meter", "ftlb": "foot_pound",
    "h": "hour", "hr": "hour", "hrs": "hour", "hours": "hour",
    "min": "minute", "s": "second", "sec": "second", "ms": "millisecond",
    "days": "day", "day": "day",
    "w/m2": "watt / meter ** 2", "w_m2": "watt / meter ** 2",
    "kw/m2": "kilowatt / meter ** 2",
    "pct": "percent", "%": "percent", "percent": "percent", "ppm": "ppm",
    "ratio": "dimensionless", "dimensionless": "dimensionless",
    # compound rate and acceleration units the corpus converts between
    "m/min": "meter / minute", "in/min": "inch / minute",
    "in/s": "inch / second", "ft/s": "foot / second",
    "m/s2": "meter / second ** 2", "mm/s2": "mm / second ** 2",
    "g-force": "standard_gravity", "gforce": "standard_gravity",
    "mm/min": "mm / minute", "l/s": "liter / second", "short_ton": "short_ton",
}

_dimension_cache = {}


def tag_unit_to_pint(tag_unit):
    """Our tag token -> a string Pint can parse, or None."""
    if not tag_unit:
        return None
    return TAG_UNIT_TO_PINT.get(str(tag_unit).strip().lower())


def get_dimensionality(unit_str):
    """Pint dimensionality for a unit token, or None if it cannot be placed.

    Cached: parsing is the expensive part and the token set is tiny.
    """
    if not unit_str or _UREG is None:
        return None
    key = str(unit_str).strip().lower()
    if key in _dimension_cache:
        return _dimension_cache[key]
    pint_str = tag_unit_to_pint(key) or key
    try:
        dim = _UREG.parse_units(pint_str).dimensionality
    except Exception:
        dim = None
    _dimension_cache[key] = dim
    return dim


def same_physical_quantity(unit_a, unit_b):
    """Do two units share a dimensionality?

    True / False when both are known. None when either cannot be placed --
    the caller decides what to do with "unknown", because refusing on an
    unparseable unit would reject most of a real corpus.
    """
    da, db = get_dimensionality(unit_a), get_dimensionality(unit_b)
    if da is None or db is None:
        return None
    return da == db


def dimensional_conflict(unit_a, unit_b) -> bool:
    """True only when Pint can place BOTH units and they disagree.

    This is the veto: a positive answer is mathematical proof that two readings
    cannot be the same measurement.
    """
    return same_physical_quantity(unit_a, unit_b) is False


def verify_conversion(value, from_unit, to_unit, our_result, tol=1e-6):
    """Cross-check one conversion against Pint. Returns (ok, pint_value, note)."""
    if _UREG is None:
        return True, None, "pint unavailable"
    pf, pt = tag_unit_to_pint(from_unit), tag_unit_to_pint(to_unit)
    if not pf or not pt:
        return True, None, f"no pint mapping for {from_unit if not pf else to_unit}"
    try:
        pv = _UREG.Quantity(value, pf).to(pt).magnitude
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    rel = abs(our_result - pv) / max(abs(pv), 1e-12)
    return rel <= tol, pv, f"rel={rel:.2e}"
