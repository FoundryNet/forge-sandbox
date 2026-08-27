"""Output invariant checker — the relief valve.

Runs on the FINAL response, after the whole pipeline, on every field: resolved,
unresolved, coerced or passed through. It knows nothing about resolution,
channels, folds or packs. It only knows what must never be true of an answer we
hand to a caller.

The pipeline is the intended path; this is the safety net. When the pipeline is
correct this finds nothing and costs a fraction of a millisecond. When the
pipeline has a bug we have not found yet, this catches it at the door: the value
is nulled with a reason, the violation is logged for us to fix upstream, and the
caller still gets clean output.

Every correction is REPORTED, never silent. A response that had to be corrected
carries `_invariant_violations`, so a clean-looking answer can always be
distinguished from one the valve had to rescue.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List

try:                                              # pragma: no cover
    from app import field_registry as _registry
    from app.value_validator import physics_bounds_for as _bounds_for
except ImportError:                               # pragma: no cover
    import field_registry as _registry
    from value_validator import physics_bounds_for as _bounds_for

__all__ = ["check_response_invariants", "TIER_1_SENTINELS"]

log = logging.getLogger("forge.invariants")

_DICT_CACHE = None

# Integer type boundaries. No sensor reports these as a reading; they are what a
# device sends when it has nothing to send.
TIER_1_SENTINELS = {
    65535,          # uint16 max
    32767,          # int16 max
    -32768,         # int16 min
    2147483647,     # int32 max
    -2147483648,    # int32 min
    4294967295,     # uint32 max
}

_NUMERIC_TYPES = ("float", "int", "integer", "number")


def _dictionary_fields():
    """The shipped canonical dictionary, which is where `type` is declared.

    Two files describe a canonical field: the REGISTRY (bounds, units,
    quantities) and the DICTIONARY (description, type, vertical). `type` lives
    only in the dictionary, so reading the registry alone made every numeric
    contract look absent and silently disarmed invariants 2 and 2b.
    """
    global _DICT_CACHE
    if _DICT_CACHE is None:
        try:
            from app import corpus
            _DICT_CACHE = corpus.load()[2].get("fields", {})
        except Exception:                         # pragma: no cover
            _DICT_CACHE = {}
    return _DICT_CACHE


def _numeric_contract(field: str) -> bool:
    """Does the canonical schema say this field holds a number?"""
    declared = (_dictionary_fields().get(field) or {}).get("type")
    if declared:
        return declared in _NUMERIC_TYPES
    try:
        spec = _registry.field_spec(field) or {}
    except Exception:                             # pragma: no cover
        return False
    if spec.get("type"):
        return spec["type"] in _NUMERIC_TYPES
    # No declared type. A field that declares physics bounds or a unit is a
    # measurement by construction; anything else stays unclaimed.
    return bool(spec.get("physics_bounds") or spec.get("unit"))


def _bounds(field: str):
    try:
        b = _bounds_for(field)
    except Exception:                             # pragma: no cover
        return None
    if not b or len(b) != 2 or b[0] is None or b[1] is None:
        return None
    return b


def check_response_invariants(response: Dict[str, Any]) -> List[str]:
    """Check and repair a finished response. Returns the violations found.

    An empty list means the pipeline did its job. Any entry means the pipeline
    has an upstream bug AND the output was corrected before shipping.
    """
    violations: List[str] = []
    normalized = response.get("normalized")
    if not isinstance(normalized, dict):
        return violations

    def _null(field: str, reason: str, raw: Any = None) -> None:
        normalized[field] = None
        entry = {"null_state": True, "null_reason": reason,
                 "raw_field": field, "stage": "output_invariant"}
        if raw is not None:
            entry["raw_value"] = raw
        ns = response.get("null_states")
        if not isinstance(ns, dict):
            ns = {}
            response["null_states"] = ns
        ns[field] = entry

    # A field whose value was COMPUTED by a unit conversion is not carrying a
    # wire artifact, it is carrying arithmetic. 65,535,000 Wh is a real
    # cumulative energy reading and converts to exactly 65535.0 kWh -- nulling
    # that as a "sentinel" would destroy good data to enforce a rule aimed at
    # something else. Verified: this exact case occurs on a Tesla pack payload.
    converted = {c.get("canonical_field") for c in (response.get("unit_conversions") or [])
                 if isinstance(c, dict) and c.get("converted")}

    for field in list(normalized):
        value = normalized[field]

        # ── INVARIANT 7: no blank field names ──────────────────────────────
        if not field or not str(field).strip():
            violations.append("EMPTY_FIELD: blank field name in output")
            del normalized[field]
            continue

        if value is None:
            continue

        # ── INVARIANT 3: no raw objects or arrays ─────────────────────────
        # An OPC quality wrapper, a nested dict or a sample array is not a
        # reading. Nothing downstream can bound-check or convert one.
        if isinstance(value, (dict, list, tuple, set)):
            violations.append(
                f"STRUCTURE_LEAK: {field}={type(value).__name__} is a complex "
                f"type that survived the pipeline")
            _null(field, f"invariant_catch: unprocessable {type(value).__name__}")
            continue

        # ── INVARIANT 5: no NaN or Infinity ───────────────────────────────
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            violations.append(f"MATH_LEAK: {field}={value} is NaN or Infinity")
            _null(field, f"invariant_catch: {value}")
            continue

        # ── INVARIANT 2: no strings in numeric fields ─────────────────────
        if isinstance(value, str):
            if _numeric_contract(field):
                violations.append(
                    f'TYPE_LEAK: {field}="{value}" is a string in a numeric field')
                _null(field, "invariant_catch: string in numeric field", raw=value)
            # A string under an UNRESOLVED tag name has no contract to violate.
            continue

        if isinstance(value, bool):
            # bool is an int in Python; a true/false is never a measurement.
            if _numeric_contract(field):
                violations.append(
                    f"TYPE_LEAK: {field}={value} is a boolean in a numeric field")
                _null(field, "invariant_catch: boolean in numeric field", raw=value)
            continue

        if not isinstance(value, (int, float)):
            continue

        # ── INVARIANT 1: no tier-1 sentinel survives ──────────────────────
        if value in TIER_1_SENTINELS and field not in converted:
            violations.append(
                f"SENTINEL_LEAK: {field}={value} is a tier-1 sentinel that "
                f"survived the pipeline")
            _null(field, f"invariant_catch: {value} is a wire sentinel", raw=value)
            continue

        # ── INVARIANT 4: no physics impossibility ─────────────────────────
        b = _bounds(field)
        if b and not (b[0] <= value <= b[1]):
            violations.append(
                f"PHYSICS_LEAK: {field}={value} outside [{b[0]}, {b[1]}]")
            _null(field, f"invariant_catch: {value} outside [{b[0]}, {b[1]}]",
                  raw=value)
            continue

    # ── INVARIANT 6: coverage arithmetic is self-consistent ───────────────
    total = response.get("fields_total")
    mapped = response.get("fields_mapped")
    unknown = response.get("fields_unknown")
    if all(isinstance(x, int) for x in (total, mapped, unknown)):
        if total != mapped + unknown:
            violations.append(
                f"COVERAGE_MATH: total={total} != mapped={mapped} + "
                f"unknown={unknown}")

    for v in violations:
        log.warning("OUTPUT INVARIANT VIOLATION: %s", v)

    return violations
