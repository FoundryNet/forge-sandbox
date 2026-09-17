"""forge_core.corpus_version — what decided this reading.

WHY THIS EXISTS
---------------
`nte_hash` proved a normalize response was not MODIFIED. It did not prove what
the response MEANT, because the meaning of `spindle_load_pct: 87.0` is a
function of the corpus that produced it — the packs, the curated rows, the unit
table, the physics bounds, the sentinel sets. Change any of those and the same
raw tag resolves to a different canonical, or the same canonical carries a
different number, while the old digest stays perfectly valid over the old bytes.

So an attested record was provably unaltered and NOT provably interpretable.
That is the gap between a receipt and a citation, and it is the only gap that
matters once a settlement, a warranty claim or a regulatory filing turns on the
value: the counterparty does not ask "were these bytes edited", they ask "what
did this field mean on the day you decided it".

This module produces the missing identifier and puts it inside the digest.

WHAT IS DIGESTED — SEMANTIC CONTENT, NOT FILE BYTES
---------------------------------------------------
Hashing the .py/.json files on disk would be simpler and wrong. A reformatted
comment would bump the corpus version (churning every citation for no semantic
reason) while a mapping edited through any path that does not touch those exact
files would not. We digest the LOADED DECISION TABLES instead:

    registry   canonical_fields.json → per field: unit, dimension, quantity,
               physics_bounds, accepted_input_units, si.  NOT corpus_tags or
               generated_at, which drift without changing any decision.
    packs      universal_normalize._PACKS → (oem, standard, tag → canonical)
               plus the declared per-pack units.
    bounds     physics_validator.PHYSICAL_BOUNDS
    sentinels  value_validator: string + tier-1 + tier-2 sentinel sets
    units      unit_converter: CONVERSIONS, TARGET_UNIT, UNIT_ALIASES, QUANTITY

Each of those is something that, if changed, changes the output for some input.
Nothing else is in the digest.

STATIC vs DYNAMIC — TWO HALVES, DELIBERATELY SEPARATE
-----------------------------------------------------
The five components above ship with the container: they are fixed for the life
of a deploy, which is what makes them citable. They compose into the RELEASE id:

    corpus_version = "1.0.0-a3f8e2c9"
                      ^^^^^ ^^^^^^^^
                      |     digest over the five decision tables (automatic)
                      semver, bumped by hand (intent: breaking vs additive)

The semver is what goes in contract language; the suffix is what an auditor
recomputes. The suffix is automatic ON PURPOSE: if someone edits a pack and
forgets to bump the semver, the suffix still moves, so it is impossible to ship
two different corpora under one identifier. Manual-only versioning fails open;
this fails closed.

But two layers are NOT fixed for the life of a deploy:

    index       the confirmed rows in forge_schema_mappings, refreshed hourly
                into corpus_index — this is the corpus_exact/corpus_folded layer
    auto_packs  mappings promoted out of the LLM cache at runtime

Both decide real mappings. Folding them into the release id would make it churn
hourly and destroy its citability; leaving them out of the digest entirely would
make the digest lie. So they are reported separately, as `corpus_state`, and —
like everything else in the response — they land INSIDE the nte_hash. The
citable string stays stable; the hash still covers the whole truth.

ENGINE PARITY
-------------
forge-prod and forge-sandbox run different corpora by design (the sandbox is
built from public sources only and has no Supabase index). They therefore
produce DIFFERENT corpus_versions for the same input, and that is the correct
behaviour, not a parity break: they are different corpora and must not issue
interchangeable citations. What stays identical between the two engines is the
hash FUNCTION and the field names — see the parity test in both repos.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any, Optional

log = logging.getLogger("forge.corpus_version")

__all__ = [
    "CORPUS_SEMVER", "ENGINE", "release", "release_detail", "state",
    "layer_digests", "invalidate",
]

# ── Bump by hand when the meaning of the corpus changes. ─────────────────────
# MAJOR  a canonical field is removed or its meaning/unit changes (a record
#        decided under the old major cannot be read under the new one)
# MINOR  fields or mappings added; existing decisions unchanged
# PATCH  a mapping corrected — same vocabulary, a different answer for some tag
#
# Forgetting to bump this does NOT allow two corpora to share an identifier:
# the digest suffix is computed from the tables themselves. The semver carries
# the INTENT (is this breaking?), the suffix carries the FACT (did anything
# change?). Both are needed; neither substitutes for the other.
CORPUS_SEMVER = "1.0.0"

# Which engine issued the citation. prod and sandbox hold different corpora, so
# a bare version string is ambiguous without this.
ENGINE = "forge-sandbox"

_SUFFIX_LEN = 8          # 32 bits — birthday-safe past ~65k releases, still readable
_SHORT_LEN  = 8          # dynamic-layer digests, same rationale

_lock = threading.RLock()
_cache: dict[str, Any] = {}


# ── helpers ──────────────────────────────────────────────────────────────────
def _strict(o: Any):
    """Refuse to digest anything without a stable cross-process representation.

    This exists because of a real bug caught before ship. `unit_converter.
    CONVERSIONS` stores `(lambda, rule_name)` tuples, and a permissive
    `default=str` happily serialised the lambda as
    `<function <lambda> at 0x10f378860>` — a MEMORY ADDRESS. Every process got
    a different corpus version for an identical corpus, which is the one
    failure that would make this whole module worthless: two replicas of one
    deploy issuing different citations for the same work.

    Failing loudly here means a future non-data object surfaces as a visible
    `error` on that component in /health, instead of silently poisoning every
    citation the deploy issues."""
    raise TypeError(
        f"corpus_version: refusing to digest {type(o).__name__} — no stable "
        "representation across processes. Digest its behaviour, not the object.")


def _sha(obj: Any) -> str:
    """sha256 over a canonicalised structure: sorted keys, no whitespace, and a
    strict encoder. Every container is normalised to a sorted list by `_sorted`
    before it gets here, so set iteration order cannot leak in either."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   default=_strict).encode("utf-8")).hexdigest()


