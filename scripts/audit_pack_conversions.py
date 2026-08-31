#!/usr/bin/env python3
"""Audit every pack unit declaration for double-conversion bugs.

The PF class of defect: a SunSpec scale factor already normalises a register to
its final magnitude, and then the unit converter fires a SECOND time because
the pack declared the pre-scale unit. `PF` with `PF_SF=-4` yields 0.973 -- a
ratio -- and a pack saying `"PF": "%"` divides it by 100 again. The value is
100x wrong and ships at confidence 1.0, which is the worst combination there
is: no validator can catch it, because 0.00973 is a perfectly plausible ratio.

Run standalone for a report; imported, `find_double_conversions()` returns the
findings so CI can assert the list stays empty.
"""
import json
import os
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PACK_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app", "packs")

# Units a SunSpec scale factor ALREADY resolves. If a scaled point also
# declares one of these, the converter re-applies a factor the SF spent.
_SF_RESOLVED = {
    "%": "the scale factor already yields a ratio",
    "pct": "the scale factor already yields a ratio",
    "percent": "the scale factor already yields a ratio",
}

# Points whose magnitude is set by a scale factor in the SunSpec models we ship.
_SCALED_POINT_HINTS = ("pf", "_sf", "w", "va", "var", "wh", "hz", "a", "v",
                       "tmp", "dc")


def _load_packs():
    for path in sorted(glob(os.path.join(PACK_DIR, "*.json"))):
        if ".bak" in path or os.path.basename(path).startswith("_"):
            continue
        try:
            with open(path) as fh:
                yield os.path.basename(path), json.load(fh)
        except Exception as exc:                      # a broken pack is a finding
            print(f"  !! {os.path.basename(path)}: unreadable ({exc})")


def _canonical_fields():
    from app.corpus import load
    return load()[2]["fields"]


def _sunspec_scaled_points():
    """Points the shipped SunSpec model definitions attach a scale factor to."""
    scaled = set()
    try:
        from app import sunspec as _ss
        models = getattr(_ss, "MODELS", None) or {}
        for spec in models.values():
            for pt, meta in (spec.get("points") or {}).items():
                if isinstance(meta, dict) and meta.get("sf"):
                    scaled.add(pt.lower())
    except Exception:
        pass
    return scaled


def find_double_conversions():
    """Every (pack, point) where an SF and a unit conversion would both fire."""
    fields = _canonical_fields()
    scaled = _sunspec_scaled_points()
    findings = []
    for name, pack in _load_packs():
        tag_units = pack.get("tag_units") or {}
        mappings = pack.get("mappings") or {}
        is_sunspec_family = bool(pack.get("sunspec_sf_map")) or any(
            k in name for k in ("sunspec", "fronius", "solaredge", "sma",
                                "sungrow", "victron", "tesla"))
        for point, declared in tag_units.items():
            canonical = mappings.get(point)
            if not canonical:
                continue
            target = (fields.get(canonical) or {}).get("unit")
            low = str(declared).strip().lower()
            point_l = point.lower()
            sf_applies = (point_l in scaled
                          or (is_sunspec_family
                              and any(h in point_l for h in _SCALED_POINT_HINTS)))
            if not sf_applies:
                continue
            if low in _SF_RESOLVED and str(target).strip().lower() == "ratio":
                findings.append({
                    "pack": name, "point": point, "canonical": canonical,
                    "declared_unit": declared, "target_unit": target,
                    "why": _SF_RESOLVED[low],
                    "fix": "declare 'ratio' -- the scale factor is the conversion",
                })
    return findings


def main():
    findings = find_double_conversions()
    print("Pack conversion audit -- SF + unit-converter double application\n")
    if not findings:
        print("  clean: no pack declares a unit that a scale factor already resolved")
        return 0
    for f in findings:
        print(f"  DOUBLE CONVERSION: {f['pack']}/{f['point']} -> {f['canonical']}")
        print(f"     declared: {f['declared_unit']!r}   canonical target: {f['target_unit']!r}")
        print(f"     {f['why']}")
        print(f"     fix: {f['fix']}\n")
    print(f"  {len(findings)} finding(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
