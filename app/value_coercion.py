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


__all__ = ["coerce_value", "parse_numeric_string", "locale_for_oem",
           "OEM_LOCALE", "SENTINEL_STRINGS"]


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

# "4,82e6" — one comma decimal followed by an exponent.
_SCI_COMMA = re.compile(r"^[+-]?\d+,\d+[eE][+-]?\d+$")


def coerce_value(raw_value: Any,
                 field_hint: Optional[str] = None,
                 oem: Optional[str] = None,
                 locale: Optional[str] = None) -> Tuple[Any, Optional[str], str]:
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
        return _coerce_string(raw_value, oem=oem, locale=locale)

    if isinstance(raw_value, list):
        if not raw_value:
            return None, "empty_array", "array"
        if len(raw_value) == 1:
            inner, _applied, _t = coerce_value(raw_value[0], field_hint, oem, locale)
            return inner, "array_single_element", "array"
        # A multi-sample array is a series, not a reading. Taking [0] keeps the
        # pipeline moving on the oldest sample and SAYS SO, rather than
        # collapsing a series into one number with no trace.
        inner, _applied, _t = coerce_value(raw_value[0], field_hint, oem, locale)
        return inner, "array_first_element", "array"

    if isinstance(raw_value, dict):
        # OPC UA and several historians wrap the reading with its quality flag.
        for key in ("value", "Value", "val"):
            if key in raw_value:
                inner, _applied, _t = coerce_value(raw_value[key], field_hint, oem, locale)
                return inner, f"unwrapped_object:{key}", "object"
        # No recognizable value key. Hand it back flagged: the physics gate
        # cannot check a dict, and pretending otherwise is how an object ends up
        # stored under a field declared float.
        return raw_value, "unprocessable_object", "object"

    return raw_value, "unknown_type", type(raw_value).__name__


def _coerce_string(raw: str, oem=None, locale=None) -> Tuple[Any, Optional[str], str]:
    stripped = raw.strip()

    if not stripped:
        return None, "empty_string", "string"

    # Sentinels FIRST. "NaN" and "INF" both parse as floats, so testing numeric
    # coercion first would turn a declared non-value into a non-finite number
    # and lose the reason it was never a reading.
    if stripped.upper() in SENTINEL_STRINGS:
        return stripped, "string_sentinel", "string"

    # Locale-sensitive shapes go to Babel FIRST. Python's float() happily reads
    # the German "1.842" as 1.842 when the plant means 1842 -- a 1000x error
    # that no later gate can detect, because 1.842 is a perfectly plausible
    # reading. Only strings with no separator at all skip this.
    # Scientific notation with a locale decimal comma ("4,82e6" = 4.82e6 in
    # de_DE). Babel does not parse exponents, so the decimal symbol is folded to
    # a dot and handed to float(). Guarded to the exact shape so a grouped
    # number never reaches it.
    m_sci = _SCI_COMMA.match(stripped)
    if m_sci:
        try:
            return float(stripped.replace(",", ".")), "scientific_comma_decimal", "string"
        except ValueError:
            pass

    if any(ch in stripped for ch in ",. \u00a0\u202f\u2019'") and "e" not in stripped.lower():
        val, method, _loc = parse_numeric_string(stripped, oem=oem, locale=locale)
        if val is not None:
            return val, method, "string"
        if str(method).startswith("ambiguous_number"):
            return _Ambiguous(method), "ambiguous_number", "string"

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


# ── Locale-aware numeric parsing via Babel ───────────────────────────────────
# "1.842" is 1842 on a Siemens line in Stuttgart and 1.842 on a Haas in Ohio.
# Guessing wrong is a 1000x error, and the guess is not ours to make blindly:
# the OEM tells us which convention its controller writes.
try:
    from babel.numbers import (parse_decimal, NumberFormatError,
                           get_group_symbol, get_decimal_symbol)
    from decimal import InvalidOperation
    _BABEL = True
except Exception:                                # pragma: no cover
    _BABEL = False
    class NumberFormatError(Exception): pass
    class InvalidOperation(Exception): pass

