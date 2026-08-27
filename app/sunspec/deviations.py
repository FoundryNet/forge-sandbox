"""Detection for the SunSpec deviations real vendors actually ship.

The specification is unambiguous. Devices are not. REIG's field reports name
four deviations that survive certification and reach commissioning:

  1. Byte order   32-bit points are specified big-endian. Some vendors emit the
                  two 16-bit registers word-swapped.
  2. Model ID     A device reports model 101 (single phase) while populating the
                  three-phase registers of model 103.
  3. Stale SF     Scale-factor registers refresh on a different cycle than the
                  value registers, so a fresh value is paired with an old
                  exponent for one poll.
  4. Latency      A gateway turns a read around in ~200ms against a 50ms client
                  timeout.

(4) is a transport property — it is the client's socket timeout, not something
the normalizer can see in a decoded reading — so it is out of scope here and
called out in the report rather than faked with a detector that cannot work.
The other three are all visible in the data and are detected below.

Every detector here REPORTS. None of them silently rewrites a reading: a
heuristic that quietly "fixes" byte order will eventually fix a correct value
into a wrong one, and the failure would be indistinguishable from the bug it was
meant to catch. Correction is offered explicitly and separately.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from .scale_factor import is_not_implemented, model_spec

__all__ = [
    "WORD_ORDER_BIG", "WORD_ORDER_LITTLE", "decode_u32", "decode_i32",
    "detect_word_order", "detect_model_mismatch", "DecadeJumpDetector",
]

WORD_ORDER_BIG = "big"          # specified: most-significant register first
WORD_ORDER_LITTLE = "little"    # observed: word-swapped


def decode_u32(hi: int, lo: int, word_order: str = WORD_ORDER_BIG) -> int:
    """Assemble two 16-bit registers into a uint32."""
    a, b = (hi, lo) if word_order == WORD_ORDER_BIG else (lo, hi)
    return ((int(a) & 0xFFFF) << 16) | (int(b) & 0xFFFF)


def decode_i32(hi: int, lo: int, word_order: str = WORD_ORDER_BIG) -> int:
    v = decode_u32(hi, lo, word_order)
    return v - 0x100000000 if v & 0x80000000 else v


def detect_word_order(hi: int, lo: int, plausible, *, signed: bool = False,
                      point: Optional[str] = None) -> dict:
    """Decide which word order a 32-bit register pair was transmitted in.

    `plausible` is a (low, high) range the decoded quantity must fall inside —
    the physics of the field, supplied by the caller, because nothing about the
    two registers themselves says which end is which.

    The decision is only reported when exactly ONE order lands in range. Word
    swapping is undetectable when both decodes are plausible (a lifetime-energy
    counter reading 0x00010001 is 65537 either way), and claiming a verdict there
    would be a guess dressed as a measurement.
    """
    dec = decode_i32 if signed else decode_u32
    big, little = dec(hi, lo, WORD_ORDER_BIG), dec(hi, lo, WORD_ORDER_LITTLE)
    lo_b, hi_b = plausible
    ok_big, ok_little = (lo_b <= big <= hi_b), (lo_b <= little <= hi_b)

    out = {"point": point, "registers": [hi, lo],
           "decoded_big": big, "decoded_little": little,
           "plausible_range": [lo_b, hi_b]}
    if ok_big and not ok_little:
        out.update(word_order=WORD_ORDER_BIG, confident=True, deviation=False,
                   detail="Big-endian decode is in range; word-swapped is not. "
                          "Conforms to the specification.")
    elif ok_little and not ok_big:
        out.update(word_order=WORD_ORDER_LITTLE, confident=True, deviation=True,
                   detail=(f"Word-swapped decode ({little}) is in range and the "
                           f"specified big-endian decode ({big}) is not. This "
                           "device deviates from SunSpec; the client must swap "
                           "words for 32-bit points."))
    elif ok_big and ok_little:
        out.update(word_order=None, confident=False, deviation=None,
                   detail=("Both decodes are plausible — word order is not "
                           "determinable from this sample. Needs a value whose "
                           "high and low words differ in magnitude."))
    else:
        out.update(word_order=None, confident=False, deviation=None,
                   detail=("Neither decode is plausible. The pair is corrupt, "
                           "misaligned, or the range is wrong — do not guess."))
    return out


# Points that exist in the single-phase inverter model but which a genuinely
# single-phase device must report as "not implemented". Populating them is the
# signature of a device that answers 101 and behaves like 103.
_THREE_PHASE_ONLY = ("AphB", "AphC", "PhVphB", "PhVphC",
                     "PPVphBC", "PPVphCA", "WphB", "WphC")
_SPLIT_PHASE_OK = ("AphB", "PhVphB", "PPVphAB", "WphB")


def detect_model_mismatch(data: dict, declared_model_id) -> dict:
    """Check a reading against the model it claims to implement.

    Models 101, 102 and 103 share an identical 45-point layout — the spec
    distinguishes them by which points a conforming device *implements*, not by
    which points exist. So the test is not "is AphC in the payload" (it always
    is) but "does AphC carry a measurement when the device says it is single
    phase".
    """
    spec = model_spec(declared_model_id)
    out = {"declared_model_id": declared_model_id,
           "known_model": spec is not None, "mismatch": False,
           "populated_unexpected": [], "detail": ""}
    if spec is None:
        out["detail"] = f"Model {declared_model_id} is not in the shipped index."
        return out

    types = {n: p.get("type") for n, p in spec["points"].items()}

    def populated(name):
        if name not in data:
            return False
        v = data[name]
        if v is None or is_not_implemented(v, types.get(name)):
            return False
        try:
            return float(v) != 0.0
        except (TypeError, ValueError):
            return False

    if int(declared_model_id) == 101:
        unexpected = [p for p in _THREE_PHASE_ONLY if populated(p)]
        if unexpected:
            out.update(
                mismatch=True, populated_unexpected=unexpected,
                suggested_model_id=103 if any(
                    p not in _SPLIT_PHASE_OK for p in unexpected) else 102,
                detail=(f"Declared model 101 (single phase) but {unexpected} carry "
                        "measurements. A single-phase inverter reports those as "
                        "not-implemented. Treat the block as three-phase (103) — "
                        "reading it as 101 discards two thirds of the plant."))
    elif int(declared_model_id) in (102, 103):
        expected = ("AphB",) if int(declared_model_id) == 102 else ("AphB", "AphC")
        missing = [p for p in expected if p in data and not populated(p)]
        if missing:
            out.update(
                mismatch=True, populated_unexpected=[],
                missing_expected=missing,
                detail=(f"Declared model {declared_model_id} but {missing} are "
                        "not implemented. The device may in fact be single "
                        "phase, or those phases are down."))
    if not out["mismatch"]:
        out["detail"] = f"Consistent with model {declared_model_id}."
    return out


class DecadeJumpDetector:
    """Catch a scale factor that went stale relative to its value register.

    When a vendor refreshes SF and value registers on different cycles, one poll
    pairs a fresh value with the previous exponent. The resulting reading is off
    by exactly a power of ten and then snaps back — which is precisely what makes
    it detectable, and precisely what a threshold alarm cannot see, because the
    excursion is a single sample.

    The test is not "did the value jump" but "did it jump by a factor that is a
    clean power of ten". Real electrical quantities move continuously; a grid
    frequency does not go from 59.98 to 5998 because the grid did something.

    State is per (device, point) and holds one previous sample. Feed it the
    SCALED value — the whole point is to catch a bad exponent after it was
    applied.
    """

    def __init__(self, tolerance: float = 0.02, min_magnitude: float = 1e-9):
        # tolerance: how close to a clean 10^n the ratio must be. 2% leaves room
        # for the quantity genuinely drifting between polls without letting a
        # 10.0x-vs-9.4x ambiguity through.
        self.tolerance = tolerance
        self.min_magnitude = min_magnitude
        self._last: dict[tuple, Any] = {}

    def observe(self, device: str, point: str, value) -> Optional[dict]:
        key = (device, point)
        prev = self._last.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and not math.isnan(float(value)):
            self._last[key] = float(value)
        else:
            return None
        if prev is None or abs(prev) < self.min_magnitude \
                or abs(float(value)) < self.min_magnitude:
            return None

        ratio = float(value) / prev
        if ratio <= 0:
            return None
        exponent = math.log10(ratio)
        nearest = round(exponent)
        if nearest == 0 or abs(exponent - nearest) > self.tolerance:
            return None
        return {
            "code": "decade_jump", "severity": "error",
            "device": device, "point": point,
            "previous": prev, "current": float(value),
            "ratio": ratio, "decades": nearest,
            "detail": (f"{point} moved by exactly 10^{nearest} between polls "
                       f"({prev} -> {value}). An electrical quantity does not "
                       "change by a clean power of ten; this is the signature of "
                       "a scale-factor register that refreshed out of step with "
                       "its value register."),
        }
