#!/usr/bin/env python3
"""Pack compiler — the build-time gate a mapping pack must pass before it ships.

WHY THIS EXISTS. A pack row is believed at confidence 1.0 forever. `corpus.py`'s
layer 1 returns `pack.mappings[tag]` verbatim with `match_type: "corpus"` and no
membership check, no unit check, no collision check (app/corpus.py:1353-1357).
`field_registry.is_canonical()` calls itself "the hard gate" and is never called
at runtime. So a wrong pack row is indistinguishable, downstream, from a correct
one — which makes the ONLY place a bad row can be stopped the build.

WHAT IT REFUSES. Five classes, all BLOCKING, each with the reason in the message:

  malformed            the file is not a pack (missing/misshaped required keys,
                       mappings that are not str -> str, sibling maps keyed on a
                       tag the pack does not declare)
  unknown_canonical    a mapping target is not a canonical field. This is the
                       one existing precedent in either repo
                       (tools/build_robotics_packs.py:713-721,
                       scripts/build_energy_packs.py:153-156) and is kept.
  dimension_mismatch   the DECLARED wire unit of a tag and the DECLARED unit of
                       its target field are different physical dimensions, e.g.
                       `powerusage_kw` (kW, power) -> `energy_kwh` (kWh, energy).
                       At read time unit_converter refuses this and flags
                       `unit_quantity_mismatch`, leaving the raw number in a
                       field of the wrong dimension. That is a wrong number in a
                       control room with a flag nobody reads; it belongs at
                       build.
  tag_collision        the same raw tag resolves to two different canonical
                       fields — across packs sharing an alias/OEM key, or within
                       one pack once unit suffixes and punctuation are folded
                       away (corpus.Pack.folded_all). The resolver settles a
                       fold collision by sorted order, which is deterministic
                       and arbitrary.
  missing_decoder      a pack declares a documented enum/state token in
                       `tag_enums` and the target field's registry decoder has no
                       entry for it, so the token lands verbatim in a field whose
                       `canonical_values` do not contain it. The engine has no
                       mechanism to notice.

DIMENSION IS NOT RESTATED HERE. It is derived from the tables that already own
it — `unit_converter.QUANTITY` (unit -> quantity) composed with
`unit_converter.DIMENSION` (quantity -> dimension override) — the same two-step
the converter itself performs at unit_converter.py:1044-1045. The field side
prefers `unit_converter.field_declared_unit()` (the field NAME, authoritative per
its own docstring) and falls back to the registry's `unit`, so a field whose name
and registry disagree is reported rather than silently resolved.

USAGE
    python3 tools/compile_pack.py                     # compile every pack
    python3 tools/compile_pack.py app/packs/haas.json  # compile named packs
    python3 tools/compile_pack.py --release out.json   # also cut a release
    python3 tools/compile_pack.py --json               # machine-readable report

Exit 0 = every pack compiled. Exit 1 = at least one refusal. There is no
--force: the point of the gate is that it cannot be waved through.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import corpus as _corpus              # noqa: E402
from app import field_registry as _registry    # noqa: E402
from app import unit_converter as _uc          # noqa: E402

PACK_DIR = os.path.join(ROOT, "app", "packs")

# Required top-level keys and their types. Read off corpus.Pack.__init__
# (app/corpus.py:607-623): these are the ones it indexes without a default, so a
# pack missing one raises KeyError at load and takes the process down.
_REQUIRED = {
    "oem": str, "display_name": str, "vertical": str, "protocol": str,
    "canonical_fields": list, "mappings": dict,
}
_OPTIONAL = {
    "aliases": list, "source": str, "units": dict, "tag_units": dict,
    "sunspec_model": str, "sunspec_sf_map": dict, "provenance": dict,
    "mapping_provenance": dict, "topup_history": list,
    "vendor_native_note": str,
    "tag_enums": dict, "tag_polarity": dict, "not_mapped": dict,
    # Two licensing keys are typed loosely because the existing packs disagree
    # with each other: `excluded_from_license` is a bool in 4 packs and would be
    # a list in any pack that named the excluded rows, and `license_exclusion`
    # is a prose string in some and an object in others. Nothing reads either at
    # runtime, so the compiler checks the shape it is given and does not force a
    # migration it has no reason to force.
    "excluded_from_license": (bool, list),
    "license_exclusion": (dict, str),
}
# Sibling maps keyed by RAW TAG. Every key must be a tag the pack maps, or the
# metadata describes a row that does not exist and will never be read.
_TAG_KEYED = ("tag_units", "tag_enums", "tag_polarity", "mapping_provenance")
_POLARITY_VOCAB = frozenset({"higher_is_worse", "higher_is_better", "none"})

BLOCKING = ("malformed", "unknown_canonical", "dimension_mismatch",
            "tag_collision", "missing_decoder")


class Refusal(Exception):
    """One pack did not compile. Carries the class and the reason."""

    def __init__(self, cls: str, pack: str, detail: str):
        self.cls, self.pack, self.detail = cls, pack, detail
        super().__init__(f"[{cls}] {pack}: {detail}")


# ── dimension, derived from the EXISTING tables (never restated) ─────────────
def unit_dimension(unit):
    """The dimension a unit converts within. unit -> QUANTITY -> DIMENSION.

    Exactly unit_converter.py:1044-1045's two-step, which is also what the
    private `_si_dimension` does. Reimplemented as two dict reads rather than
    imported so the compiler does not depend on a private name, and verified
    against `_si_dimension` by tests/test_pack_compiler.py.
    """
    if not unit:
        return None
    q = _uc.QUANTITY.get(unit)
    if q is None:
        q = _uc.QUANTITY.get(_uc.UNIT_ALIASES.get(unit, ""))
    return _uc.DIMENSION.get(q, q) if q else None


def field_unit(canonical):
    """(unit, source) for a canonical field. The NAME wins over the registry.

    `field_declared_unit` documents itself as authoritative because the field
    name is the contract the rest of the stack reads (see the memory note: the
    field NAME is authoritative; convert or flag, never both). The registry is
    the fallback for the many fields whose name carries no unit suffix.
    """
    named = _uc.field_declared_unit(canonical)
    if named:
        return named, "field_name"
    spec = _registry.field_spec(canonical) or {}
    return spec.get("unit"), "registry"


def field_dimension(canonical):
    """(dimension, source) for a canonical field.

    Neither repo has a field->dimension function; this composes the two answers
    that exist and prefers the stored registry `dimension` only when the unit
    route yields nothing, so a stored dimension cannot overrule a declared unit.
    """
    unit, src = field_unit(canonical)
    dim = unit_dimension(unit)
    if dim:
        return dim, f"{src}:{unit}"
    spec = _registry.field_spec(canonical) or {}
    stored = spec.get("dimension")
    return (stored, "registry:dimension") if stored else (None, "none")


# ── the five checks ─────────────────────────────────────────────────────────
def check_malformed(name, raw):
    for key, typ in _REQUIRED.items():
        if key not in raw:
            raise Refusal("malformed", name,
                          f"required key {key!r} is missing. corpus.Pack."
                          f"__init__ indexes it without a default "
                          f"(app/corpus.py:607-618), so this pack raises "
                          f"KeyError at load and takes the engine down with it.")
        if not isinstance(raw[key], typ):
            raise Refusal("malformed", name,
                          f"{key!r} must be {typ.__name__}, got "
                          f"{type(raw[key]).__name__}")
    for key, typ in _OPTIONAL.items():
        if key in raw and not isinstance(raw[key], typ):
            want = (typ.__name__ if isinstance(typ, type)
                    else " or ".join(t.__name__ for t in typ))
            raise Refusal("malformed", name,
                          f"optional key {key!r} must be {want}, got "
                          f"{type(raw[key]).__name__}")
    if not raw["mappings"]:
        raise Refusal("malformed", name, "`mappings` is empty: a pack that maps "
                                         "nothing is a pack that silently does "
                                         "nothing")
    for tag, canon in raw["mappings"].items():
        if not isinstance(tag, str) or not isinstance(canon, str) or not canon:
            raise Refusal("malformed", name,
                          f"every mapping must be str -> non-empty str; "
                          f"{tag!r} -> {canon!r} is "
                          f"{type(tag).__name__} -> {type(canon).__name__}")
    declared = set(raw["mappings"])
    for key in _TAG_KEYED:
        orphans = sorted(set(raw.get(key) or {}) - declared)
        if orphans:
            raise Refusal("malformed", name,
                          f"{key} is keyed by RAW TAG and carries "
                          f"{len(orphans)} key(s) this pack does not map: "
                          f"{orphans[:6]}. Metadata for a row that does not "
                          f"exist is never read, so a typo there is invisible "
                          f"— which is how a declared wire unit silently stops "
                          f"applying.")
    bad_pol = {t: v for t, v in (raw.get("tag_polarity") or {}).items()
               if v not in _POLARITY_VOCAB}
    if bad_pol:
        raise Refusal("malformed", name,
                      f"tag_polarity values must be one of "
                      f"{sorted(_POLARITY_VOCAB)}; got {bad_pol}")
    for tag, toks in (raw.get("tag_enums") or {}).items():
        if not isinstance(toks, list) or not toks or \
                not all(isinstance(t, str) for t in toks):
            raise Refusal("malformed", name,
                          f"tag_enums[{tag!r}] must be a non-empty list of "
                          f"strings, got {toks!r}")


def check_unknown_canonical(name, raw, dictionary):
    known_dict = set(dictionary["fields"])
    bad = sorted({c for c in raw["mappings"].values() if c not in known_dict})
    if bad:
        raise Refusal("unknown_canonical", name,
                      f"{len(bad)} mapping target(s) are not canonical fields: "
                      f"{bad[:8]}. Layer 1 returns a pack target verbatim at "
                      f"confidence 1.0 with no membership check "
                      f"(app/corpus.py:1353-1357), so an invented name ships as "
                      f"a confirmed mapping.")
    bad_reg = sorted({c for c in raw["mappings"].values()
                      if not _registry.is_canonical(c)})
    if bad_reg:
        raise Refusal("unknown_canonical", name,
                      f"{len(bad_reg)} target(s) are in the resolve-time "
                      f"dictionary but NOT in the registry that owns units and "
                      f"bounds: {bad_reg[:8]}. Such a field resolves and then "
                      f"has no unit contract and no bounds.")
    listed, used = set(raw["canonical_fields"]), set(raw["mappings"].values())
    if listed != used:
        raise Refusal("malformed", name,
                      f"`canonical_fields` disagrees with `mappings`: "
                      f"{len(listed - used)} listed but unused "
                      f"{sorted(listed - used)[:5]}, "
                      f"{len(used - listed)} used but unlisted "
                      f"{sorted(used - listed)[:5]}. The index publishes "
                      f"canonical_field_count from this list and nothing else "
                      f"checks it (tests/test_prospect_evaluation.py:667-698 "
                      f"checks mapping_count only).")


def check_dimension(name, raw):
    """Refuse a declared wire unit whose dimension is not the target's."""
    tag_units = raw.get("tag_units") or {}
    problems = []
    for tag, unit in sorted(tag_units.items()):
        canon = raw["mappings"][tag]
        src_dim = unit_dimension(unit)
        if src_dim is None:
            problems.append(
                f"{tag!r} declares wire unit {unit!r}, which unit_converter."
                f"QUANTITY does not recognise — declared-but-unconvertible is "
                f"worse than undeclared: the unit is trusted and the "
                f"conversion never happens")
            continue
        dst_dim, dst_src = field_dimension(canon)
        if dst_dim is None:
            continue                     # unitless/enum target: nothing to check
        if src_dim != dst_dim:
            problems.append(
                f"{tag!r} ({unit} = {src_dim}) -> {canon!r} "
                f"({dst_src} = {dst_dim}): refusing to ship a cross-dimension "
                f"mapping. unit_converter would flag this "
                f"`unit_quantity_mismatch` at read time and leave the raw "
                f"number in a {dst_dim} field (unit_converter.py:1044-1054)")
    if problems:
        raise Refusal("dimension_mismatch", name,
                      f"{len(problems)} dimension problem(s): " +
                      "; ".join(problems[:6]))
    # SCALE, not just dimension. A field can be dimensionless-but-scaled: the
    # registry's `robot.speed_scaling` has unit null and physics_bounds [0, 1]
    # — a RATIO — and a pack declaring a `%` wire unit on it ships a reading
    # that the read-time validator NULLS as "physics_violation: 100 outside
    # [0, 1]". The dimension check cannot see it (unit null => no dimension), so
    # a percent-shaped wire unit against unit-bounds is checked separately.
    for tag, unit in sorted(tag_units.items()):
        canon = raw["mappings"][tag]
        if unit_dimension(unit) != "percent":
            continue
        spec = _registry.field_spec(canon) or {}
        if spec.get("unit"):
            continue                      # the dimension check already covers it
        pb = spec.get("physics_bounds")
        if isinstance(pb, dict) and pb.get("max") is not None \
                and float(pb["max"]) <= 1.0:
            raise Refusal("dimension_mismatch", name,
                          f"{tag!r} declares wire unit {unit!r} (percent) but "
                          f"{canon!r} declares no unit and physics_bounds "
                          f"{[pb.get('min'), pb.get('max')]} — a RATIO, not a "
                          f"percent. Every reading above the bound is nulled at "
                          f"read time as a physics_violation and the pack looks "
                          f"correct while producing nothing. Use a field whose "
                          f"declared unit is '%', or divide by 100 upstream.")

    # Also check the canonical-keyed `units` override, which overrides the
    # dictionary's unit for the FIELD and so can contradict the field name.
    for canon, unit in sorted((raw.get("units") or {}).items()):
        named = _uc.field_declared_unit(canon)
        if not named or not unit:
            continue
        a, b = unit_dimension(unit), unit_dimension(named)
        if a and b and a != b:
            raise Refusal("dimension_mismatch", name,
                          f"units[{canon!r}] = {unit!r} ({a}) contradicts the "
                          f"dimension its own field NAME declares "
                          f"({named} = {b}). The name is authoritative; an "
                          f"override that changes the dimension is a renamed "
                          f"field, not a unit.")


