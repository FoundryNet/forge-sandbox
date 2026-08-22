"""The canonical vocabulary and its unit contract — one source of truth.

WHY THIS EXISTS
---------------
The kernel had FOUR disagreeing notions of "the canonical field list":

    forge_core.canonicals._canonical_field_universe()   243 hardcoded names
    distinct canonical_field in forge_schema_mappings   366 names (what
                                                        resolution actually emits)
    corpus_v2/canonical_schema_v2.json                  75 entries in a different
                                                        namespace (cnc.spindle.*)
    the sandbox's packs/_canonical_fields.json          408 names

`axes.x_position_actual` is emitted by the corpus 232 times and is absent from
the hardcoded universe. Any validator built on the wrong list would have rejected
real fields, so there was no list you could safely validate against — which is
precisely why the LLM layer was free to invent names.

`canonical_fields.json` is generated from what the corpus actually emits and is
the ONLY vocabulary the kernel may output. A resolution that lands outside it is
a hallucination and is recorded as UNRESOLVED.

    An UNRESOLVED tag is honest. A hallucinated field name is a lie.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Optional

log = logging.getLogger("forge.field_registry")

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "canonical_fields.json")

__all__ = [
    "load", "fields", "is_canonical", "field_spec", "accepts_unit",
    "unit_of", "valid_range", "prompt_vocabulary", "REGISTRY_VERSION",
]

REGISTRY_VERSION = "1.1.0"


@lru_cache(maxsize=1)
def load() -> dict:
    try:
        with open(_PATH) as fh:
            return json.load(fh)
    except Exception as e:                                   # pragma: no cover
        log.error("field_registry: cannot load %s: %s", _PATH, e)
        return {"fields": {}, "field_count": 0, "version": "unloaded"}


@lru_cache(maxsize=1)
def fields() -> dict:
    return load().get("fields") or {}


@lru_cache(maxsize=1)
def _canonical_set() -> frozenset:
    return frozenset(fields())


def is_canonical(name: Optional[str]) -> bool:
    """True only for a name in the registry. This is the hard gate."""
    return bool(name) and name in _canonical_set()


def field_spec(name: str) -> dict:
    return fields().get(name) or {}


def unit_of(name: str) -> Optional[str]:
    return field_spec(name).get("unit")


def valid_range(name: str):
    vr = field_spec(name).get("valid_range")
    return tuple(vr) if isinstance(vr, list) and len(vr) == 2 else None


def accepts_unit(name: str, unit: Optional[str]) -> Optional[bool]:
    """Does this field accept a value declared in `unit`?

    True  — accepted (either already correct, or convertible)
    False — the field has a unit contract and `unit` is not in it
    None  — no contract for this field, or no unit declared: nothing to check
    """
    if not unit:
        return None
    spec = field_spec(name)
    accepted = spec.get("accepted_input_units")
    if not accepted:
        return None
    return unit in accepted


def prompt_vocabulary(limit: Optional[int] = None,
                      vertical: Optional[str] = None) -> str:
    """The field list as it is pasted into the LLM prompt.

    Rendered as `name [unit]` so the model can see the unit contract and stops
    trying to encode units into names it invents (`condenser_pressure_psi`).
    """
    fs = fields()
    names = sorted(fs)
    if limit:
        names = names[:limit]
    lines = []
    for n in names:
        u = fs[n].get("unit")
        lines.append(f"{n} [{u}]" if u else n)
    return "\n".join(lines)