# Controller vendors write numbers in their home convention.
OEM_LOCALE = {
    "siemens": "de_DE", "krones": "de_DE", "trumpf": "de_DE", "dmg_mori": "de_DE",
    "heidenhain": "de_DE", "beckhoff": "de_DE", "wago": "de_DE", "festo": "de_DE",
    "sick": "de_DE", "endress_hauser": "de_DE", "bosch_rexroth": "de_DE",
    "baumuller": "de_DE", "baumüller": "de_DE", "weg": "pt_BR",
    "abb": "de_CH", "fronius": "de_AT", "copa_data": "de_AT", "b_and_r": "de_AT",
    "schneider": "fr_FR", "dassault": "fr_FR",
    "comau": "it_IT", "prima": "it_IT",
    "fagor": "es_ES",
    "danfoss": "da_DK", "grundfos": "da_DK",
    "abb_sweden": "sv_SE", "sandvik": "sv_SE",
    "wartsila": "fi_FI", "konecranes": "fi_FI",
    "yamazaki_mazak": "ja_JP", "fanuc": "ja_JP", "mitsubishi": "ja_JP",
    "okuma": "ja_JP",
    "doosan": "ko_KR", "hyundai_wia": "ko_KR",
}
DEFAULT_LOCALE = "en_US"


class _Ambiguous(str):
    """A number we refuse to guess at. Subclasses str so it flows through the
    existing type guard, carrying its own explanation."""
    __slots__ = ()


def locale_for_oem(oem):
    """The number convention an OEM's controller writes, or the US default."""
    if not oem:
        return DEFAULT_LOCALE
    return OEM_LOCALE.get(str(oem).strip().lower(), DEFAULT_LOCALE)


# Group separators as they arrive on the wire versus as CLDR spells them. French
# groups with a NARROW NO-BREAK SPACE (U+202F) and Swiss German with a RIGHT
# SINGLE QUOTE (U+2019), but a historian export writes a plain space and an
# ASCII apostrophe. Babel is strict and rejects both, so the real-world spelling
# is normalised to the locale's own symbol before parsing.
_GROUPISH = ("\u00a0", "\u202f", "\u2009", "\u2019", "'", " ")


def _babel_parse(text, locale):
    if not _BABEL:
        return None
    try:
        return float(parse_decimal(text, locale=locale, strict=True))
    except (NumberFormatError, InvalidOperation, ValueError, TypeError):
        pass
    # Retry with the wire spelling folded onto the locale's group symbol.
    try:
        group = get_group_symbol(locale)
        dec = get_decimal_symbol(locale)
    except Exception:
        return None
    if not any(ch in text for ch in _GROUPISH):
        return None
    folded = text
    for ch in _GROUPISH:
        if ch and ch != dec:
            folded = folded.replace(ch, group)
    try:
        return float(parse_decimal(folded, locale=locale, strict=True))
    except (NumberFormatError, InvalidOperation, ValueError, TypeError):
        return None


def parse_numeric_string(value_str, oem=None, locale=None):
    """Parse a numeric string under the OEM's locale.

    Returns (value, method, locale) or (None, reason, None).

    `strict=True` matters: without it Babel accepts "1.2.3" and silently
    returns 12.3. The ambiguity rule below is the important part -- a bare
    "1,234" means 1234 in en_US and 1.234 in de_DE, a 1000x difference, so when
    the locale is unknown AND the two readings disagree the value is REFUSED
    rather than guessed.
    """
    text = (value_str or "").strip()
    if not text:
        return None, "empty_string", None

    # An explicit locale on the request beats the OEM default: the caller knows
    # their own plant better than our vendor table does.
    loc = locale or locale_for_oem(oem)
    known_oem = bool(locale) or (bool(oem) and str(oem).strip().lower() in OEM_LOCALE)

    primary = _babel_parse(text, loc)
    if primary is not None and known_oem:
        return primary, f"babel_{loc}", loc

    # No declared locale for this OEM. Read it both ways; if the two agree the
    # string was never ambiguous and either answer is right.
    us = _babel_parse(text, DEFAULT_LOCALE)
    eu = _babel_parse(text, "de_DE")
    # Indian lakh/crore grouping (1,23,456) is neither, and unambiguous when it
    # appears -- no other locale accepts that shape.
    if us is None and eu is None:
        inr = _babel_parse(text, "en_IN")
        if inr is not None:
            return inr, "babel_en_IN", "en_IN"
    if us is not None and eu is not None and abs(us - eu) > 1e-12:
        return None, (f"ambiguous_number: '{text}' reads as {us:g} in {DEFAULT_LOCALE} "
                      f"and {eu:g} in de_DE. No locale was declared for this OEM, so "
                      f"the value is refused rather than guessed."), None
    for val, meth, lc in ((primary, f"babel_{loc}", loc),
                          (us, f"babel_{DEFAULT_LOCALE}", DEFAULT_LOCALE),
                          (eu, "babel_de_DE", "de_DE")):
        if val is not None:
            return val, meth, lc
    try:
        return float(text), "float_fallback", None    # scientific notation etc.
    except ValueError:
        return None, "not_numeric", None
