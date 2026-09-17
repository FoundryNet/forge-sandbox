#!/usr/bin/env python3
"""Detect drift between the production engine and the sandbox engine.

Prod (`forge-prod/forge_core/`) and the sandbox (`forge-sandbox/app/`) carry
SEPARATE copies of unit_converter.py, value_validator.py and the canonical
registry. Today they were hand-ported. In a month they drift, and the drift is
invisible until a prospect's number disagrees with production's.

A byte-level diff is useless here: the sandbox is a deliberate SUBSET (fewer
fields, fewer packs, no embedder), so the files differ by design. What must NOT
differ is the CONTRACT — the tables that decide what a unit means, what a field
holds, and what counts as a physically possible value. Those are compared
semantically:

    UNIT_ALIASES · QUANTITY · TARGET_UNIT · CONVERSIONS(keys)
    _NAME_SUFFIXES · _UNAMBIGUOUS_SUFFIX · _BARE_SUFFIX_CONTEXT
    PHYSICS_BOUNDS_BY_QUANTITY
    and, for every field present in BOTH registries: unit, quantity, bounds

Subset is allowed in one direction only: the sandbox may be MISSING fields.
It may never DISAGREE about one it has.

    python3 scripts/check_engine_parity.py            # exits 1 on drift
    PROD_ROOT=/path/to/forge-prod python3 scripts/...
"""
import importlib.util
import json
import os
import sys

SB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_ROOT = os.environ.get(
    "PROD_ROOT",
    os.path.expanduser("~/replicant/pages/api/request/mint4.0/FoundryLabs/forge-prod"))


def load_pkg(root, package, module):
    """Import `package.module` from `root`.

    Loaded as a package member, not a bare file: both engines' value_validator
    does `from . import field_registry`, which fails outright under
    spec_from_file_location. The two roots use different package names
    (forge_core vs app), so they coexist in sys.modules without clobbering.
    """
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"{package}.{module}")


def diff_map(label, a, b, drift, allow_extra_prod=True):
    """Compare two {key: value} tables. A key in prod but not sandbox is a
    PORT GAP; a key present in both with different values is DISAGREEMENT."""
    for k in sorted(set(a) & set(b)):
        if a[k] != b[k]:
            drift.append(f"{label}: '{k}' is {a[k]!r} in prod but {b[k]!r} in sandbox")
    missing = sorted(set(a) - set(b))
    if missing:
        drift.append(f"{label}: {len(missing)} entr(y/ies) in prod but NOT ported to "
                     f"sandbox: {missing[:12]}")
    extra = sorted(set(b) - set(a))
    if extra and not allow_extra_prod:
        drift.append(f"{label}: {len(extra)} sandbox-only entr(y/ies): {extra[:12]}")
    elif extra:
        drift.append(f"{label}: {len(extra)} entr(y/ies) in sandbox but NOT in prod: "
                     f"{extra[:12]}")