def check_collisions(name, raw, others):
    """Within-pack fold collisions, and cross-pack collisions on a shared key."""
    pack = _corpus.Pack(raw)
    internal = []
    for key, rows in sorted(pack.folded_all.items()):
        canons = {c for _t, c, _u in rows}
        if len(canons) > 1:
            internal.append(f"{key!r} <- " + ", ".join(
                f"{t!r}->{c}" for t, c, _u in sorted(rows)))
    if internal:
        raise Refusal("tag_collision", name,
                      f"{len(internal)} tag(s) collide once unit suffixes and "
                      f"punctuation are folded away, and resolve to DIFFERENT "
                      f"canonical fields: {internal[:4]}. corpus.Pack.folded "
                      f"keeps whichever sorted first (app/corpus.py:634-639), "
                      f"so the loser is silently unreachable through layer 1b.")

    # Cross-pack: two packs claiming the same OEM key (oem or any alias) and
    # the same raw tag for different canonicals. The alias map is first-wins
    # (`by_alias.setdefault`, app/corpus.py:657), so the second pack's row is
    # unreachable for that alias and nothing says so.
    mine = {str(raw["oem"]).lower(), *(str(a).lower() for a in raw.get("aliases") or [])}
    clashes = []
    for other_name, other in sorted(others.items()):
        if other_name == name:
            continue
        theirs = {str(other["oem"]).lower(),
                  *(str(a).lower() for a in other.get("aliases") or [])}
        shared_key = mine & theirs
        if not shared_key:
            continue
        for tag, canon in sorted(raw["mappings"].items()):
            their_canon = other["mappings"].get(tag)
            if their_canon and their_canon != canon:
                clashes.append(f"{tag!r}: this pack says {canon}, "
                               f"{other['oem']!r} says {their_canon} "
                               f"(shared key {sorted(shared_key)})")
    if clashes:
        raise Refusal("tag_collision", name,
                      f"{len(clashes)} raw tag(s) collide with an existing pack "
                      f"that shares an OEM key: {clashes[:4]}. by_alias is "
                      f"first-wins (app/corpus.py:657), so which answer a "
                      f"caller gets depends on filename sort order.")
    if not clashes:
        for other_name, other in sorted(others.items()):
            if other_name == name:
                continue
            dupe = sorted(set(raw["oem"] for _ in [0]) &
                          {other["oem"], *(other.get("aliases") or [])})
            if dupe:
                raise Refusal("tag_collision", name,
                              f"OEM key {dupe[0]!r} is already claimed by "
                              f"{other_name}. by_alias.setdefault means this "
                              f"pack would be unreachable by its own name.")


