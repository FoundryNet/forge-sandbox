"""SunSpec scale-factor processing.

THE PROBLEM
-----------
SunSpec transmits measurements as integers with a *separate* scale-factor
register. The register pair is meaningless apart:

    40083  W      = 48700
    40084  W_SF   = -2
    real power    = 48700 x 10^-2 = 487.00 W

Read `W` alone and you report 48,700 W for a 487 W inverter — off by 100x. The
error is silent, plausible-looking, and survives every downstream check that
does not know about the second register. This is the single largest source of
error in solar SCADA integration; REIG reports a Power Electronics SF
discrepancy costing four days of remediation on a 47 MW project.

WHERE THIS SITS IN THE PIPELINE
-------------------------------
Order is load-bearing, for exactly the reason finding F1 documented on
2026-08-22 (`app/corpus.py`, sentinel-before-conversion):

    1. sentinel gate      on the RAW register value
    2. scale factor       <-- THIS MODULE
    3. unit conversion    W -> kW, Wh -> kWh
    4. physics bounds     on the converted value

Applying the scale factor before the sentinel gate would launder the sentinel
exactly as unit conversion did: uint16 "not implemented" is 0xFFFF = 65535, and
65535 x 10^-2 = 655.35, which is not a sentinel, is inside every plausible
bound, and reads as a perfectly ordinary measurement. Same bug, different
multiplier. Scaling therefore runs strictly AFTER the sentinel gate and strictly
BEFORE unit conversion — the value must be a true quantity in the register's
declared unit before anything converts that unit.

WHAT COUNTS AS AUTHORITY
------------------------
Point names, data types, units and value->SF associations come from
`sunspec_models.json`, generated verbatim from the SunSpec Alliance's published
model definitions (Apache-2.0). Nothing about the register layout is recalled or
hand-transcribed. The "not implemented" values below are from the SunSpec
Information Model reference, which the JSON model files do not carry.
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from typing import Any, Optional

__all__ = [
    "SUNSPEC_ID", "SUNSPEC_ID_BYTES", "NOT_IMPLEMENTED", "SF_MIN", "SF_MAX",
    "load_models", "model_spec", "detect_sunspec_block", "governed_points",
    "is_not_implemented", "scale", "apply_scale_factors",
]

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "sunspec_models.json")

# The marker that opens every SunSpec register map: ASCII "SunS" as two 16-bit
# registers (0x5375, 0x6E53). Its presence at the base address is how a client
# knows it is talking to a SunSpec device at all.
SUNSPEC_ID = 0x53756E53
SUNSPEC_ID_BYTES = b"SunS"

# Scale factors are int16 constrained to this range by the specification. A
# value outside it is a malformed register, not an extreme scale.
SF_MIN, SF_MAX = -10, 10

# "Not implemented" values, per the SunSpec Information Model reference. These
# are the wire encodings for "this device does not provide this point" — they
# are NOT measurements and must never survive into a normalized reading.
#
# Accumulators are deliberately absent. The spec gives 0 as their
# "not accumulated" value, but 0 kWh is also what a brand-new meter legitimately
# reports, and a device that has genuinely produced nothing today reads 0 all
# morning. Nulling it would destroy real data to catch a case we cannot
# distinguish, so acc16/acc32/acc64 zeros are reported as a diagnostic
# (`acc_zero`) and passed through as the number 0.
NOT_IMPLEMENTED: dict[str, Any] = {
    "int16":      -0x8000,              # 0x8000
    "uint16":      0xFFFF,
    "int32":      -0x80000000,          # 0x80000000
    "uint32":      0xFFFFFFFF,
    "int64":      -0x8000000000000000,
    "uint64":      0xFFFFFFFFFFFFFFFF,
    "sunssf":     -0x8000,
    "enum16":      0xFFFF,
    "enum32":      0xFFFFFFFF,
    "bitfield16":  0xFFFF,
    "bitfield32":  0xFFFFFFFF,
    "bitfield64":  0xFFFFFFFFFFFFFFFF,
    "pad":        -0x8000,
}

# Representable range per register type. A value outside its own type's range
# did not come off that register — it came off a decoder that got the signedness
# or the word count wrong. 65535 on an int16 point is the classic: the register
# holds 0xFFFF, the correct signed decode is -1, and an unsigned decode reports
# 65535. Scaling either one produces a number; only one of them is the reading.
#
# This is the check that catches a whole family of vendor/client mismatches
# before any of them reach a canonical field.
TYPE_RANGE: dict[str, tuple] = {
    "int16":      (-0x8000, 0x7FFF),
    "uint16":     (0, 0xFFFF),
    "acc16":      (0, 0xFFFF),
    "enum16":     (0, 0xFFFF),
    "bitfield16": (0, 0xFFFF),
    "sunssf":     (-0x8000, 0x7FFF),
    "int32":      (-0x80000000, 0x7FFFFFFF),
    "uint32":     (0, 0xFFFFFFFF),
    "acc32":      (0, 0xFFFFFFFF),
    "enum32":     (0, 0xFFFFFFFF),
    "bitfield32": (0, 0xFFFFFFFF),
    "int64":      (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    "uint64":     (0, 0xFFFFFFFFFFFFFFFF),
    "acc64":      (0, 0xFFFFFFFFFFFFFFFF),
}

_ACCUMULATOR_TYPES = frozenset({"acc16", "acc32", "acc64"})
_FLOAT_TYPES = frozenset({"float32", "float64"})
_STRING_TYPES = frozenset({"string"})


@lru_cache(maxsize=1)
def load_models() -> dict:
    with open(_PATH) as fh:
        return json.load(fh)


def model_spec(model_id) -> Optional[dict]:
    """The distilled spec for a model id, or None if we do not carry it."""
    if model_id is None:
        return None
    return load_models()["models"].get(str(int(model_id)))


def _as_model_ids(model_id) -> list:
    """Normalize the model argument to a list.

    A real device is not one model. A BESS advertises model 124 (storage
    control) AND model 802 (battery measurements) in the same register map, and
    an inverter with a built-in meter advertises 103 alongside 203. Scaling such
    a device against a single model silently leaves every point from the other
    block unscaled: model 802 does not define ChaState, so a 124+802 battery
    scaled as "802" reported state of charge as 340% and had it nulled by the
    physics bound — the reading looked absent rather than mis-scaled, which is
    the harder failure to trace.
    """
    if model_id is None:
        return []
    if isinstance(model_id, (list, tuple, set, frozenset)):
        return [int(m) for m in model_id if m is not None]
    return [int(model_id)]


def merged_spec(model_ids: list) -> Optional[dict]:
    """One combined point map across several model blocks.

    Where two models define the same point name (both 103 and 203 have `W`,
    `Hz`, `A`), the FIRST model listed wins. Callers pass the device's primary
    model first for that reason; the alternative — refusing to merge on any
    overlap — would make the common inverter-plus-meter device unscalable.
    """
    specs = [model_spec(m) for m in model_ids]
    specs = [sp for sp in specs if sp is not None]
    if not specs:
        return None
    if len(specs) == 1:
        return specs[0]
    points: dict = {}
    for sp in specs:
        for name, p in sp["points"].items():
            points.setdefault(name, p)
    return {
        "id": specs[0]["id"],
        "slug": "+".join(sp["slug"] for sp in specs),
        "label": " + ".join(str(sp.get("label")) for sp in specs),
        "point_count": len(points),
        "merged_from": [sp["id"] for sp in specs],
        "scale_factor_points": sorted(
            {n for n, p in points.items() if p.get("type") == "sunssf"}),
        "points": points,
    }


def detect_sunspec_block(registers) -> bool:
    """True if `registers` opens with the SunSpec identifier.

    Accepts the raw bytes, the 32-bit word, or the two 16-bit registers as they
    come off a Modbus read — a client may present any of the three, and all of
    them are the same four bytes.
    """
    if registers is None:
        return False
    if isinstance(registers, (bytes, bytearray)):
        return bytes(registers[:4]) == SUNSPEC_ID_BYTES
    if isinstance(registers, str):
        return registers[:4] == "SunS"
    if isinstance(registers, int):
        return registers == SUNSPEC_ID
    try:
        regs = list(registers)
    except TypeError:
        return False
    if len(regs) >= 2 and all(isinstance(r, int) for r in regs[:2]):
        return ((regs[0] << 16) | regs[1]) == SUNSPEC_ID
    return False


def is_not_implemented(value, point_type: Optional[str]) -> bool:
    """True when `value` is the wire encoding for 'this point is absent'.

    Type-directed, not value-directed: 65535 is 'not implemented' for a uint16
    point and an ordinary reading for an acc32 one, so the same number must be
    judged differently depending on the register it came out of.
    """
    if point_type is None:
        return False
    if point_type in _FLOAT_TYPES:
        return isinstance(value, float) and math.isnan(value)
    if point_type in _STRING_TYPES:
        return value is None or (isinstance(value, str) and not value.strip())
    if point_type in _ACCUMULATOR_TYPES:
        return False                      # see NOT_IMPLEMENTED's docstring
    sentinel = NOT_IMPLEMENTED.get(point_type)
    if sentinel is None:
        return False
    try:
        return int(value) == sentinel
    except (TypeError, ValueError):
        return False


def out_of_type_range(value, point_type: Optional[str]) -> bool:
    """True when `value` cannot have come off a register of this type."""
    rng = TYPE_RANGE.get(point_type or "")
    if rng is None:
        return False
    try:
        v = int(value)
    except (TypeError, ValueError):
        return False
    return not (rng[0] <= v <= rng[1])


def governed_points(spec: dict) -> dict:
    """{point_name: sf_point_name} for every point the model says is scaled.

    Read from the spec's own `sf` attribute, so a shared scale factor governing
    four registers (A_SF over A/AphA/AphB/AphC) is picked up exactly as the
    standard defines it, not guessed from the name.
    """
    return {name: p["sf"] for name, p in (spec.get("points") or {}).items()
            if p.get("sf")}


def _sf_by_convention(data: dict) -> dict:
    """Fallback association when the model id is unknown.

    SunSpec names a scale factor after the point it scales (`W` / `W_SF`), so
    the convention recovers most of the map. It cannot recover a SHARED factor —
    `A_SF` also governs AphA/AphB/AphC, and nothing in the name says so — which
    is why an explicit model id is always preferred and its absence is reported.
    """
    sf_names = {k for k in data if str(k).endswith("_SF")}
    out = {}
    for sf in sf_names:
        base = str(sf)[:-3]
        if base in data:
            out[base] = sf
    return out


def scale(value, sf: int):
    """value x 10^sf, with the decimal placed exactly.

    Deliberately not `value * 10**sf`: 48700 * 10**-2 is 486.99999999999994 in
    binary floating point. SunSpec scaling is a decimal point move on an integer
    register, so it is done as one, and 487.0 comes back exactly. The acceptance
    bar for this sprint is 0.01% and float error alone would eat a tenth of it
    on a chain of conversions.
    """
    from decimal import Decimal
    if value is None:
        return None
    d = Decimal(str(value)).scaleb(int(sf))
    as_float = float(d)
    return int(as_float) if d == d.to_integral_value() and abs(as_float) < 2**53 \
        else as_float


def apply_scale_factors(data: dict, model_id=None, *, strict: bool = False,
                        drop_sf_points: bool = True,
                        sf_map: Optional[dict] = None) -> tuple[dict, list, list]:
    """Apply SunSpec scale factors to a flat {point_name: value} reading.

    This is a pre-normalization transform: it consumes the `*_SF` points and
    returns the measurement points as true quantities in their declared units,
    which is the form the rest of the pipeline assumes every value already has.

    Returns (scaled, records, diagnostics).
      scaled       the reading with scale factors applied and SF points removed
      records      one entry per point actually scaled — the audit trail that
                   makes a 100x correction reviewable instead of invisible
      diagnostics  everything that was wrong or suspicious, each with a
                   `severity` of "error" | "warning" | "info"

    `strict=True` nulls a governed point whose scale factor is missing rather
    than passing it through unscaled. Default is False because point names like
    `W` and `A` are not SunSpec-exclusive, and nulling a non-SunSpec device's
    watts because it did not ship a W_SF would be worse than the problem. The
    diagnostic is raised either way — silence is the one option not offered.
    """
    model_ids = _as_model_ids(model_id)
    spec = merged_spec(model_ids)
    scaled = dict(data)
    records: list[dict] = []
    diags: list[dict] = []

    if model_ids and spec is None:
        diags.append({
            "code": "unknown_model", "severity": "warning", "model_id": model_id,
            "detail": (f"SunSpec model {model_id} is not in the shipped index; "
                       "falling back to name-convention scale-factor association, "
                       "which cannot see shared factors such as A_SF."),
        })

    if spec:
        governs = governed_points(spec)
        point_types = {n: p.get("type") for n, p in spec["points"].items()}
        source = ("model_" + "+".join(str(m) for m in spec["merged_from"])
                  if spec.get("merged_from") else f"model_{spec['id']}")

        # A vendor may implement SunSpec faithfully while renaming every point.
        # SolarEdge does: the register layout is model 103, but the points are
        # called I_AC_Power / I_AC_Power_SF rather than W / W_SF. The model's
        # own map then matches nothing in the payload and the device comes back
        # entirely unscaled — silently, because every point simply falls outside
        # `governs`.
        #
        # So the model map is UNIONED with name-convention association, which
        # covers vendor spellings that keep the X / X_SF pairing. The model
        # always wins where it has an opinion, since only it can see a shared
        # factor.
        # Pack-supplied groupings first: they carry vendor documentation the
        # naming convention cannot infer, such as one I_AC_Voltage_SF governing
        # all six SolarEdge voltage registers.
        if sf_map:
            governs = {**governs, **{p: sf for p, sf in sf_map.items()
                                     if p in data and p not in governs}}
        by_name = _sf_by_convention(data)
        conventional = {p: sf for p, sf in by_name.items() if p not in governs}
        if conventional:
            governs = {**governs, **conventional}
            matched_model_points = sum(1 for p in governed_points(spec) if p in data)
            diags.append({
                "code": "vendor_point_names", "severity": "info",
                "points": sorted(conventional),
                "model_points_matched": matched_model_points,
                "detail": (f"{len(conventional)} scaled point(s) are not named in "
                           f"the model definition and were paired with their "
                           f"scale factor by the X/X_SF naming convention. This "
                           "is how a vendor-renamed SunSpec implementation is "
                           "handled. Note that a SHARED factor cannot be "
                           "recovered this way — if this device also reports "
                           "per-phase points governed by one factor, those go "
                           "unscaled."),
            })
    else:
        governs = dict(_sf_by_convention(data))
        if sf_map:
            governs.update({p: sf for p, sf in sf_map.items()
                            if p in data and p not in governs})
        point_types = {}
        source = "pack_sf_map" if sf_map else "name_convention"
        if any(str(k).endswith("_SF") for k in data):
            diags.append({
                "code": "no_model_id", "severity": "warning",
                "detail": ("Scale factors present but no SunSpec model id was "
                           "supplied. Shared factors (A_SF over AphA/AphB/AphC) "
                           "cannot be resolved by name alone and those points "
                           "will go unscaled."),
            })

    # ── Resolve every scale factor register first ───────────────────────────
    sf_values: dict[str, Any] = {}
    for sf_name in sorted(set(governs.values())):
        if sf_name not in data:
            continue
        raw = data[sf_name]
        if is_not_implemented(raw, "sunssf"):
            sf_values[sf_name] = None
            diags.append({
                "code": "sf_not_implemented", "severity": "error",
                "sf_point": sf_name, "raw": raw,
                "detail": (f"{sf_name} reads the sunssf not-implemented value "
                           f"({NOT_IMPLEMENTED['sunssf']}). The points it scales "
                           "have no defined magnitude and are nulled."),
            })
            continue
        try:
            sf_int = int(raw)
        except (TypeError, ValueError):
            sf_values[sf_name] = None
            diags.append({
                "code": "sf_not_numeric", "severity": "error",
                "sf_point": sf_name, "raw": raw,
                "detail": f"{sf_name} is not an integer: {raw!r}.",
            })
            continue
        if not (SF_MIN <= sf_int <= SF_MAX):
            sf_values[sf_name] = None
            diags.append({
                "code": "sf_out_of_range", "severity": "error",
                "sf_point": sf_name, "raw": sf_int,
                "detail": (f"{sf_name} = {sf_int} is outside the specified "
                           f"[{SF_MIN}, {SF_MAX}] range for sunssf. Treated as "
                           "unusable rather than applied — a bad exponent moves "
                           "the decimal further than any real measurement."),
            })
            continue
        sf_values[sf_name] = sf_int

    # ── Apply ───────────────────────────────────────────────────────────────
    for point, sf_name in sorted(governs.items()):
        if point not in data:
            continue
        raw = data[point]
        ptype = point_types.get(point)

        if is_not_implemented(raw, ptype):
            scaled[point] = None
            diags.append({
                "code": "point_not_implemented", "severity": "info",
                "point": point, "raw": raw, "type": ptype,
                "detail": (f"{point} reads the {ptype} not-implemented value; "
                           "nulled before scaling so it cannot be laundered into "
                           "a plausible number."),
            })
            continue

        if out_of_type_range(raw, ptype):
            scaled[point] = None
            lo, hi = TYPE_RANGE[ptype]
            diags.append({
                "code": "type_range_violation", "severity": "error",
                "point": point, "raw": raw, "type": ptype,
                "expected_range": [lo, hi],
                "detail": (f"{point} is declared {ptype} (range [{lo}, {hi}]) but "
                           f"the reading is {raw}. A value outside its own "
                           "register type did not come off that register — the "
                           "usual cause is a client decoding a signed register "
                           "as unsigned, or assembling a 32-bit point from the "
                           "wrong number of words. Nulled: scaling it would turn "
                           "a decode error into a plausible measurement."),
            })
            continue

        if ptype in _ACCUMULATOR_TYPES and _is_zero(raw):
            diags.append({
                "code": "acc_zero", "severity": "info",
                "point": point, "type": ptype,
                "detail": (f"{point} is an accumulator reading 0, which the spec "
                           "also uses for 'not accumulated'. Passed through as a "
                           "real zero — the two cases are indistinguishable on "
                           "the wire and discarding it would lose real data."),
            })

        if sf_name not in data:
            diags.append({
                "code": "sf_missing", "severity": "error",
                "point": point, "sf_point": sf_name, "raw": raw,
                "detail": (f"{point} is a scaled point but {sf_name} was not in "
                           f"the reading. Its magnitude is undefined — it may be "
                           f"correct or off by any power of ten from 10^-10 to "
                           f"10^10. " + ("Nulled (strict)." if strict else
                                         "Passed through UNSCALED and flagged.")),
            })
            if strict:
                scaled[point] = None
            continue

        sf = sf_values.get(sf_name)
        if sf is None:
            scaled[point] = None
            diags.append({
                "code": "sf_unusable", "severity": "error",
                "point": point, "sf_point": sf_name, "raw": raw,
                "detail": f"{point} nulled: {sf_name} could not be used.",
            })
            continue

        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            diags.append({
                "code": "value_not_numeric", "severity": "warning",
                "point": point, "raw": raw,
                "detail": f"{point} is not numeric ({raw!r}); left as-is.",
            })
            continue

        out = scale(raw, sf)
        scaled[point] = out
        if sf != 0:
            records.append({
                "point": point, "sf_point": sf_name, "scale_factor": sf,
                "raw": raw, "scaled": out,
                "multiplier": f"10^{sf}",
                # .get, not [] — a point paired by naming convention is not in
                # the model definition at all, so there is no units entry for
                # it. Indexing here raised KeyError on every vendor-renamed
                # SunSpec device and the whole stage degraded to unscaled.
                "units": ((spec["points"].get(point) or {}).get("units")
                          if spec else None),
                "source": source,
            })

    if drop_sf_points:
        for sf_name in set(governs.values()):
            scaled.pop(sf_name, None)
        # A payload can carry SF registers for points it did not send. Those are
        # still SunSpec plumbing, not measurements, and must not reach the
        # normalizer where the signal classifier would try to make sense of them.
        for k in [k for k in list(scaled) if str(k).endswith("_SF")]:
            scaled.pop(k, None)

    return scaled, records, diags


def _is_zero(v) -> bool:
    try:
        return float(v) == 0.0
    except (TypeError, ValueError):
        return False