def main():
    if not os.path.isdir(PROD_ROOT):
        print(f"prod repo not found at {PROD_ROOT} — set PROD_ROOT. Skipping.")
        return 0

    p_uc = load_pkg(PROD_ROOT, "forge_core", "unit_converter")
    p_vv = load_pkg(PROD_ROOT, "forge_core", "value_validator")
    s_uc = load_pkg(SB_ROOT, "app", "unit_converter")
    s_vv = load_pkg(SB_ROOT, "app", "value_validator")

    drift = []
    diff_map("UNIT_ALIASES", p_uc.UNIT_ALIASES, s_uc.UNIT_ALIASES, drift)
    diff_map("QUANTITY", p_uc.QUANTITY, s_uc.QUANTITY, drift)
    diff_map("TARGET_UNIT", p_uc.TARGET_UNIT, s_uc.TARGET_UNIT, drift)
    diff_map("_NAME_SUFFIXES", dict(p_uc._NAME_SUFFIXES), dict(s_uc._NAME_SUFFIXES), drift)
    diff_map("_BARE_SUFFIX_CONTEXT", p_uc._BARE_SUFFIX_CONTEXT, s_uc._BARE_SUFFIX_CONTEXT, drift)
    diff_map("PHYSICS_BOUNDS_BY_QUANTITY",
             p_vv.PHYSICS_BOUNDS_BY_QUANTITY, s_vv.PHYSICS_BOUNDS_BY_QUANTITY, drift)

    # CONVERSIONS holds lambdas, which never compare equal. The PAIRS are the
    # contract: "can this engine convert X to Y at all".
    diff_map("CONVERSIONS(pairs)",
             {f"{a}->{b}": True for a, b in p_uc.CONVERSIONS},
             {f"{a}->{b}": True for a, b in s_uc.CONVERSIONS}, drift)

    # _UNAMBIGUOUS_SUFFIX is a set.
    only_p = sorted(p_uc._UNAMBIGUOUS_SUFFIX - s_uc._UNAMBIGUOUS_SUFFIX)
    only_s = sorted(s_uc._UNAMBIGUOUS_SUFFIX - p_uc._UNAMBIGUOUS_SUFFIX)
    if only_p:
        drift.append(f"_UNAMBIGUOUS_SUFFIX: {len(only_p)} token(s) in prod only: {only_p[:12]}")
    if only_s:
        drift.append(f"_UNAMBIGUOUS_SUFFIX: {len(only_s)} token(s) in sandbox only: {only_s[:12]}")

    # ── field contracts, for fields BOTH engines carry ─────────────────────
    p_reg = json.load(open(os.path.join(PROD_ROOT, "forge_core", "canonical_fields.json")))["fields"]
    s_reg = json.load(open(os.path.join(SB_ROOT, "app", "canonical_fields.json")))["fields"]
    shared = sorted(set(p_reg) & set(s_reg))
    for f in shared:
        a, b = p_reg[f] or {}, s_reg[f] or {}
        for key in ("unit", "quantity"):
            if a.get(key) != b.get(key):
                drift.append(f"field '{f}': {key} is {a.get(key)!r} in prod but "
                             f"{b.get(key)!r} in sandbox")
        if a.get("physics_bounds") and b.get("physics_bounds") and \
                a["physics_bounds"] != b["physics_bounds"]:
            drift.append(f"field '{f}': bounds {a['physics_bounds']} in prod vs "
                         f"{b['physics_bounds']} in sandbox")

    # ── baseline ───────────────────────────────────────────────────────────
    # The two engines already diverge in ~68 places, most of them the sandbox
    # being RICHER than prod (24 unit aliases, 25 conversions it has and prod
    # does not). Failing the gate on all of that would block every push until a
    # two-engine reconciliation lands, and a check that always fails gets
    # disabled within a week.
    #
    # So the known set is recorded, and the gate fails on anything NEW. The
    # backlog stays visible in the output and shrinks as it is ported; it can
    # never silently grow.
    #   REBASELINE=1 python3 scripts/check_engine_parity.py   # accept current state
    base_path = os.path.join(SB_ROOT, "scripts", "engine_parity_baseline.json")
    baseline = []
    if os.path.exists(base_path):
        baseline = json.load(open(base_path)).get("known", [])
    if os.environ.get("REBASELINE") == "1":
        json.dump({"known": sorted(drift),
                   "note": ("Accepted prod/sandbox divergence. Shrink this list; never "
                            "grow it. Regenerate with REBASELINE=1.")},
                  open(base_path, "w"), indent=2)
        print(f"rebaselined: {len(drift)} known finding(s) -> {base_path}")
        return 0
    known = set(baseline)
    new_drift = [d for d in drift if d not in known]
    fixed = sorted(known - set(drift))

    print(f"prod   : {PROD_ROOT}")
    print(f"sandbox: {SB_ROOT}")
    print(f"registry: prod {len(p_reg)} fields, sandbox {len(s_reg)}, "
          f"{len(shared)} shared (subset is expected and allowed)\n")
    if fixed:
        print(f"\033[32m{len(fixed)} baselined finding(s) now FIXED\033[0m — "
              f"re-run with REBASELINE=1 to shrink the baseline:")
        for d in fixed[:8]:
            print(f"  + {d}")
        print()
    if not drift:
        print("\033[32mNO DRIFT\033[0m — unit, bounds and field contracts are identical "
              "everywhere both engines overlap.")
        return 0
    if not new_drift:
        print(f"\033[33m{len(drift)} known divergence(s), all baselined\033[0m — no NEW "
              f"drift. Backlog is in scripts/engine_parity_baseline.json.")
        return 0
    print(f"\033[31mNEW DRIFT — {len(new_drift)} finding(s) not in the baseline\033[0m\n")
    for d in new_drift:
        print(f"  - {d}")
    print(f"\n({len(drift) - len(new_drift)} further known divergence(s) baselined.)")
    print("Port the gap, or state why the two engines must differ.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