def check_decoders(name, raw, dictionary):
    """Every documented enum token must decode on the target field."""
    problems = []
    for tag, tokens in sorted((raw.get("tag_enums") or {}).items()):
        canon = raw["mappings"][tag]
        spec = _registry.field_spec(canon) or {}
        values = spec.get("canonical_values")
        decoder = spec.get("mappings") or {}
        if not values:
            problems.append(
                f"{tag!r} declares {len(tokens)} documented state token(s) but "
                f"its target {canon!r} has no `canonical_values` and no "
                f"decoder at all — every token lands verbatim as free text")
            continue
        upper = {str(k).upper(): v for k, v in decoder.items()}
        missing = [t for t in tokens
                   if t not in decoder and str(t).upper() not in upper]
        if missing:
            problems.append(
                f"{tag!r} -> {canon!r} has no decoder entry for "
                f"{missing}; the field's canonical_values are {values}, so "
                f"those token(s) would be stored as-is and every consumer "
                f"reading canonical_values would miss them")
    if problems:
        raise Refusal("missing_decoder", name,
                      f"{len(problems)} undecodable state set(s): " +
                      "; ".join(problems[:4]))


# ── driver ──────────────────────────────────────────────────────────────────
def pack_files(paths=None):
    if paths:
        return [os.path.abspath(p) for p in paths]
    return [os.path.join(PACK_DIR, f) for f in sorted(os.listdir(PACK_DIR))
            if f.endswith(".json") and not f.startswith("_")]