def _sorted(x):
    """Order-independent normalisation. Set iteration order is not stable across
    processes, so a raw set would give two replicas of the SAME deploy two
    different corpus versions — the exact failure this module exists to prevent."""
    if isinstance(x, (set, frozenset)):
        return sorted(x, key=str)
    if isinstance(x, (list, tuple)):
        return [_sorted(i) for i in x]
    if isinstance(x, dict):
        return {str(k): _sorted(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    return x


# ── the five static decision tables ──────────────────────────────────────────
def _registry_component() -> dict:
    """_canonical_fields.json, reduced to the facts that decide an output."""
    from app import field_registry
    fields = {}
    for name, spec in (field_registry.fields() or {}).items():
        if not isinstance(spec, dict):
            continue
        fields[name] = {
            "unit":        spec.get("unit"),
            "dimension":   spec.get("dimension"),
            "quantity":    spec.get("quantity"),
            "si":          spec.get("si"),
            "bounds":      spec.get("physics_bounds"),
            "valid_range": spec.get("valid_range"),
            "accepts":     _sorted(spec.get("accepted_input_units") or []),
        }
    return {"sha256": _sha(_sorted(fields)), "fields": len(fields),
            "registry_version": getattr(field_registry, "REGISTRY_VERSION", None)}


def _packs_component() -> dict:
    """The vendor packs in app/packs. `tag_units` is digested alongside the
    mappings because a register named for its quantity (SunSpec `W`, `WH`)
    carries its unit ONLY there — change it and the stored value changes with
    no mapping edit at all."""
    from app import corpus
    packs_obj, _by_alias, _dictionary = corpus.load()
    packs = []
    for oem, p in sorted(packs_obj.items()):
        packs.append({
            "oem": oem, "vertical": getattr(p, "vertical", None),
            "protocol": getattr(p, "protocol", None),
            "aliases": _sorted(getattr(p, "aliases", []) or []),
            "mappings": _sorted(getattr(p, "mappings", {}) or {}),
            "units": _sorted(getattr(p, "units", {}) or {}),
            "tag_units": _sorted(getattr(p, "tag_units", {}) or {}),
            "sunspec_model": getattr(p, "sunspec_model", None),
            "sunspec_sf_map": _sorted(getattr(p, "sunspec_sf_map", None) or {}),
        })
    return {"sha256": _sha(packs), "packs": len(packs),
            "mappings": sum(len(p["mappings"]) for p in packs)}


def _bounds_component() -> dict:
    """The sandbox bounds table is keyed by QUANTITY, not by field name (see
    value_validator.PHYSICS_BOUNDS_BY_QUANTITY) — a different mechanism from
    production's per-field PHYSICAL_BOUNDS, digested the same way."""
    from app import value_validator as vv
    b = _sorted(getattr(vv, "PHYSICS_BOUNDS_BY_QUANTITY", {}) or {})
    return {"sha256": _sha(b), "quantities": len(b)}


def _sentinels_component() -> dict:
    from app import value_validator as vv
    payload = {
        "strings":     _sorted(getattr(vv, "SENTINEL_STRINGS", frozenset())),
        "always":      _sorted(getattr(vv, "ALWAYS_SENTINEL_NUMBERS", frozenset())),
        "contractual": _sorted(getattr(vv, "CONTRACTUAL_SENTINEL_NUMBERS", frozenset())),
    }
    return {"sha256": _sha(payload),
            "tokens": sum(len(v) for v in payload.values())}


# Fixed probes for conversion behaviour. -40 is deliberate: it is the F/C
# crossover, so a sign error or a swapped direction that 1.0 and 100.0 might
# both survive shows up here.
_PROBE_INPUTS = (1.0, 100.0, -40.0)


def _conversion_behaviour(fn) -> list:
    """What a conversion DOES, as a stable string.

    Digesting the rule NAME would miss a changed factor — `psi_to_bar` edited
    from 0.0689476 to 0.07 keeps its name while silently changing every stored
    pressure. Digesting the callable is impossible (see `_strict`). So we
    evaluate it at fixed inputs: the output is the rule's observable identity,
    which is exactly the thing a citation needs to pin down.

    `%.12g` drops the last couple of float ULPs so a libm difference between
    build platforms cannot fork the corpus version of an unchanged table."""
    out = []
    for x in _PROBE_INPUTS:
        try:
            out.append(f"{float(fn(x)):.12g}")
        except Exception as e:
            out.append(f"error:{type(e).__name__}")
    return out


def _units_component() -> dict:
    """The conversion tables. A changed factor changes every stored value that
    passes through it, so this is as load-bearing as the mappings themselves."""
    from app import unit_converter as uc
    conversions = []
    for key, val in (getattr(uc, "CONVERSIONS", {}) or {}).items():
        src, dst = (list(key) + [None, None])[:2] if isinstance(key, tuple) else (key, None)
        fn, rule = val if isinstance(val, tuple) and len(val) == 2 else (val, None)
        conversions.append([str(src), str(dst), rule,
                            _conversion_behaviour(fn) if callable(fn) else _sorted(fn)])
    conversions.sort(key=lambda r: (r[0], r[1], str(r[2])))
    payload = {
        "conversions": conversions,
        "target_unit": _sorted(getattr(uc, "TARGET_UNIT", {}) or {}),
        "aliases":     _sorted(getattr(uc, "UNIT_ALIASES", {}) or {}),
        "quantity":    _sorted(getattr(uc, "QUANTITY", {}) or {}),
    }
    return {"sha256": _sha(payload), "conversions": len(conversions)}


_COMPONENTS = (
    ("registry",  _registry_component),
    ("packs",     _packs_component),
    ("bounds",    _bounds_component),
    ("sentinels", _sentinels_component),
    ("units",     _units_component),
)


# ── public: the static release id ────────────────────────────────────────────
def layer_digests() -> dict:
    """Per-component digests. Computed once per process — these tables are
    immutable for the life of the container.

    Exposed so an auditor comparing two releases can LOCALISE the change: if
    only `units` moved, no mapping was re-pointed and only converted values are
    affected. A single opaque version id cannot answer that question."""
    with _lock:
        if "layers" in _cache:
            return _cache["layers"]
    out: dict = {}
    for name, fn in _COMPONENTS:
        try:
            out[name] = fn()
        except Exception as e:                                # pragma: no cover
            # An unreadable component must be VISIBLE, never silently omitted —
            # omitting it would let the version claim to cover something it did
            # not. The error text is part of the digest input, so a deploy that
            # cannot read its own packs gets a distinct version.
            log.warning("corpus_version: component %s unreadable: %s", name, e)
            out[name] = {"sha256": _sha(f"unavailable:{e!r}"), "error": str(e)}
    with _lock:
        _cache["layers"] = out
    return out


def _release_digest() -> str:
    with _lock:
        if "digest" in _cache:
            return _cache["digest"]
    layers = layer_digests()
    d = _sha({k: v.get("sha256") for k, v in layers.items()})
    with _lock:
        _cache["digest"] = d
    return d


def release() -> str:
    """The citable identifier, e.g. "forge-prod/1.0.0-a3f8e2c9".

    Stable for the life of a deploy. This is the string that goes in a contract,
    a claim file or a disclosure footnote.

    The engine is IN the string rather than only in the sibling `corpus_state`,
    because prod and sandbox hold different corpora and a bare "1.0.0-a3f8e2c9"
    cannot say which one decided the reading. A citation that needs a second
    field to disambiguate is not a citation — it has to survive being pasted
    into a claim file on its own."""
    return f"{ENGINE}/{CORPUS_SEMVER}-{_release_digest()[:_SUFFIX_LEN]}"


def release_detail() -> dict:
    """Everything an auditor needs to verify a citation. Served by /health."""
    layers = layer_digests()
    return {
        "corpus_version": release(),
        "semver":         CORPUS_SEMVER,
        "engine":         ENGINE,
        "digest":         _release_digest(),          # full 64-char
        "layers":         layers,
        "state":          state(),
    }


# ── public: the dynamic half ─────────────────────────────────────────────────
# Production's movable layers are the Supabase corpus index and runtime-promoted
# auto-packs. This engine has neither. What moves HERE is:
#
#   fleet_overlay   mappings overlaid at runtime by corpus.apply_fleet_overlay
#   satellite       the corpus-delta version the control plane has pushed and
#                   this container has applied (satellite.AGENT.corpus_version)
#
# Different layers, same rule: anything that can change what a tag means after
# the image was built has to be inside the digest, or the citation is a claim
# about a corpus that is not the one that answered.
#
# NOTE these are version STRINGS, not digests — the control plane issues them —
# so they are carried whole rather than truncated. Truncating a version label to
# eight characters would make "1.47" and "1.47-hotfix" indistinguishable.
def _fleet_overlay_version():
    try:
        from app import corpus
        return corpus.fleet_overlay_version()
    except Exception as e:                                    # pragma: no cover
        log.debug("corpus_version: fleet overlay unavailable: %s", e)
        return None


def _satellite_version():
    try:
        from app.satellite import AGENT
        return AGENT.corpus_version if AGENT.enabled() else None
    except Exception as e:                                    # pragma: no cover
        log.debug("corpus_version: satellite version unavailable: %s", e)
        return None


def state() -> dict:
    """The compact corpus identity carried on every normalize response.

    `release` is stable and citable. The rest are the layers that move
    underneath a running container; they are here so the nte_hash covers them.
    A null means that layer contributed nothing to this deploy."""
    return {
        "release":       release(),
        "engine":        ENGINE,
        "fleet_overlay": _fleet_overlay_version(),
        "satellite":     _satellite_version(),
    }


def invalidate() -> None:
    """Drop the static cache. Tests only — the tables do not change in a running
    container, and recomputing per request would put a full registry walk on the
    hot path."""
    with _lock:
        _cache.clear()


# ── evidentiary ladder ───────────────────────────────────────────────────────
# A certificate that prints `confidence: 1.0` for a hand-curated pack row and
# `confidence: 0.94` for an LLM guess has already lost the argument: those two
# numbers are not on the same scale and never were. One is an assertion about a
# table, the other is a model's self-report. `match_layer` names WHERE the
# mapping came from and `evidence` names WHAT KIND of claim it is, so a reader
# can weigh them without knowing the engine's internals.
#
#   deterministic   a table decided it — replays identically against this corpus
#   probabilistic   a model decided it — an embedder, a classifier or an LLM
#   derived         carried from this machine's own prior mappings (stateful)
#   unresolved      no canonical was asserted at all
#
# The L-numbers are EVIDENCE TIERS, not the engine's internal resolution order —
# forge-sandbox, for instance, tries its fleet overlay (its own "layer 0") before
# the identity check that is L0 here. A reader of a certificate cares which kind
# of claim backs the value, not which branch of the resolver ran first.
#
# This table is shared by both engines and lists labels neither one emits alone,
# because a certificate issued by either has to be readable by the same auditor.
#
# Unknown match types fall through to ("L_unclassified", "probabilistic"): a
# label this table has not seen must never be reported as deterministic, since
# that is the one direction in which a wrong answer overstates the evidence.
MATCH_LAYERS: dict[str, tuple[str, str]] = {
    # L0 — the tag IS the canonical name
    "exact_canonical":     ("L0_identity",   "deterministic"),
    "identity":            ("L0_identity",   "deterministic"),
    # L1 — a curated table decided it
    "corpus":              ("L1_pack",       "deterministic"),
    "corpus_normalized":   ("L1_pack",       "deterministic"),
    "fleet_corpus":        ("L1_fleet",      "deterministic"),
    # Resolved against a DIFFERENT vendor's pack. Still a table lookup, so it
    # replays exactly — but the provenance is weaker than a pack written for
    # this OEM, and the layer name is where that shows. `evidence` answers
    # "does this replay?", not "how much would I bet on it".
    "cross_oem":               ("L1_cross_oem", "deterministic"),
    "cross_oem_unit_evidence": ("L1_cross_oem", "deterministic"),
    "deterministic":       ("L1_pack",       "deterministic"),
    "auto_pack":           ("L1_auto_pack",  "deterministic"),
    "corpus_exact":        ("L1_corpus",     "deterministic"),
    "corpus_folded":       ("L1_corpus",     "deterministic"),
    "user_confirmed":      ("L1_user",       "deterministic"),
    "confirmed_local":     ("L1_confirmed",  "deterministic"),
    # L2 — a model decided it
    "vector":              ("L2_vector",     "probabilistic"),
    "signal_inferred":     ("L2_signal",     "probabilistic"),
    "signal":              ("L2_signal",     "probabilistic"),
    # L3 — an LLM decided it
    "llm_cached":          ("L3_llm_cached", "probabilistic"),
    "llm_cached_db":       ("L3_llm_cached", "probabilistic"),
    "llm_inferred":        ("L3_llm",        "probabilistic"),
    "llm":                 ("L3_llm",        "probabilistic"),
    # L4 — carried from prior state for this machine
    "sticky":              ("L4_sticky",     "derived"),
    "sticky_rename":       ("L4_sticky",     "derived"),
    # nothing was asserted
    "noise_filtered":      ("L_none",        "unresolved"),
    "abstained":           ("L_none",        "unresolved"),
    "ambiguous_abstained": ("L_none",        "unresolved"),
    "guard_rejected":      ("L_none",        "unresolved"),
    "physics_rejected":    ("L_none",        "unresolved"),
    "unit_incompatible":   ("L_none",        "unresolved"),
    "unknown":             ("L_none",        "unresolved"),
}

_UNCLASSIFIED = ("L_unclassified", "probabilistic")


def match_layer(match_type: Optional[str]) -> tuple[str, str]:
    """(layer, evidence) for a match_type. Never raises; never over-claims."""
    if not match_type:
        return _UNCLASSIFIED
    return MATCH_LAYERS.get(str(match_type), _UNCLASSIFIED)
