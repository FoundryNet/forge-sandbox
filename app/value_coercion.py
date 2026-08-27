"""Value coercion gate — industrial data types in, clean Python types out.

Runs BEFORE the normalization pipeline. The point is narrow and important: the
sentinel gate, the physics bounds and the unit converter all only look at
numbers. Anything that reaches them wearing the wrong Python type skips every
one of those checks while still landing under a canonical field name, so the
response reads "100% coverage" over data nothing validated.

That is not a hypothetical. A Kepware OPC UA export commonly quotes every value
("72.1"), a German-locale gateway writes a comma decimal ("72,1"), a Modbus
bridge emits a register as hex ("0xFFFF"), and an OPC UA client that keeps the
quality flag sends {"value": 72.1, "quality": "good"}. Each of those used to
sail straight through unvalidated.

Coercion is always REPORTED, never silent: every caller gets back what was done
so the response can show it. A reading whose type had to be repaired is a
reading an integrator should be able to see.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

# Kept in sync with value_validator.SENTINEL_STRINGS. Imported rather than
# redefined so the two can never disagree about what "no value" looks like.
try:                                             # pragma: no cover
    from app.value_validator import SENTINEL_STRINGS
except ImportError:                              # pragma: no cover
    from value_validator import SENTINEL_STRINGS


__all__ = ["coerce_value", "SENTINEL_STRINGS"]


# A number followed by a UNIT. The trailing part must actually look like a unit:
# it starts with a letter or a unit symbol and is short. Accepting "anything
# after the number" was a real defect -- "12,345,678" parsed as 12 with a unit
# of ",345,678" (a millionfold error) and "72.1.3" parsed as 72.1, silently
# discarding the rest of a malformed number.
_VALUE_WITH_UNIT = re.compile(
    r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*"
    r"([A-Za-z%\u00b0\u00b5\u03bc][A-Za-z0-9%\u00b0\u00b5\u03bc/\.\^_-]{0,11})$"
)

# 1,234,567 — grouped thousands. Common in exported reports. Parsed rather than
# rejected, but ONLY in the exact grouping shape, so "12,34" stays a decimal
# comma and "1,23,456" (which is neither) is refused.
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+$")

# "1,234" is a thousands separator in en-US and a decimal comma in de-DE. One
# comma with 1-2 trailing digits reads as a decimal; three trailing digits is a
# thousands group and is left alone rather than silently multiplied by 1000.
_COMMA_DECIMAL = re.compile(r"^[+-]?\d+,\d{1,2}$")


def coerce_value(raw_value: Any,
                 field_hint: Optional[str] = None) -> Tuple[Any, Optional[str], str]:
    """Normalize one industrial value into a clean Python type.

    Returns (coerced_value, coercion_applied, original_type). `coercion_applied`
    is None when nothing had to be done, so a caller can report only the
    readings it actually touched.
    """
    # bool BEFORE int: in Python `True` is an int, so testing numerics first
    # would label every boolean "numeric" and lose the distinction a status
    # register depends on.
    if isinstance(raw_value, bool):
        return raw_value, None, "boolean"

    if isinstance(raw_value, (int, float)):
        return raw_value, None, "numeric"

    if raw_value is None:
        return None, "null_passthrough", "null"

    if isinstance(raw_value, str):
        return _coerce_string(raw_value)

    if isinstance(raw_value, list):
        if not raw_value:
            return None, "empty_array", "array"
        if len(raw_value) == 1:
            inner, _applied, _t = coerce_value(raw_value[0], field_hint)
            return inner, "array_single_element", "array"
        # A multi-sample array is a series, not a reading. Taking [0] keeps the
        # pipeline moving on the oldest sample and SAYS SO, rather than
        # collapsing a series into one number with no trace.
        inner, _applied, _t = coerce_value(raw_value[0], field_hint)
        return inner, "array_first_element", "array"

    if isinstance(raw_value, dict):
        # OPC UA and several historians wrap the reading with its quality flag.
        for key in ("value", "Value", "val"):
            if key in raw_value:
                inner, _applied, _t = coerce_value(raw_value[key], field_hint)
                return inner, f"unwrapped_object:{key}", "object"
        # No recognizable value key. Hand it back flagged: the physics gate
        # cannot check a dict, and pretending otherwise is how an object ends up
        # stored under a field declared float.
        return raw_value, "unprocessable_object", "object"

    return raw_value, "unknown_type", type(raw_value).__name__


def _coerce_string(raw: str) -> Tuple[Any, Optional[str], str]:
    stripped = raw.strip()

    if not stripped:
        return None, "empty_string", "string"

    # Sentinels FIRST. "NaN" and "INF" both parse as floats, so testing numeric
    # coercion first would turn a declared non-value into a non-finite number
    # and lose the reason it was never a reading.
    if stripped.upper() in SENTINEL_STRINGS:
        return stripped, "string_sentinel", "string"

    # Plain numeric: "72.1", "8500", "4.82e6", "-12.4", "0"
    try:
        num = float(stripped)
    except ValueError:
        num = None
    if num is not None:
        if num != num or num in (float("inf"), float("-inf")):
            # Reachable via "1e309". Return the token the string-sentinel gate
            # already knows so it nulls with a reason instead of flowing on.
            return ("INF" if num > 0 else "-INF"), "non_finite_string", "string"
        return num, "string_to_numeric", "string"

    # Grouped thousands, checked BEFORE the decimal comma so "1,234" is 1234
    # and not 1.234. The two shapes are unambiguous: a decimal comma has 1-2
    # trailing digits, a thousands group has exactly 3.
    if _THOUSANDS.match(stripped):
        try:
            return float(stripped.replace(",", "")), "thousands_separator", "string"
        except ValueError:
            pass

    # Decimal comma, de-DE and most of continental Europe: "72,1"
    if _COMMA_DECIMAL.match(stripped):
        try:
            return float(stripped.replace(",", ".")), "comma_decimal", "string"
        except ValueError:
            pass

    # Hex register dump: "0xFF", "0xFFFF"
    if stripped[:2].lower() == "0x":
        try:
            return float(int(stripped, 16)), "hex_to_int", "string"
        except ValueError:
            pass

    # Value carrying its own unit: "72.1 degF". Checked LAST so a bare number
    # never reaches it. The unit travels in the coercion label so the caller can
    # feed it to the converter instead of guessing.
    m = _VALUE_WITH_UNIT.match(stripped)
    if m:
        try:
            return (float(m.group(1)),
                    f"extracted_unit:{m.group(2).strip()}", "string")
        except ValueError:
            pass

    # A genuine string: "RUNNING", "AUTOMATIK". Left alone for enum handling.
    return stripped, None, "string"