def compile_packs(paths=None):
    """(ok: list[dict], refusals: list[Refusal]). Never raises."""
    files = pack_files(paths)
    loaded, refusals = {}, []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path) as fh:
                loaded[name] = json.load(fh)
        except (OSError, ValueError) as exc:
            refusals.append(Refusal("malformed", name,
                                    f"not readable as JSON: "
                                    f"{type(exc).__name__}: {exc}"))
    # The whole on-disk set, so a cross-pack collision is found even when only
    # one pack is being compiled.
    world = {}
    for f in pack_files():
        try:
            with open(f) as fh:
                w = json.load(fh)
            if isinstance(w, dict) and "mappings" in w and "oem" in w:
                world[os.path.basename(f)] = w
        except (OSError, ValueError):
            pass
    world.update({k: v for k, v in loaded.items()
                  if isinstance(v, dict) and "mappings" in v and "oem" in v})

    _, _, dictionary = _corpus.load()
    ok = []
    for name, raw in loaded.items():
        try:
            if not isinstance(raw, dict):
                raise Refusal("malformed", name,
                              f"top level must be an object, got "
                              f"{type(raw).__name__}")
            check_malformed(name, raw)
            check_unknown_canonical(name, raw, dictionary)
            check_dimension(name, raw)
            check_collisions(name, raw, world)
            check_decoders(name, raw, dictionary)
        except Refusal as r:
            refusals.append(r)
            continue
        ok.append({
            "pack": name, "oem": raw["oem"], "vertical": raw["vertical"],
            "protocol": raw["protocol"],
            "aliases": sorted(raw.get("aliases") or []),
            "display_name": raw["display_name"],
            "mapping_count": len(raw["mappings"]),
            "canonical_field_count": len(set(raw["mappings"].values())),
            "declared_wire_units": len(raw.get("tag_units") or {}),
            "declared_enums": len(raw.get("tag_enums") or {}),
            "declared_polarity": len(raw.get("tag_polarity") or {}),
            "cited_rows": len(raw.get("mapping_provenance") or {}),
            "documented_but_unmapped": len(raw.get("not_mapped") or {}),
        })
    return ok, refusals


