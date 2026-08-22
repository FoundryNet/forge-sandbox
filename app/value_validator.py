"""Sentinel, null-state and physics validation for normalized values.

WHY THIS EXISTS
---------------
Confirmed against live production on 2026-08-21: every one of these was served as
a real coolant temperature, with no flag and no warning.

    "UNAVAILABLE" -> 'UNAVAILABLE'      65535 -> 65535       9999 -> 9999
    "NaN"         -> 'NaN'              32767 -> 32767       -1   -> -1
    "N/A"         -> 'N/A'              ""    -> ''          null -> None

A 9999 in a Celsius field reads as a catastrophic overheat. An agent acting on it
escalates a machine that is merely reporting "sensor offline". MTConnect's
UNAVAILABLE *was* handled — but only inside `kernel/adapters/mtconnect.py`, which
anything arriving through `/v1/normalize` bypasses entirely.

    The agent should see `null` plus a reason, never a fake number.

THE TWO TIERS
-------------
Not every suspicious number is a sentinel, and over-rejecting destroys real data:

  ALWAYS      Integer type boundaries (65535, 32767, -32768, 2147483647,
              4294967295) are artifacts of the wire encoding, never physical
              readings. Strings, None, NaN and Inf likewise.

  CONTRACTUAL 9999, -9999, 99999, 999, -999, -1 are *conventional* sentinels but
              also perfectly ordinary values. -1 is a real temperature. 999 is a
              real spindle speed. 9999 is a real part count. These are flagged
              ONLY when they fall outside the field's declared physics bounds, so
              a field with no contract never loses data to a guess.

That distinction is the difference between catching corruption and causing it.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from app import field_registry as _registry

__all__ = [
    "validate_value", "is_sentinel_string", "SENTINEL_STRINGS",
    "ALWAYS_SENTINEL_NUMBERS", "CONTRACTUAL_SENTINEL_NUMBERS",
    "PHYSICS_BOUNDS_BY_QUANTITY", "physics_bounds_for",
]

# ── string non-values ───────────────────────────────────────────────────────
SENTINEL_STRINGS = frozenset({
    "UNAVAILABLE",                      # MTConnect standard
    "NAN", "NA", "N/A", "NULL", "NONE", "NIL",
    "ERROR", "FAULT", "INVALID", "BAD", "FAILED",
    "---", "--", "-", "?", "??",
    "",                                 # empty / whitespace-only after strip
})

# ── numeric sentinels, tier 1: wire-encoding artifacts ─────────────────────
# These are integer type boundaries. No sensor reports 4294967295 °C.
ALWAYS_SENTINEL_NUMBERS = frozenset({
    65535,          # 0xFFFF   uint16 max
    -32768,         # 0x8000   int16 min
    32767,          # 0x7FFF   int16 max
    4294967295,     # 0xFFFFFFFF uint32 max
    2147483647,     # 0x7FFFFFFF int32 max
    -2147483648,    # int32 min
})

# ── numeric sentinels, tier 2: conventional but ambiguous ──────────────────
# Flagged ONLY when they violate the field's physics bounds. Blanket-rejecting
# these would null out real readings: -1 °C, 999 rpm, 9999 parts are all normal.
CONTRACTUAL_SENTINEL_NUMBERS = frozenset({
    9999, -9999, 99999, -99999, 999, -999, -1,
})

# ── physics bounds by quantity ─────────────────────────────────────────────
# Deliberately generous: this catches the physically impossible, not the
# improbable. A field's own `valid_range` (from canonical_schema_v2) wins when
# present.
PHYSICS_BOUNDS_BY_QUANTITY = {
    "temperature":      (-273.15, 3000.0),     # absolute zero .. plasma cutting
    "pressure":         (0.0, 1500.0),         # absolute pressure is non-negative
    "percent":          (0.0, 100.0),
    "rotational":       (0.0, 500000.0),       # see NOTE on reverse spindle
    "length":           (-100000.0, 100000.0), # positions are signed
    "linear_speed":     (-1e7, 1e7),
    "vehicle_speed":    (-500.0, 1000.0),
    "energy":           (0.0, 1e12),           # cumulative energy never decreases
    "power":            (-1e6, 1e6),           # signed: regen / export is real
    "flow":             (0.0, 1e6),
    "mass_flow":        (0.0, 1e5),
    "mass":             (0.0, 1e7),
    "torque":           (-1e6, 1e6),           # signed: direction is real
    "vibration_accel":  (0.0, 1e7),
    "vibration_velocity": (0.0, 1e6),
    "time":             (0.0, 1e9),
    "voltage":          (-1e5, 1e5),
    "current":          (-1e5, 1e5),
}

# NOTE on rotational: an M04 reverse spindle legitimately reports negative rpm on
# some controllers. The floor is 0 here because that is the requested contract;
# any OEM that reports signed rpm needs a per-field override in
# canonical_fields.json rather than a change to this default.

# Quantities where a negative value is physically meaningless regardless of the
# declared bounds — an RMS or a magnitude cannot be below zero.
_NON_NEGATIVE_NAME_MARKERS = ("_rms", "rms_", ".rms", "_magnitude", "_abs")


def is_sentinel_string(value: Any) -> Optional[str]:
    """Return the normalized sentinel token if `value` is a string non-value."""
    if not isinstance(value, str):
        return None
    token = value.strip().upper()
    if token in SENTINEL_STRINGS:
        return token or "<empty>"
    return None


def physics_bounds_for(field: str) -> Optional[tuple]:
    """Bounds for a canonical field: its own declared range first, else the
    default for its quantity."""
    spec = _registry.field_spec(field)
    pb = spec.get("physics_bounds")
    if isinstance(pb, dict) and "min" in pb and "max" in pb:
        return (pb["min"], pb["max"])
    vr = _registry.valid_range(field)
    if vr:
        return vr
    q = spec.get("quantity")
    return PHYSICS_BOUNDS_BY_QUANTITY.get(q) if q else None


def _null(raw, reason: str) -> dict:
    return {"value": None, "null_state": True, "null_reason": reason,
            "raw_value": raw}


def validate_value(field: str, value: Any,
                   field_contract: Optional[dict] = None) -> dict:
    """Validate one normalized value.

    Returns {"value": <value>, "null_state": False} when the reading is real, or
    a null descriptor {"value": None, "null_state": True, "null_reason": ...,
    "raw_value": ...} when it is not. A sentinel is NEVER passed through as a
    real reading.
    """
    spec = field_contract if field_contract is not None else _registry.field_spec(field)

    # ── missing / null ──────────────────────────────────────────────────────
    if value is None:
        return _null(None, "missing: value was null")

    # ── string non-values ───────────────────────────────────────────────────
    if isinstance(value, str):
        token = is_sentinel_string(value)
        if token is not None:
            return _null(value, f"string_sentinel: '{value}'")
        # A numeric-looking string is NOT coerced here. Locale parsing is a
        # separate concern (a German "1.234" means 1234, not 1.234) and guessing
        # would be exactly the silent corruption this module exists to stop.
        return {"value": value, "null_state": False}

    # ── booleans are legitimate values, and bool is a subclass of int ───────
    if isinstance(value, bool):
        return {"value": value, "null_state": False}

    if not isinstance(value, (int, float)):
        return {"value": value, "null_state": False}

    # ── NaN / Inf ───────────────────────────────────────────────────────────
    if isinstance(value, float):
        if math.isnan(value):
            return _null(value, "numeric_sentinel: NaN")
        if math.isinf(value):
            return _null(value, f"numeric_sentinel: {'+' if value > 0 else '-'}Inf")

    bounds = None
    if spec:
        pb = spec.get("physics_bounds")
        if isinstance(pb, dict) and "min" in pb and "max" in pb:
            bounds = (pb["min"], pb["max"])
    if bounds is None:
        bounds = physics_bounds_for(field)

    # ── tier 1: wire-encoding artifacts, always a sentinel ─────────────────
    if value in ALWAYS_SENTINEL_NUMBERS:
        return _null(value, f"numeric_sentinel: {value} (integer type boundary)")

    # ── negative RMS / magnitude — mathematically impossible ───────────────
    low = (field or "").lower()
    if value < 0 and any(m in low for m in _NON_NEGATIVE_NAME_MARKERS):
        return _null(value, f"negative_rms: {value}")

    # ── physics bounds ─────────────────────────────────────────────────────
    if bounds:
        lo, hi = bounds
        if value < lo or value > hi:
            return _null(value,
                         f"physics_violation: {value} outside [{lo}, {hi}]")

    # ── tier 2: conventional sentinels, only when unbounded-implausible ────
    # Already inside the physics envelope at this point, so a bounded field has
    # cleared them as real. Only flag when the field declares NO bounds and the
    # value is one of the conventional error codes.
    if bounds is None and value in CONTRACTUAL_SENTINEL_NUMBERS:
        return _null(value,
                     f"numeric_sentinel: {value} (conventional error code; "
                     f"field declares no physics bounds to disambiguate)")

    return {"value": value, "null_state": False}


# ── enum / status convergence ───────────────────────────────────────────────
# Haas says READY, Siemens says AUTOMATIK, Fanuc says IDLE, and a Modbus
# controller says 1. Before this, all four passed through raw, so any
# fleet_health count of "running machines" compared vendor dialects to each
# other and got a different answer per OEM. The canonical value is what gets
# counted; the vendor's own string is preserved alongside it.

def normalize_enum_value(field: str, value: Any,
                         field_contract: Optional[dict] = None) -> Optional[dict]:
    """Map a vendor status value onto the field's canonical enum.

    Returns None when the field has no enum contract (nothing to do), otherwise
    {"value", "raw_value", "matched"}. An unrecognised value becomes "unknown"
    with matched=False rather than being invented into a state.
    """
    spec = field_contract if field_contract is not None else _registry.field_spec(field)
    if not spec or spec.get("type") != "enum":
        return None
    mappings = spec.get("mappings") or {}
    canon_values = spec.get("canonical_values") or []
    if value is None:
        return {"value": "unknown", "raw_value": value, "matched": False}

    token = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    hit = mappings.get(token)
    if hit is None and token.lower() in [c.lower() for c in canon_values]:
        hit = token.lower()          # already canonical
    if hit is None:
        return {"value": "unknown", "raw_value": value, "matched": False}
    return {"value": hit, "raw_value": value, "matched": True}