def cut_release(ok, out_path=None):
    """A release manifest: the compiled set, its counts, and a content digest.

    The digest is over the compiled summary, not the files, so reformatting a
    pack does not move it and changing a mapping does. `corpus_release` is the
    engine's own corpus identifier, included so a release can be tied to the
    version /health will report.
    """
    body = {
        "compiled": sorted(ok, key=lambda r: r["pack"]),
        "pack_count": len(ok),
        "mapping_total": sum(r["mapping_count"] for r in ok),
        "compiler": "tools/compile_pack.py",
        "blocking_classes": list(BLOCKING),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    body["digest_sha256"] = hashlib.sha256(canonical).hexdigest()
    try:
        from app import corpus_version
        body["corpus_release"] = corpus_version.release()
    except Exception as exc:                      # never block a release cut
        body["corpus_release"] = None
        body["corpus_release_error"] = f"{type(exc).__name__}: {exc}"
    if out_path:
        with open(out_path, "w") as fh:
            json.dump(body, fh, indent=2, sort_keys=True)
            fh.write("\n")
    return body


def rebuild_index(ok, out_path=None):
    """Regenerate app/packs/_index.json from the COMPILED packs.

    The manifest is prospect-facing and only its pack set and mapping_count are
    tested (tests/test_prospect_evaluation.py:667-698); aliases, vertical,
    protocol, display_name and canonical_field_count were free to drift. Writing
    it from the compiler's own output is what stops that.
    """
    path = out_path or os.path.join(PACK_DIR, "_index.json")
    with open(path) as fh:
        idx = json.load(fh)
    # MERGE, never replace. 23 of the 29 packs that predate this compiler do not
    # compile yet (see GATE1_GATE2.md), and rewriting `packs` from the compiled
    # set alone would silently delete them from the prospect-facing manifest and
    # break tests/test_prospect_evaluation.py's three-way agreement check. The
    # compiler owns the rows it compiled and leaves the rest alone.
    idx.setdefault("packs", {}).update({
        r["oem"]: {"aliases": r["aliases"],
                   "canonical_field_count": r["canonical_field_count"],
                   "display_name": r["display_name"],
                   "mapping_count": r["mapping_count"],
                   "protocol": r["protocol"],
                   "vertical": r["vertical"]}
        for r in sorted(ok, key=lambda r: r["oem"])
    })
    with open(path, "w") as fh:
        json.dump(idx, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("packs", nargs="*", help="pack files (default: all)")
    ap.add_argument("--release", metavar="PATH", help="write a release manifest")
    ap.add_argument("--write-index", action="store_true",
                    help="regenerate app/packs/_index.json from the compiled set")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)

    ok, refusals = compile_packs(args.packs or None)
    by_class = defaultdict(list)
    for r in refusals:
        by_class[r.cls].append(r)

    if args.json:
        print(json.dumps({
            "compiled": ok,
            "refused": [{"class": r.cls, "pack": r.pack, "detail": r.detail}
                        for r in refusals],
        }, indent=2, sort_keys=True))
    else:
        for r in ok:
            print(f"  OK      {r['pack']:<26} {r['mapping_count']:>5} mappings "
                  f"-> {r['canonical_field_count']:>3} canonicals  "
                  f"({r['declared_wire_units']} wire units, "
                  f"{r['cited_rows']} cited rows)")
        for cls in BLOCKING:
            for r in by_class.get(cls, []):
                print(f"  REFUSED {r.pack:<26} [{cls}]")
                print(f"          {r.detail}")
        print(f"\n{len(ok)} pack(s) compiled, {len(refusals)} refused "
              f"({sum(r['mapping_count'] for r in ok)} mappings total)")

    if refusals:
        return 1
    if args.write_index:
        print("index:", rebuild_index(ok))
    if args.release:
        rel = cut_release(ok, args.release)
        print(f"release: {args.release}  digest={rel['digest_sha256'][:16]}... "
              f"corpus_release={rel.get('corpus_release')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
